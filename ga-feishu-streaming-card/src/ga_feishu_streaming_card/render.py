"""卡片渲染（独立实现）。

设计约定：
- render_card(session) -> dict：飞书 interactive 卡片 JSON。
- html_escape_card_text(text)：lark_md 内容转义。
- 超限降级：内容超 safe_bytes/max_elements 时逐元素截断，保证交付可用
  （fail-open：宁可截断也不让渲染失败阻塞投递）。
依赖：session.CardSession / status.resolve_display_status / text.strip_think_tags
+ normalize_stream_text + split_markdown_blocks / limits.enforce_card_limits。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, Optional

from .config import CardLimitsConfig
from .limits import (
    CardLimitExceeded,
    card_json_bytes,
    count_elements,
    count_tables,
    enforce_card_limits,
)
from .session import CardSession, InteractionState, TimelineItem, ToolState
from .status import DisplayStatus, resolve_display_status
from .text import (
    normalize_stream_text,
    split_markdown_blocks,
    strip_think_tags,
)

_HEADER_BY_STATUS = {
    DisplayStatus.THINKING: ("思考中…", "blue"),
    DisplayStatus.IN_PROGRESS: ("生成中…", "blue"),
    DisplayStatus.RUNNING: ("运行中…", "indigo"),
    DisplayStatus.WAITING: ("等待中…", "orange"),
    DisplayStatus.COMPLETED: ("已完成", "green"),
    DisplayStatus.FAILED: ("执行失败", "red"),
}

_TOOL_STATUS_ICON = {
    "running": "⏳",
    "pending": "🕐",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "⏹️",
    "canceled": "⏹️",
}

# ---------------------------------------------------------------- header subtitle / footer（T5）
# 运行中态显示安全动作词（session.runtime_header_text 白名单）；无动作词时回退阶段文案。
_ACTIVE_STAGE_TEXT = {
    DisplayStatus.THINKING: "正在思考",
    DisplayStatus.IN_PROGRESS: "正在生成",
    DisplayStatus.RUNNING: "正在执行",
    DisplayStatus.WAITING: "等待用户操作",
}


def _abbr_count(v: int) -> str:
    """token 缩写（对齐上游 _format_count）：<1k 原样；>=1k → '1.2k'；>=1M → '1.2m'；
    整数倍不带小数（1k/2m）；1 位小数去尾零。非法输入按 0 处理。"""
    try:
        v = max(0, int(v))
    except (TypeError, ValueError):
        v = 0
    if v < 1000:
        return str(v)
    for factor, suffix in ((1_000_000, "m"), (1000, "k")):
        if v >= factor:
            scaled = v / factor
            if scaled >= 100 or scaled.is_integer():
                return f"{int(round(scaled))}{suffix}"
            return f"{scaled:.1f}".rstrip("0").rstrip(".") + suffix
    return str(v)


def _footer_meta(session: CardSession) -> str:
    """单行元信息：⏱ 耗时 · model · ↑in/↓out tokens · ctx used/max %（缺失段省略；无敏感内容）。"""
    parts: list[str] = []
    end = session.completed_at or session.updated_at or session.created_at
    if end and session.created_at:
        secs = max(0, int(end - session.created_at))
        parts.append(f"⏱ {secs}s" if secs < 60 else f"⏱ {secs // 60}m{secs % 60}s")
    if session.model:
        parts.append(session.model)
    tool_count = sum(
        1 for it in (session.timeline or [])
        if getattr(it, "kind", "") == "tool"
    )
    if tool_count:
        parts.append(f"工具调用 {tool_count} 次")
    tokens = session.tokens or {}
    if tokens:
        parts.append(
            f"↑{_abbr_count(tokens.get('input_tokens', 0))}/"
            f"↓{_abbr_count(tokens.get('output_tokens', 0))} tokens"
        )
    context = session.context or {}
    used = context.get("used_tokens", 0)
    mx = context.get("max_tokens", 0)
    if used:
        if mx > 0:
            parts.append(f"ctx {_abbr_count(used)}/{_abbr_count(mx)} {round(used / mx * 100)}%")
        else:
            parts.append(f"ctx {_abbr_count(used)}")
    return " · ".join(parts)


@dataclass
class CardRenderResult:
    """渲染结果：卡片 JSON + 交付语义 disposition（与上游 render_card_result 对齐）。

    disposition 取值：
    - "card": 正常卡片投递；
    - "deferred_native": 内容超限，降级为橙色交接卡，等待原生消息（非终态）；
    - "native": 终态内容超限，直接交接原生消息（completed 降级）。
    """

    card: Dict[str, Any]
    disposition: str = "card"
    extra: Dict[str, Any] = field(default_factory=dict)


def html_escape_card_text(text: str) -> str:
    """lark_md 文本转义：& < > \" '（& 必须先转，避免二次转义）。"""
    if not text:
        return text
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _markdown_element(content: str) -> Dict[str, Any]:
    return {"tag": "markdown", "content": html_escape_card_text(content)}


def _timeline_elements(items: list[TimelineItem]) -> list[Dict[str, Any]]:
    """渲染安全过程步骤（平铺）。

    - 思考 → 每步独立可折叠面板，标题带摘要（`💭 {前24字}`，原生 _TaskCard
      同款观感：收起也能看出每步在干什么），展开看全文；无任何 HTML 标签
      （飞书卡片 markdown 不解析 `<font>` 等标签，且 _markdown_element
      会转义尖括号，生产实测尖括号会原样显示）；
    - 工具 → 通用文案行（固定“工具调用”，不含工具名/参数/结果）。
    不读取 ToolState.detail，杜绝参数/结果泄露。
    """
    elements: list[Dict[str, Any]] = []
    for item in items:
        if item.kind == "reasoning":
            body = (item.content or "").strip()
            title = item.title or "思考"
            if not body:
                continue
            head = " ".join(body.split())[:24]
            if len(" ".join(body.split())) > 24:
                head += "…"
            elements.append(
                {
                    "tag": "collapsible_panel",
                    "element_id": f"step_{len(elements)}",
                    "expanded": False,
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": f"💭 {title} · {head}",
                        },
                        "vertical_align": "center",
                    },
                    "border": {"color": "grey", "corner_radius": "8px"},
                    "padding": "4px 8px 4px 8px",
                    "elements": [_markdown_element(body)],
                }
            )
        elif item.kind == "tool":
            status = str(item.status or "running").lower()
            if status in {"completed", "success", "succeeded", "ok"}:
                mark, label = "✅", "已完成"
            elif status in {"failed", "error"}:
                mark, label = "❌", "失败"
            elif status in {"cancelled", "canceled"}:
                mark, label = "⏹️", "已取消"
            else:
                mark, label = "⏳", "执行中"
            elements.append(_markdown_element(f"{mark} **工具调用** · {label}"))
    return elements


def _degraded_card(limits: CardLimitsConfig) -> Dict[str, Any]:
    """构造橙色原生消息交接卡（D6）：明确告知等待原生消息，不截断伪装完整。"""
    degraded: Dict[str, Any] = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "内容较长"},
        },
        "body": {
            "elements": [
                _markdown_element("⏳ **卡片内容超出展示限制，请等待原生消息。**")
            ]
        },
    }
    try:
        enforce_card_limits(
            degraded,
            safe_bytes=limits.safe_bytes,
            max_elements=limits.max_elements,
            max_tables=limits.max_tables,
        )
        return degraded
    except CardLimitExceeded:
        # 极端自定义限额连降级卡也容不下时，返回同主题空元素最小卡（仍不抛异常）
        degraded["body"]["elements"] = []
        return degraded


def _degrade_disposition(status: DisplayStatus) -> str:
    """降级 disposition：终态超限 → native（直接原生交接），非终态 → deferred_native。"""
    return "native" if status == DisplayStatus.COMPLETED else "deferred_native"


def _safe_render_session(session: Any) -> Optional[CardSession]:
    """返回可供 renderer 读取的会话投影；损坏的展示字段按 fail-open 丢弃。

    事件层始终构造 CardSession，但公开 render API 也可能被第三方直接调用，
    因此这里不修改原会话，只对六类契约外输入作最小归一。
    """
    if not isinstance(session, CardSession):
        return None
    # 浅复制足够：renderer 只读这些字段，且容器会被新建，不污染会话状态。
    safe = CardSession(session.conversation_id, session.chat_id)
    safe.__dict__.update(session.__dict__)
    safe.thinking = session.thinking if isinstance(session.thinking, str) else ""
    safe.timeline = [
        item for item in (session.timeline or []) if isinstance(item, TimelineItem)
    ] if isinstance(session.timeline, (list, tuple)) else []
    safe.tools = {
        key: value for key, value in (session.tools or {}).items()
        if isinstance(value, ToolState)
    } if isinstance(session.tools, dict) else {}
    safe.interactions = {
        key: value for key, value in (session.interactions or {}).items()
        if isinstance(value, InteractionState)
    } if isinstance(session.interactions, dict) else {}
    safe.notices = list(session.notices) if isinstance(session.notices, (list, tuple)) else []
    return safe


# ---------------------------------------------------------------- T11: 按钮行
# 协议（T11_button_plan）：button value = {"hfc": 1, "action": "<命令或交互key>"}；
# 回调由宿主（fsapp 127.0.0.1:8898 POST /card/actions）白名单校验后回灌 handle_command。
# T27-E: 命令结果卡底部按钮 → 2 行 × 2 列（两个 action 容器，每容器 2 短按钮）。
# 注意：飞书 1.0 卡片 **column_set 内不允许 action 组件**（实测 200410
# "action components are not allowed in the column"），故 2×2 采用两个
# action 容器（每容器一行横排 2 按钮）+ **固定短文案**（杜绝长文案撑宽换行竖排）。
# 行1: 新会话 + 切换模型（固定短文案，当前模型名见 footer meta）；
# 行2: 设置（二级菜单卡）+ 状态（GA /status）。回调一律经宿主白名单校验后回灌。
_COMMAND_BUTTONS_ROW2: Tuple[Tuple[str, str], ...] = (
    ("/settings", "设置"),
    ("/status", "状态"),
)


def _button_element(text: str, action: str) -> Dict[str, Any]:
    """lark 卡片按钮元素（value 遵循 T11 协议：hfc=1 + action）。"""
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": str(text)},
        "type": "primary",
        "value": {"hfc": 1, "action": str(action)},
    }


def _command_buttons_element(current_model: Optional[str] = None) -> List[Dict[str, Any]]:
    """命令快捷按钮区（T27-G/2.0：#152 物理实测，飞书 1.0 action 容器内按钮
    每按钮占满整行竖排，无法 2×2；改 2.0 schema column_set + button 块元素 →
    2 行×2 列横排稳定，实测通过）。

    - 两行 column_set，每行两列（width=weighted, weight=1），每列一个 button 块；
    - 固定短文案（新会话/切换模型/设置/状态），/model 不注入模型名（防长名换行），
      当前模型名由卡片底部 meta 行承载；current_model 参数保留仅为签名兼容；
    - 2.0 卡片中 button 是块级元素，可直接放 column.elements（实测通过）。
    value 一律 T11 协议（hfc=1 + action），回调由宿主白名单校验后回灌。
    """
    rows = [
        [("新会话", "/new"), ("切换模型", "/model")],
        [(label, cmd) for cmd, label in _COMMAND_BUTTONS_ROW2],
    ]
    return [
        {
            "tag": "column_set",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [_button_element(label, cmd)],
                }
                for label, cmd in row
            ],
        }
        for row in rows
    ]



# ---------------------------------------------------------------- T27: 设置二级菜单卡
# /settings 按钮（命令结果卡行2）→ 宿主 fsapp 白名单校验后发送本菜单卡；
# 三个子按钮 value 均走 T11 协议，回调仍由宿主白名单校验（/goal_hive /help /about）。
# 本模块只渲染、不执行任何命令；goal hive 子项在宿主侧为入口提示文本卡（无执行面）。


def render_settings_menu_card() -> Dict[str, Any]:
    """设置二级菜单卡：goal hive 入口 / 帮助 / 关于（2.0 每按钮独占一行，竖排一列）。"""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "设置"},
        },
        "body": {
            "elements": [
                # T27-G7: 3 个按钮一行太挤看不清 → 每按钮一个 column_set 独占整行
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [_button_element(label, cmd)],
                        }
                    ],
                }
                for label, cmd in [
                    ("🎯 Goal Hive", "/goal_hive"),
                    ("📖 帮助", "/help"),
                    ("ℹ️ 关于", "/about"),
                ]
            ]
        },
    }


def render_plain_info_card(title: str, body: str) -> Dict[str, Any]:
    """通用信息文本卡（T27：goal hive 入口提示 / 关于 等本地构造内容）。

    与命令结果卡不同：无命令按钮行（避免无意义回环），正文经 _markdown_element
    转义，仅承载宿主传入的固定文案（无工具名/参数/结果泄露面）。
    """
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": html_escape_card_text(str(title))},
        },
        "body": {"elements": [_markdown_element(str(body))]},
    }


def _interaction_buttons_element(pending: Sequence[Any]) -> Dict[str, Any]:
    """pending 交互按钮行：每个交互一个按钮，value 携带交互 key（2.0 column_set 横排）。"""
    btns = [_button_element(it.id, it.id) for it in pending if getattr(it, "id", "")]
    return {
        "tag": "column_set",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [b],
            }
            for b in btns
        ],
    }


# ---------------------------------------------------------------- T16a: 模型选择卡
# /llm /llms 命令输出（GA 格式 "LLMs:\\n→ [0] name\\n  [1] name"，→ 标记当前模型）
# 渲染为模型选择卡：每个可用模型一个可点按钮，value 携带 model 名（白名单校验在宿主
# fsapp 侧 T16b：value.model ∈ GA list_llms() 才执行 /model <name>）。
_LLMS_HEADER = "LLMs:"
_LLMS_LINE_RE = re.compile(r"^[→\s]\s*\[(\d+)\]\s+(.+?)\s*$")


def _parse_llms_selector(text: str) -> Optional[tuple]:
    """解析模型列表文本 → (models, current_name)，非 LLMs 列表/空列表返回 None。"""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    if not lines or not lines[0].lstrip().startswith(_LLMS_HEADER):
        return None
    models: list = []
    current = None
    for ln in lines[1:]:
        m = _LLMS_LINE_RE.match(ln)
        if not m:
            continue
        name = m.group(2)
        is_cur = ln.lstrip().startswith("→")
        models.append((name, is_cur))
        if is_cur:
            current = name
    return (models, current) if models else None


def _model_selector_element(text: str) -> Optional[Dict[str, Any]]:
    """模型选择下拉（T27-A）：option.value 存渠道索引（list_llms 序号）。

    静态 value 只声明受控 /model 动作；飞书回调的 action.option.value 是用户选择，
    宿主侧按索引范围白名单校验（T27-G6）后回灌 /llms <n> 执行 next_llm 切换。
    """
    parsed = _parse_llms_selector(text)
    if parsed is None:
        return None
    models, current = parsed
    options = []
    current_idx = 0
    for i, (name, is_cur) in enumerate(models):
        options.append(
            {
                "text": {"tag": "plain_text", "content": str(name)},
                "value": str(i),
            }
        )
        if is_cur:
            current_idx = i
    if not options:
        return None
    return {
        "tag": "select_static",
        "placeholder": {"tag": "plain_text", "content": "选择模型"},
        # T27-G2: 2.0 卡 initial_option 必须是字符串（option.value）；
        # 传整个 option 对象 → 飞书 200621 parse card json err → 整卡发送失败回退文本。
        "initial_option": str(current_idx),
        "options": options,
        "value": {"hfc": 1, "action": "/model"},
    }


def _render_error_card() -> Dict[str, Any]:
    """公开 renderer 收到非会话对象时的固定、无敏感信息降级卡。"""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"template": "red", "title": {"tag": "plain_text", "content": "渲染失败"}},
        "body": {"elements": [_markdown_element("render_error: code=RC01")]},
    }


def render_card_result(
    session: CardSession,
    card_limits: Optional[CardLimitsConfig] = None,
) -> CardRenderResult:
    """渲染会话为卡片，返回卡片 JSON 与交付语义 disposition。永不抛异常（超限自动降级）。"""
    limits = card_limits or CardLimitsConfig()
    safe_session = _safe_render_session(session)
    if safe_session is None:
        return CardRenderResult(_render_error_card(), "card")
    session = safe_session
    status = resolve_display_status(session)
    title, template = _HEADER_BY_STATUS[status]

    header: Dict[str, Any] = {
        "template": template,
        "title": {"tag": "plain_text", "content": title},
    }
    # T5：运行中态加 subtitle（安全动作词优先，回退阶段文案）；终态无 subtitle 保持兼容。
    # 生产实测（T10，2026-08-09）：header.subtitle 仅接受 tag=lark_md；
    # tag=plain_text 会被飞书生产接口拒（ErrCode 11310 unsupported type of block）。
    if status in _ACTIVE_STAGE_TEXT:
        subtitle = (session.runtime_header_text or "").strip() or _ACTIVE_STAGE_TEXT[status]
        header["subtitle"] = {"tag": "lark_md", "content": subtitle}

    card: Dict[str, Any] = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": header,
        "body": {"elements": []},
    }

    # 1) 安全过程时间线 + 独立主文本。工具参数/结果仅存在 ToolState.detail，永不上卡。
    visible = normalize_stream_text(strip_think_tags(session.visible_text()))
    timeline_bytes = sum(
        len((item.title + item.content + item.status).encode("utf-8"))
        for item in session.timeline
    )
    # D6 预检：完整过程与正文超限时不静默截断，整体降级为原生消息交接卡。
    if timeline_bytes + len(visible.encode("utf-8")) > limits.safe_bytes:
        return CardRenderResult(_degraded_card(limits), _degrade_disposition(status))
    if session.timeline:
        card["body"]["elements"].extend(_timeline_elements(session.timeline))
    max_blocks = max(1, limits.max_elements - 8)  # 预留 header/时间线/交互/命令按钮/模型选择/状态位
    for block in split_markdown_blocks(visible, max_bytes=limits.safe_bytes // 2, max_blocks=max_blocks):
        card["body"]["elements"].append(_markdown_element(block))

    # 2) T16a: /llm /llms 模型列表文本 → 模型选择卡（每个非当前模型一个可点按钮）
    selector = _model_selector_element(visible)
    if selector is not None:
        card["body"]["elements"].append(selector)

    # 2) ToolState 仅供生命周期维护；用户可见时间线只读取安全名称和状态。

    # 3) 交互区（pending 交互 → 按钮行；T11 替换原纯文本占位）
    pending = [it for it in session.interactions.values() if it.status == "pending"]
    if pending:
        card["body"]["elements"].append(_markdown_element("🖱️ **等待用户操作…**"))
        card["body"]["elements"].append(_interaction_buttons_element(pending))

    # 4) 失败/通知区
    if session.status == "failed" and session.error_text:
        card["body"]["elements"].append(
            _markdown_element(f"❌ 错误：{html_escape_card_text(session.error_text)}")
        )
    for notice in session.notices[-3:]:
        card["body"]["elements"].append(_markdown_element(f"📢 {html_escape_card_text(notice)}"))

    # 5) T11：命令快捷按钮行（任何状态下可点；白名单动作由宿主回调校验）
    card["body"]["elements"].extend(_command_buttons_element())

    # 6) 底部单行元信息（⏱ 耗时 · 工具调用 N 次 · model · tokens；缺失段省略，无敏感内容）
    # T10：元素块不可用 tag=plain_text（生产拒 11310），改 markdown 块（与正文同款已实证）。
    meta = _footer_meta(session)
    if meta:
        card["body"]["elements"].append(_markdown_element(html_escape_card_text(meta)))

    # 6) 限额校验 + 超限降级
    try:
        enforce_card_limits(
            card,
            safe_bytes=limits.safe_bytes,
            max_elements=limits.max_elements,
            max_tables=limits.max_tables,
        )
        return CardRenderResult(card, "card")
    except CardLimitExceeded:
        return CardRenderResult(_degraded_card(limits), _degrade_disposition(status))


def render_card(
    session: CardSession,
    card_limits: Optional[CardLimitsConfig] = None,
) -> Dict[str, Any]:
    """渲染卡片 JSON（兼容旧接口；降级语义请用 render_card_result）。永不抛异常。"""
    return render_card_result(session, card_limits).card

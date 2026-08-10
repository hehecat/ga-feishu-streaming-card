"""命令结果卡：slash 命令 → 飞书命令结果卡（独立实现）。

职责边界：
- 本模块只做「结果卡化」：不解析、不执行任何命令。content 由宿主命令执行层
  给出（GA 侧 chatapp_common/fsapp 生成），本模块只负责标题/模板映射与安全渲染。
- 渲染永不抛异常：任何异常（含 CardLimitExceeded 超限）都降级为固定安全卡
  （render_error 语义，无命令/工具敏感信息）。
- 所有文本经 HTML 转义：标题用 html_escape_card_text，正文经 _markdown_element
  （转义尖括号），杜绝注入与哨兵泄露。

命令 → 标题/模板映射表与替换语义参考 hermes-feishu-streaming-card v4.2.8
(MIT, hermes_feishu_card/hook_runtime.py 3645/3830/3843/4247)：
- 替换语义：保留映射表命中项；未命中 → 命令原文（标题）+ green（模板）。
- /llm 仅在「有参数（执行切换）」时映射为「模型已切换」；无参列表态回退默认。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .config import CardLimitsConfig
from .limits import enforce_card_limits
from .render import (
    _command_buttons_element,
    _markdown_element,
    _model_selector_element,
    _render_error_card,
    html_escape_card_text,
)
from .text import split_markdown_blocks

#: 未命中映射表时使用的默认 header 模板（源项目「其余 green」语义）。
DEFAULT_TEMPLATE = "green"

#: 命令 → 结果卡标题映射表（源映射表语义，hook_runtime.py:3830）。
TITLE_BY_COMMAND: Dict[str, str] = {
    "/new": "会话已重置",
    "/reset": "会话已重置",
    "/undo": "已撤销上一步",
    "/clear": "上下文已清理",
    "/stop": "已停止",
    "/llm": "模型已切换",
    "/model": "模型已切换",
    "/llms": "模型列表",
}

#: 命令 → 结果卡 header 模板映射表（源映射表语义，hook_runtime.py:3843）。
TEMPLATE_BY_COMMAND: Dict[str, str] = {
    "/new": "green",
    "/reset": "green",
    "/undo": "orange",
    "/clear": "green",
    "/llm": "green",
    "/model": "green",
    "/llms": "green",
}


def _command_name(command: str) -> str:
    """取命令首个 token 并小写；空/非命令 → ''。"""
    if not command:
        return ""
    return str(command).strip().split()[0].lower()


def title_for(command: str, *, has_arg: bool = False) -> str:
    """命令 → 卡片标题。未命中 → 命令原文（默认模板语义）。

    /llm 仅在有参数（切换）时映射为「模型已切换」；无参列表态回退默认（'/llm'）。
    """
    name = _command_name(command)
    if name == "/llm" and not has_arg:
        return name
    return TITLE_BY_COMMAND.get(name, name or "/")


def template_for(command: str, *, has_arg: bool = False) -> str:
    """命令 → header 模板；未命中 → DEFAULT_TEMPLATE（green）。"""
    name = _command_name(command)
    if name == "/llm" and not has_arg:
        return DEFAULT_TEMPLATE
    return TEMPLATE_BY_COMMAND.get(name, DEFAULT_TEMPLATE)


def render_command_result_card(
    content: str,
    title: str,
    template: str = DEFAULT_TEMPLATE,
    limits: Optional[CardLimitsConfig] = None,
    current_model: Optional[str] = None,
) -> Dict[str, Any]:
    """把命令结果文本渲染为命令结果卡 JSON。永不抛异常。

    - 标题经 html_escape_card_text 转义，正文按 safe_bytes / max_elements 预算
      分块（复用 text.split_markdown_blocks，预留 6 个元素给 header/两行按钮）；
    - current_model（T27）：命令按钮行「切换模型」按钮文案动态显示当前模型名，
      由宿主 fsapp._reply 注入（self.agent.get_llm_name()），None 时回退静态文案；
    - 渲染后按 CardLimitsConfig 校验（enforce_card_limits），任一超限 → 降级
      固定安全卡（_render_error_card）；
    - 任何异常路径同样降级，保证调用方（bridge/宿主）拿到的一定是合法卡片。
    """
    try:
        cfg_limits = limits or CardLimitsConfig()
        safe_title = html_escape_card_text(str(title or ""))
        max_blocks = max(1, cfg_limits.max_elements - 6)  # 预留 header/两行按钮
        blocks = split_markdown_blocks(
            str(content or ""),
            max_bytes=max(1, cfg_limits.safe_bytes),
            max_blocks=max_blocks,
        )
        # T16a: /llm /llms 模型列表文本 → 模型选择卡（每个非当前模型一个可点按钮）
        selector = _model_selector_element(str(content or ""))
        # T27-G3: 有下拉时正文不再重复列表文本（用户反馈冗余），只保留一行提示；
        # 其余命令（无下拉）保持原正文渲染。
        body_elements = (
            [_markdown_element("请选择要切换的模型 ↓")] + [selector]
            if selector is not None
            else [_markdown_element(b) for b in blocks]
        )
        card: Dict[str, Any] = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "template": str(template or DEFAULT_TEMPLATE),
                "title": {"tag": "plain_text", "content": safe_title},
            },
            "body": {"elements": (
                body_elements
                # T27-G: 2×2 四按钮（两个 column_set，每行两列各 1 个 button 块）
                + list(_command_buttons_element(current_model))
            )},
        }
        enforce_card_limits(
            card,
            safe_bytes=cfg_limits.safe_bytes,
            max_elements=cfg_limits.max_elements,
            max_tables=cfg_limits.max_tables,
        )
        return card
    except Exception:
        return _render_error_card()

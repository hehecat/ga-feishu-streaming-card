"""会话模型（引擎核心，独立实现）。

设计约定：
- CardSession.status ∈ {thinking, completed, failed}；展示态由 status.py 派生。
- ToolState(id, name, status ∈ {running, completed, failed, cancelled, canceled,
  pending}, detail, duration_ms)：同 id 原地更新；**终态后不再改**。
- InteractionState(status ∈ {pending, completed, failed})。
- apply_event(ev) 按事件类型做状态迁移（thinking/answer 文本累积、工具
  原地更新、终态固化、交互流转）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .events import CardEvent, EventType

#: 工具终态集合：到达后该工具不再接受状态/详情更新。
TOOL_TERMINAL = frozenset({"completed", "failed", "cancelled", "canceled"})
TOOL_ACTIVE = frozenset({"running", "pending"})

INTERACTION_TERMINAL = frozenset({"completed", "failed"})

# ---------------------------------------------------------------- 运行中安全动作词（T5）
# 卡片 header subtitle 只显示白名单动作词；工具名/参数/路径等敏感信息绝不展示。
# 未知工具名 → 空串，render 回退为系统阶段文案。
_ACTION_WORD_RULES: tuple = (
    ("正在搜索", ("search", "google", "bing", "baidu", "duckduckgo", "web_search", "query", "搜索", "检索")),
    ("正在浏览", ("browse", "open", "goto", "visit", "http", "url", "浏览", "打开", "导航")),
    ("正在读取", ("read", "cat", "view", "查看", "读取", "内容")),
    ("正在编辑", ("edit", "write", "patch", "sed", "insert", "append", "编辑", "写入", "修改", "删除")),
    ("正在执行操作", ("run", "exec", "shell", "bash", "python", "cmd", "code", "执行", "运行", "操作", "调用")),
)
RUNTIME_HEADER_MAX_CHARS = 120  # 动作词截断上限


def action_word_for_tool(name: str) -> str:
    """按工具名关键字映射到安全动作词；未知工具返回空串（render 回退阶段文案）。"""
    name = (name or "").strip().lower()
    if not name:
        return ""
    for word, keys in _ACTION_WORD_RULES:
        if any(k in name for k in keys):
            return word
    return ""


# ---------------------------------------------------------------- 资源上限（防超大输入 DoS）
# 正常量级文本/工具远低于上限，不影响既有行为；超限即截断/忽略，保证内存有界。
MAX_TEXT_CHARS = 200_000  # thinking / answer 各自累计上限
MAX_TOOLS = 300  # 工具条目上限（超限后新工具忽略）
MAX_NOTICES = 50  # 通知条数上限
MAX_NOTICE_CHARS = 4_000  # 单条通知文本上限
MAX_INTERACTIONS = 100  # 交互条目上限
TOOL_NAME_MAX = 200
TOOL_DETAIL_MAX = 2_000
ERROR_TEXT_MAX = 4_000
_TRUNC_MARK = "…[truncated]"

# ---------------------------------------------------------------- completed 短后缀保护（D4）
# 长流式答案 + 短完成后缀：合并保留（答案 + 分隔线 + 后缀），避免后缀覆盖实质答案。
MIN_PRESERVED_STREAMED_ANSWER_CHARS = 64
MAX_SHORT_COMPLETION_POSTSCRIPT_CHARS = 240
MIN_STREAMED_ANSWER_TO_POSTSCRIPT_RATIO = 3


def _as_str(v: Any) -> str:
    """任意值转字符串（防御错类型帧：data['text'] 为 int/list 等时不炸）。"""
    return v if isinstance(v, str) else str(v)


def _as_text(v: Any) -> str:
    """文本字段取值：None → ''；非 str 转字符串（防御错类型帧）。"""
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def _clip_text(s: str, limit: int) -> str:
    """按字符上限截断（带标记）。limit 必须 >= len(_TRUNC_MARK)+1。"""
    if len(s) <= limit:
        return s
    return s[: max(0, limit - len(_TRUNC_MARK))] + _TRUNC_MARK


def _merge_completed_answer(preface: str, final: str) -> str:
    """completed 短后缀保护：长答案 + 短后缀时合并保留，否则 final 覆盖。

    规则（与上游行为规格一致）：
    - preface 为空或 final == preface：以 final 为准（去重）；
    - preface 长度 >= MIN_PRESERVED_STREAMED_ANSWER_CHARS(64) 且
      0 < final 长度 <= MAX_SHORT_COMPLETION_POSTSCRIPT_CHARS(240) 且
      preface 长度 >= final 长度 * MIN_STREAMED_ANSWER_TO_POSTSCRIPT_RATIO(3)：
      合并为 ``preface + "\\n\\n---\\n\\n" + final``；
    - 其余情况：final 覆盖（现状）。
    """
    preface = (preface or "").strip()
    final = (final or "").strip()
    if not preface or final == preface:
        return final or preface
    plen, flen = len(preface), len(final)
    if (
        plen >= MIN_PRESERVED_STREAMED_ANSWER_CHARS
        and 0 < flen <= MAX_SHORT_COMPLETION_POSTSCRIPT_CHARS
        and plen >= flen * MIN_STREAMED_ANSWER_TO_POSTSCRIPT_RATIO
    ):
        return f"{preface}\n\n---\n\n{final}"
    return final


@dataclass
class ToolState:
    """单个工具运行状态。id 为主键；终态后不可再更新。"""

    id: str
    name: str = ""
    status: str = "pending"
    detail: str = ""
    duration_ms: Optional[int] = None

    def is_terminal(self) -> bool:
        return self.status in TOOL_TERMINAL


@dataclass
class InteractionState:
    """用户交互（回传操作）状态。"""

    id: str
    status: str = "pending"
    token: Optional[str] = None


@dataclass
class TimelineItem:
    """用户可见的安全过程项；工具详情永不进入该结构。"""

    kind: str
    title: str
    status: str = ""
    content: str = ""
    tool_id: str = ""


@dataclass
class CardSession:
    """一次对话的会话状态。conversation_id + chat_id 定位；消息体为流式累积。"""

    conversation_id: str
    chat_id: str
    status: str = "thinking"
    platform: str = "feishu"
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    thinking: str = ""
    answer: str = ""
    timeline: List[TimelineItem] = field(default_factory=list)
    tools: Dict[str, ToolState] = field(default_factory=dict)
    interactions: Dict[str, InteractionState] = field(default_factory=dict)
    notices: List[str] = field(default_factory=list)
    native_disposition: str = ""  # 终态原生交接（默认空=未决定）
    display_status: str = ""  # 事件显式携带的展示态（合法值才写入；派生优先时为空）
    error_text: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    #: 已收到并应用的最近 sequence（engine 防乱序用；会话侧记录供调试）
    last_sequence: int = -1
    #: 累计 token 用量（input/output；来自 llm_after usage，缺失→空 dict）
    tokens: Dict[str, int] = field(default_factory=dict)
    #: 上下文窗口用量（used_tokens/max_tokens；来自 usage 的 total/max，缺失→空 dict；
    #: max_tokens 取历史最大值，footer 渲染 "ctx used/max %"，无敏感内容）
    context: Dict[str, int] = field(default_factory=dict)
    #: 运行中安全动作词（白名单映射，终态清空；render header subtitle 用）
    runtime_header_text: str = ""
    #: 最近 llm_after 的模型名（footer 元信息展示，无敏感内容）
    model: str = ""

    def touch(self) -> None:
        self.updated_at = time.time()

    def visible_text(self) -> str:
        """展示文本：answer 出现后接管，否则显示 thinking。"""
        return self.answer if self.answer else self.thinking

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 化的 dict（调试/持久化/渲染用）。"""
        return {
            "conversation_id": self.conversation_id,
            "chat_id": self.chat_id,
            "status": self.status,
            "platform": self.platform,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "thinking": self.thinking,
            "answer": self.answer,
            "timeline": [
                {
                    "kind": item.kind,
                    "title": item.title,
                    "status": item.status,
                    "content": item.content,
                    "tool_id": item.tool_id,
                }
                for item in self.timeline
            ],
            "tools": {
                k: {
                    "id": t.id,
                    "name": t.name,
                    "status": t.status,
                    "detail": t.detail,
                    "duration_ms": t.duration_ms,
                }
                for k, t in self.tools.items()
            },
            "interactions": {
                k: {"id": i.id, "status": i.status, "token": i.token}
                for k, i in self.interactions.items()
            },
            "notices": list(self.notices),
            "native_disposition": self.native_disposition,
            "display_status": self.display_status,
            "error_text": self.error_text,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "last_sequence": self.last_sequence,
            "tokens": dict(self.tokens),
            "context": dict(self.context),
            "runtime_header_text": self.runtime_header_text,
            "model": self.model,
        }

    def _add_reasoning(self, content: str) -> None:
        content = _clip_text(content.strip(), MAX_TEXT_CHARS)
        if not content:
            return
        self.timeline.append(
            TimelineItem(
                kind="reasoning",
                title=f"思考 {sum(i.kind == 'reasoning' for i in self.timeline) + 1}",
                content=content,
            )
        )

    def _append_answer_text(self, text: str) -> None:
        """接收完整 llm_after 文本；前置 summary 归档，余文保留为用户正文。"""
        stripped = text.lstrip()
        if stripped.startswith("<summary>"):
            end = stripped.find("</summary>", len("<summary>"))
            if end >= 0:
                self._add_reasoning(stripped[len("<summary>") : end])
                text = stripped[end + len("</summary>") :].lstrip()
        if text:
            self.answer = _clip_text(self.answer + text, MAX_TEXT_CHARS)

    def _accum_tokens(self, usage: Any) -> None:
        """从事件载荷 usage（缺失→None 忽略）累计 token；prompt/completion 兼容归一。"""
        if not isinstance(usage, dict):
            return
        inp = usage.get("input_tokens", usage.get("prompt_tokens"))
        out = usage.get("output_tokens", usage.get("completion_tokens"))
        if inp is not None:
            try:
                self.tokens["input_tokens"] = self.tokens.get("input_tokens", 0) + max(0, int(inp))
            except (TypeError, ValueError):
                pass
        if out is not None:
            try:
                self.tokens["output_tokens"] = self.tokens.get("output_tokens", 0) + max(0, int(out))
            except (TypeError, ValueError):
                pass
        # context 窗口用量（T14：与上游 footer "ctx used/max %" 对齐）
        # used_tokens 优先取 total_tokens，缺失时退化为 in+out；max_tokens 取历史最大值。
        total = usage.get("total_tokens")
        if total is None and inp is not None and out is not None:
            total = max(0, int(inp) if str(inp).isdigit() else 0) + max(0, int(out) if str(out).isdigit() else 0)
        if total is not None:
            try:
                self.context["used_tokens"] = self.context.get("used_tokens", 0) + max(0, int(total))
            except (TypeError, ValueError):
                pass
        mx = usage.get("max_tokens")
        if mx is not None:
            try:
                self.context["max_tokens"] = max(self.context.get("max_tokens", 0), max(0, int(mx)))
            except (TypeError, ValueError):
                pass

    def _upsert_tool(self, data: Dict[str, Any]) -> None:
        tid = data.get("id") or data.get("tool_id") or data.get("name")
        if not tid:
            return
        tid = _as_str(tid)
        if len(tid) > TOOL_NAME_MAX:
            tid = tid[:TOOL_NAME_MAX]
        existing = self.tools.get(tid)
        if existing is not None and existing.is_terminal():
            # 终态后不再改
            return
        if existing is None:
            if len(self.tools) >= MAX_TOOLS:
                return  # 工具条目已满：忽略新工具（防无界增长）
            # 不归档已有 answer：正文是主区，任何过程事件都不得吞掉它
            existing = ToolState(id=tid)
            self.tools[tid] = existing
            self.timeline.append(
                TimelineItem(kind="tool", title="工具", status="running", tool_id=tid)
            )
        if data.get("name") is not None:
            existing.name = _clip_text(_as_str(data["name"]), TOOL_NAME_MAX)
        elif data.get("tool_name") is not None:
            # bridge 载荷用 tool_name 键（与 id/tool_id 双键兼容一致）
            existing.name = _clip_text(_as_str(data["tool_name"]), TOOL_NAME_MAX)
        if data.get("status") in TOOL_ACTIVE | TOOL_TERMINAL:
            existing.status = _as_str(data["status"])
        if data.get("detail") is not None:
            existing.detail = _clip_text(_as_str(data["detail"]), TOOL_DETAIL_MAX)
        if data.get("duration_ms") is not None:
            try:
                existing.duration_ms = int(data["duration_ms"])
            except (TypeError, ValueError):
                existing.duration_ms = None
        for item in self.timeline:
            if item.kind == "tool" and item.tool_id == tid:
                # 生产验收标准：卡片不含工具名/参数哨兵，仅通用文案+状态。
                item.title = "工具调用"
                item.status = existing.status
                break
        # 运行中安全动作词：active → 白名单词；终态 → 清空（下个工具再设）
        if existing.status in TOOL_TERMINAL:
            self.runtime_header_text = ""
        elif existing.status in TOOL_ACTIVE:
            word = action_word_for_tool(existing.name)
            self.runtime_header_text = word[:RUNTIME_HEADER_MAX_CHARS] if word else ""

    def apply_event(self, ev: CardEvent) -> None:
        """应用事件做状态迁移（结构已由 parse_event 校验；字段类型防御见 _as_str/_clip_text）。"""
        d = ev.data or {}
        # 显式展示态（D1）：事件携带合法 display_status 时写入；派生规则见 status.py
        _explicit = _as_text(d.get("display_status")).strip().lower()
        if _explicit in {"thinking", "in_progress", "waiting", "running", "completed", "failed"}:
            self.display_status = _explicit
        if ev.type is EventType.MESSAGE_STARTED:
            self.message_id = d.get("message_id") or ev.message_id or self.message_id
            if ev.thread_id:
                self.thread_id = ev.thread_id
            if ev.turn_id:
                self.turn_id = ev.turn_id
        elif ev.type is EventType.THINKING_DELTA:
            if self.status not in ("completed", "failed"):
                self.thinking = _clip_text(self.thinking + _as_text(d.get("text")), MAX_TEXT_CHARS)
        elif ev.type is EventType.TOOL_UPDATED:
            self._upsert_tool(d)
        elif ev.type is EventType.ANSWER_DELTA:
            if self.status not in ("completed", "failed"):
                self._append_answer_text(_as_text(d.get("text")))
                self._accum_tokens(d.get("usage"))
                summary = d.get("summary")
                if isinstance(summary, dict) and summary.get("model"):
                    self.model = _as_str(summary["model"])[:200]
        elif ev.type is EventType.MESSAGE_COMPLETED:
            self.status = "completed"
            final_text = d.get("final_text")
            if final_text is not None:
                # D4 短后缀保护：长流式答案 + 短后缀 → 合并保留（防覆盖）
                self.answer = _clip_text(_merge_completed_answer(self.answer, _as_str(final_text)), MAX_TEXT_CHARS)
            self.completed_at = time.time()
            self.native_disposition = d.get("native_disposition", self.native_disposition) or ""
            self.runtime_header_text = ""  # 终态清除运行中动作词（tokens 保留供 footer）
        elif ev.type is EventType.MESSAGE_FAILED:
            self.status = "failed"
            self.error_text = _clip_text(_as_text(d.get("reason")), ERROR_TEXT_MAX)
            self.completed_at = time.time()
            self.runtime_header_text = ""
        elif ev.type is EventType.SYSTEM_NOTICE:
            if d.get("text") is not None:
                if len(self.notices) >= MAX_NOTICES:
                    return  # 通知条数已达上限：忽略（防无界增长）
                self.notices.append(_clip_text(_as_str(d["text"]), MAX_NOTICE_CHARS))
        elif ev.type is EventType.INTERACTION_REQUESTED:
            iid = d.get("id") or ev.conversation_id
            if iid:
                if iid not in self.interactions and len(self.interactions) >= MAX_INTERACTIONS:
                    return  # 交互条目已达上限：忽略新交互（防无界增长）
                self.interactions[iid] = InteractionState(
                    id=_as_str(iid), status="pending", token=d.get("token")
                )
        elif ev.type is EventType.INTERACTION_COMPLETED:
            iid = d.get("id") or ev.conversation_id
            st = self.interactions.get(iid)
            if st is not None:
                st.status = "completed"
        elif ev.type is EventType.INTERACTION_FAILED:
            iid = d.get("id") or ev.conversation_id
            st = self.interactions.get(iid)
            if st is not None:
                st.status = "failed"
        self.last_sequence = ev.sequence
        self.touch()

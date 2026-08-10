"""GA → 协议事件桥（独立实现）。

设计约定：
- ``map_ga_ctx(ctx: dict) -> list[CardEvent]``：把 GA 8 事件 locals() 快照映射为协议事件流。
- ``emit_from_ga_locals_threadsafe(ctx, cfg) -> None``：同步调用、内部线程+队列桥、绝不抛异常
  （fail-open：任何异常仅记 stderr + metrics，不向上传播）。

映射规则：

    agent_before -> message.started
    tool_before  -> tool.updated  (status=running)
    tool_after   -> tool.updated  (终态 completed/failed + detail)
    llm_after    -> answer.delta  (完整文本) + turn 汇总（放 data.summary）
    turn_after   -> system.notice （turn 汇总；终态由 agent_after 发出，避免 answer.delta 重复）
    agent_after  -> message.completed / message.failed（按 exit_reason）

事件名来源：GA 的 turn_after 与 agent_after 处于同一函数作用域，``locals()`` 快照无法区分，
因此插件回调必须在复制后的 ctx 上注入 ``_hfc_event``（见 bridge/hfc_bridge.py）；
缺失时 ``map_ga_ctx`` 做尽力推断（best-effort）。

chat_id 来源优先级（§2）：ctx 注入键 > env HFC_CHAT_ID > handler 会话映射 > 确定性 fallback。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .events import CardEvent, EventType

__all__ = [
    "map_ga_ctx",
    "emit_from_ga_locals_threadsafe",
    "register_session_chat",
    "resolve_chat_id",
    "shutdown",
    "reset_for_test",
    "metrics",
]

# GA 全部埋点事件名（agent_loop.py:22/24/49/57/58/67/102/105）
GA_EVENTS: Tuple[str, ...] = (
    "agent_before",
    "tool_before",
    "tool_after",
    "turn_before",
    "llm_before",
    "llm_after",
    "turn_after",
    "agent_after",
)

# 模块级单调序列（跨事件流全局递增；engine 按会话做防乱序）
_seq = itertools.count(1)
_seq_lock = threading.Lock()

# handler 会话映射（优先级 3）：session_key -> chat_id
_CHAT_BY_SESSION: Dict[str, str] = {}
_CHAT_LOCK = threading.Lock()

# 工具开始时间：tool_before 记录，tool_after 据此算 duration_ms
_TOOL_STARTED: Dict[str, float] = {}
_TOOL_LOCK = threading.Lock()

_metrics: Dict[str, int] = {
    "emitted": 0,
    "handled": 0,
    "dropped": 0,
    "errors": 0,
    "coalesced": 0,
}
_metrics_lock = threading.Lock()


def _bump(key: str, n: int = 1) -> None:
    with _metrics_lock:
        _metrics[key] = _metrics.get(key, 0) + n


def metrics() -> Dict[str, int]:
    with _metrics_lock:
        return dict(_metrics)


# ---------------------------------------------------------------- 工具函数

def _safe_str(v: Any, limit: int = 400) -> str:
    """任意值转字符串并截断（防大对象进事件 data）。"""
    s = str(v)
    if len(s) > limit:
        s = s[:limit] + f"...<truncated {len(s) - limit}>"
    return s


def _safe_int(v: Any) -> Optional[int]:
    """安全转 int：None/非数字/布尔 → None（防畸形帧 ValueError 炸映射）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _usage_payload(raw_usage: Any) -> Optional[Dict[str, Any]]:
    """llm_after usage → 协议载荷（T5 tokens 累计用；缺失→None，session 侧忽略）。

    dict 原样透传（session 归一 prompt_tokens/completion_tokens 别名）；
    pydantic 等对象取 input/output 或 prompt/completion 常见属性。
    """
    if raw_usage is None:
        return None
    if isinstance(raw_usage, dict):
        return raw_usage or None

    def _first(*names: str) -> Any:
        for n in names:
            v = getattr(raw_usage, n, None)
            if v is not None:
                return v
        return None

    payload: Dict[str, Any] = {}
    inp = _first("input_tokens", "prompt_tokens")
    out = _first("output_tokens", "completion_tokens")
    if inp is not None:
        payload["input_tokens"] = inp
    if out is not None:
        payload["output_tokens"] = out
    return payload or None


def _sanitize_args(args: Any, limit: int = 80, max_keys: int = 8) -> Dict[str, Any]:
    """工具参数清洗：剔除 GA 注入键，值截断，防不可序列化对象。"""
    if not isinstance(args, dict):
        return {"_raw": _safe_str(args, limit)}
    out: Dict[str, Any] = {}
    for k, v in args.items():
        if k in ("_index", "_tool_num"):
            continue
        out[str(k)] = _safe_str(v, limit)
        if len(out) >= max_keys:
            out["_more"] = f"+{len(args) - len(out)} keys"
            break
    return out


def _session_key(ctx: Dict[str, Any]) -> str:
    """会话键：优先 handler.parent.task_dir（GA 运行会话目录）。"""
    # agent_runner_loop 的 locals 键为 handler；GenericAgentHandler.dispatch
    # 的 tool_before/tool_after locals 键只有 self（见 GA agent_loop.py）。
    # 两者必须归入同一会话，否则工具卡片会更新到 fallback message_id。
    h = ctx.get("handler") or ctx.get("self")
    if h is not None:
        parent = getattr(h, "parent", None)
        td = getattr(parent, "task_dir", None) or getattr(h, "task_dir", None)
        if td:
            return str(td)
    td = ctx.get("task_dir")
    if td:
        return str(td)
    return ctx.get("conversation_id") or ctx.get("session_id") or "default"


def resolve_chat_id(ctx: Dict[str, Any], session_key: Optional[str] = None) -> str:
    """按 §2 优先级解析 chat_id（不抛异常，最终必有确定性 fallback）。"""
    # 1) ctx 注入键
    for k in ("chat_id", "hfc_chat_id", "HFC_CHAT_ID"):
        v = ctx.get(k)
        if v:
            return str(v)
    # 2) env
    env = os.environ.get("HFC_CHAT_ID")
    if env:
        return env
    # 3) handler 会话映射（register_session_chat 登记）。task-split 的
    # conversation 是 `<base-session>:<task-id>`，入站目标仍登记在 base-session。
    sk = session_key or _session_key(ctx)
    keys = (sk, sk.split(":", 1)[0]) if ":" in sk else (sk,)
    with _CHAT_LOCK:
        mapped = next((_CHAT_BY_SESSION[k] for k in keys if k in _CHAT_BY_SESSION), None)
    if mapped:
        return mapped
    # 4) 确定性 fallback：同一会话键 → 同一 chat_id
    digest = hashlib.sha1(sk.encode("utf-8")).hexdigest()[:12]
    return f"chat_{digest}"


def register_session_chat(session_key: str, chat_id: str) -> None:
    """登记 handler 会话映射（chat_id 优先级 3）。供 hfc_bridge 按 .hfc_config.json 注入。

    映射表有上限（防 GA 会话无限增长导致内存无界）：超限时淘汰最旧登记项。
    """
    with _CHAT_LOCK:
        if len(_CHAT_BY_SESSION) >= 4096:
            try:
                _CHAT_BY_SESSION.pop(next(iter(_CHAT_BY_SESSION)))
            except StopIteration:
                pass
        _CHAT_BY_SESSION[str(session_key)] = str(chat_id)


def _next_seq() -> int:
    with _seq_lock:
        return next(_seq)


def _common(ctx: Dict[str, Any]) -> Dict[str, Any]:
    # 宿主插件可显式划分一次用户任务的卡片会话；未注入时保持原推导行为。
    session_key = str(ctx.get("_hfc_conversation_id") or _session_key(ctx))
    turn = ctx.get("turn")
    turn_id = f"{session_key}#t{turn}" if turn else None
    return {
        "conversation_id": session_key,
        "chat_id": ctx.get("_hfc_chat_id") or resolve_chat_id(ctx, session_key),
        "turn_id": turn_id,
        "sequence": _next_seq(),
        "created_at": time.time(),
    }


def _agent_name(ctx: Dict[str, Any]) -> str:
    h = ctx.get("handler")
    if h is None:
        return ""
    name = getattr(h, "name", None)
    return str(name) if name else type(h).__name__


# ---------------------------------------------------------------- 映射

def _infer_event(ctx: Dict[str, Any]) -> str:
    """尽力推断事件名（_hfc_event 缺失时）。agent_after 与 turn_after 同作用域无法区分，
    推断结果只作兜底，插件路径总是显式注入。"""
    if "tool_name" in ctx:
        return "tool_after" if "ret" in ctx else "tool_before"
    if "exit_reason" in ctx:
        return "agent_after"
    if "response" in ctx:
        return "llm_after" if "next_prompt" not in ctx else "turn_after"
    if "handler" in ctx and "user_input" in ctx and "max_turns" in ctx:
        return "agent_before"
    return "agent_before"


def map_ga_ctx(ctx: Dict[str, Any]) -> List[CardEvent]:
    """把 GA 事件 locals() 快照映射为协议事件流（纯函数化，除序列号外无副作用）。"""
    if not isinstance(ctx, dict):
        return []
    event = ctx.get("_hfc_event") or _infer_event(ctx)
    common = _common(ctx)
    turn = ctx.get("turn")

    if event == "agent_before":
        return [
            CardEvent(
                type=EventType.MESSAGE_STARTED,
                data={
                    "event": "agent_before",
                    "agent": _agent_name(ctx),
                    "max_turns": ctx.get("max_turns"),
                    "input_preview": _safe_str(ctx.get("user_input"), 200),
                },
                **common,
            )
        ]

    if event == "tool_before":
        name = _safe_str(ctx.get("tool_name", ""), 100)
        index = _safe_int(ctx.get("index")) or 0
        tool_id = _safe_str(
            ctx.get("tool_id") or ctx.get("tid") or ctx.get("id") or f"tool_{name}_{index}", 200
        )
        with _TOOL_LOCK:
            # 上限：淘汰最旧登记（防会话无限增长导致内存无界）
            if len(_TOOL_STARTED) >= 4096:
                try:
                    _TOOL_STARTED.pop(next(iter(_TOOL_STARTED)))
                except StopIteration:
                    pass
            _TOOL_STARTED[tool_id] = time.monotonic()
        return [
            CardEvent(
                type=EventType.TOOL_UPDATED,
                data={
                    "event": "tool_before",
                    "tool_id": tool_id,
                    "tool_name": name,
                    "status": "running",
                    "args": _sanitize_args(ctx.get("args", {})),
                    "index": index,
                    "tool_num": _safe_int(ctx.get("tool_num")) or 1,
                },
                **common,
            )
        ]

    if event == "tool_after":
        name = _safe_str(ctx.get("tool_name", ""), 100)
        index = _safe_int(ctx.get("index")) or 0
        tool_id = _safe_str(
            ctx.get("tool_id") or ctx.get("tid") or ctx.get("id") or f"tool_{name}_{index}", 200
        )
        ret = ctx.get("ret")
        error = ctx.get("error")
        failed = error is not None or isinstance(ret, BaseException)
        detail = _safe_str(error if error is not None else ret, 600)
        duration_ms = _safe_int(ctx.get("duration_ms"))
        with _TOOL_LOCK:
            t0 = _TOOL_STARTED.pop(tool_id, None)
        if duration_ms is None and t0 is not None:
            duration_ms = int((time.monotonic() - t0) * 1000)
        return [
            CardEvent(
                type=EventType.TOOL_UPDATED,
                data={
                    "event": "tool_after",
                    "tool_id": tool_id,
                    "tool_name": name,
                    "status": "failed" if failed else "completed",
                    "detail": detail,
                    "duration_ms": duration_ms,
                },
                **common,
            )
        ]

    if event == "llm_after":
        resp = ctx.get("response")
        text = _safe_str(getattr(resp, "content", "") or "", _LLM_TEXT_MAX)
        tool_calls = getattr(resp, "tool_calls", None) or []
        return [
            CardEvent(
                type=EventType.ANSWER_DELTA,
                data={
                    "event": "llm_after",
                    "text": text,
                    "turn": turn,
                    "usage": _usage_payload(getattr(resp, "usage", None)),  # T5: tokens 累计
                    "summary": {
                        "tool_calls": len(tool_calls),
                        "model": _safe_str(getattr(resp, "model", None) or "", 50),
                    },
                },
                **common,
            )
        ]

    if event == "turn_after":
        exit_reason = ctx.get("exit_reason")
        data: Dict[str, Any] = {
            "event": "turn_after",
            "turn": turn,
            "status": "exit" if exit_reason else "continue",
            "tool_calls": len(ctx.get("tool_calls") or []),
            "tool_results": len(ctx.get("tool_results") or []),
            "next_prompt_preview": _safe_str(ctx.get("next_prompt"), 200),
        }
        if exit_reason:
            data["exit_reason"] = _safe_str(exit_reason, 200)
        return [CardEvent(type=EventType.SYSTEM_NOTICE, data=data, **common)]

    if event == "agent_after":
        er = ctx.get("exit_reason") or {}
        result = er.get("result") if isinstance(er, dict) else str(er)
        # while 循环耗尽（未 break）→ MAX_TURNS_EXCEEDED（返回值在 hook 之后，此处由 turn/max_turns 推断）
        turn_i = _safe_int(turn)
        max_turns_i = _safe_int(ctx.get("max_turns"))
        exhausted = (
            not er
            and turn_i is not None
            and max_turns_i is not None
            and turn_i >= max_turns_i
        )
        normalized_result = _safe_str(result, 100).strip().lower()
        completed = normalized_result in ("current_task_done", "exited", "completed") or (not er and not exhausted)
        data: Dict[str, Any] = {
            "event": "agent_after",
            "exit_reason": _safe_str(er, 200),
            "turns_total": turn,
        }
        if completed:
            return [CardEvent(type=EventType.MESSAGE_COMPLETED, data=data, **common)]
        data["reason"] = f"exit_reason={result or 'MAX_TURNS_EXCEEDED'}"
        return [CardEvent(type=EventType.MESSAGE_FAILED, data=data, **common)]

    # turn_before / llm_before / 未知事件：不产生协议事件
    return []


# ---------------------------------------------------------------- 线程+队列桥

_SENTINEL = object()

# llm_after 文本上限（防超长模型输出进内存；与 session.MAX_TEXT_CHARS 一致）
_LLM_TEXT_MAX = 200_000

# 可合并的增量事件：同一会话内的同类增量按时间/字符阈值合并为一次更新
_COALESCABLE = frozenset(
    {EventType.THINKING_DELTA, EventType.ANSWER_DELTA, EventType.TOOL_UPDATED}
)

# delta 载荷中文本字段名（与 events.py/session.py 约定一致）
_DELTA_TEXT_KEYS = ("text", "delta", "content")
_TOOL_UPDATE_DICT_KEYS = ("name", "detail", "status")


@dataclass
class _PendingBatch:
    """单个会话的待合并批次（仅桥线程访问，无锁）。"""

    key: Tuple[Any, ...]
    first_ts: float = 0.0
    chars: int = 0
    items: List[CardEvent] = field(default_factory=list)
    cfg: Any = None
    _think_idx: Optional[int] = None  # items 内 THINKING_DELTA 索引
    _answer_idx: Optional[int] = None  # items 内 ANSWER_DELTA 索引
    _tool_idx: Dict[str, int] = field(default_factory=dict)  # tool id -> items 索引


def _batch_key(ev: CardEvent) -> Tuple[Any, ...]:
    """合并归属键：按会话维度（conversation_id / chat_id / message_id）。"""
    return (
        ev.conversation_id or "",
        ev.chat_id or "",
        ev.message_id or ev.conversation_id or "",
    )


def _delta_text(ev: CardEvent) -> str:
    d = ev.data or {}
    for k in _DELTA_TEXT_KEYS:
        v = d.get(k)
        if isinstance(v, str):
            return v
    return ""


def _effective_delta(cfg: Any, attr: str, default: float) -> float:
    try:
        v = getattr(cfg.coalesce, attr, default)
        return float(v)
    except Exception:
        return float(default)


class _Bridge:
    """内部线程+队列桥：emit 同步入队即返回；后台线程消费、按会话合并增量后喂给引擎。

    coalescing（fail-open）：
    - 增量事件（thinking/answer/tool 更新）在会话维度暂存，达到 delta_ms 或 delta_chars
      阈值时合并为一次引擎调用（顺序仍保持）；非增量事件是屏障，先冲刷全部暂存再直发。
    - delta_ms/delta_chars 任一 <= 0 → 合并禁用（与旧行为一致，每个事件直发）。
    - 合并逻辑任何异常 → 该桥永久退化为直发（不丢事件、不崩溃）。
    - shutdown 冲刷全部暂存批次后再退出（不丢尾事件）。
    """

    def __init__(self, max_pending: int = 128) -> None:
        self.q: "queue.Queue[Any]" = queue.Queue(maxsize=max_pending)
        self.engine: Any = None
        self.lock = threading.Lock()
        self.pending: Dict[Tuple[Any, ...], _PendingBatch] = {}
        self.coalescing: bool = True
        self.thread = threading.Thread(target=self._run, name="hfc-bridge", daemon=True)
        self.thread.start()

    # ------------------------------------------------------------ 分发循环

    def _run(self) -> None:
        while True:
            try:
                item = self.q.get(timeout=0.05)
            except queue.Empty:
                # 空转：过期批次照常冲刷（并发突发后尾事件不会滞留到 shutdown）
                try:
                    self._flush_expired()
                except Exception as e:
                    _bump("errors")
                    sys.stderr.write(f"[hfc-bridge] flush_expired error: {e}\n")
                continue
            try:
                if item is _SENTINEL:
                    try:
                        self._flush_all()
                    except Exception as e:  # 冲刷失败也要退出线程
                        _bump("errors")
                        sys.stderr.write(f"[hfc-bridge] shutdown flush error: {e}\n")
                    break
                ev, cfg = item
                self._dispatch(ev, cfg)
            except Exception as e:  # fail-open：绝不因单事件崩溃
                _bump("errors")
                sys.stderr.write(f"[hfc-bridge] handle_event error: {e}\n")
            finally:
                self.q.task_done()

    def _dispatch(self, ev: CardEvent, cfg: Any) -> None:
        if self.coalescing and ev.type in _COALESCABLE:
            self._absorb(ev, cfg)
            self._flush_expired()
            if self._queue_near_full():
                self._flush_all()
            return
        if self.coalescing and self.pending:
            # 屏障事件：先冲刷暂存（保持顺序），再直发当前事件
            self._flush_all()
        self._direct(ev, cfg)

    def _absorb(self, ev: CardEvent, cfg: Any) -> None:
        """把增量事件并入待合并批次（fail-open：异常→退化直发）。"""
        try:
            key = _batch_key(ev)
            batch = self.pending.get(key)
            if batch is None:
                delta_ms = _effective_delta(cfg, "delta_ms", 250.0)
                delta_chars = _effective_delta(cfg, "delta_chars", 600.0)
                if delta_ms <= 0 or delta_chars <= 0:
                    # 配置禁用合并：先冲刷存量批次，再退化为直发
                    self._flush_all()
                    self.coalescing = False
                    self._direct(ev, cfg)
                    return
                batch = _PendingBatch(key=key, first_ts=time.monotonic(), cfg=cfg)
                self.pending[key] = batch
            self._merge_into(batch, ev)
            # 字符阈值：累计增量字符达标即冲刷（不等待时间窗口）
            if batch.chars >= _effective_delta(cfg, "delta_chars", 600.0):
                self.pending.pop(key, None)
                try:
                    self._flush_batch(batch)
                except Exception as e:  # 冲刷失败不双发当前事件
                    _bump("errors")
                    sys.stderr.write(f"[hfc-bridge] char-flush error: {e}\n")
        except Exception as e:
            self.coalescing = False
            self._direct(ev, cfg)
            _bump("errors")
            sys.stderr.write(f"[hfc-bridge] coalescing disabled (absorb failed): {e}\n")

    def _merge_into(self, batch: _PendingBatch, ev: CardEvent) -> None:
        if ev.type is EventType.THINKING_DELTA:
            if batch._think_idx is not None:
                target = batch.items[batch._think_idx]
                text = _delta_text(target) + _delta_text(ev)
                if len(text) > _LLM_TEXT_MAX:
                    text = text[:_LLM_TEXT_MAX] + "…[truncated]"
                target.data = dict(target.data or {})
                target.data["text"] = text
            else:
                batch._think_idx = len(batch.items)
                batch.items.append(ev)
            batch.chars = sum(len(_delta_text(it)) for it in batch.items)
        elif ev.type is EventType.ANSWER_DELTA:
            if batch._answer_idx is not None:
                target = batch.items[batch._answer_idx]
                text = _delta_text(target) + _delta_text(ev)
                if len(text) > _LLM_TEXT_MAX:
                    text = text[:_LLM_TEXT_MAX] + "…[truncated]"
                target.data = dict(target.data or {})
                target.data["text"] = text
            else:
                batch._answer_idx = len(batch.items)
                batch.items.append(ev)
            batch.chars = sum(len(_delta_text(it)) for it in batch.items)
        elif ev.type is EventType.TOOL_UPDATED:
            d = ev.data or {}
            tid = d.get("id") or d.get("tool_id") or d.get("name") or ""
            idx = batch._tool_idx.get(tid)
            if idx is not None:
                batch.items[idx] = ev  # 同 id 只保留最新快照
            else:
                batch._tool_idx[tid] = len(batch.items)
                batch.items.append(ev)
        else:  # 防御：不可合并类型不应走到这里
            batch.items.append(ev)

    def _flush_expired(self) -> None:
        if not self.pending:
            return
        now = time.monotonic()
        expired = [
            k
            for k, b in self.pending.items()
            if (now - b.first_ts) * 1000.0
            >= _effective_delta(b.cfg, "delta_ms", 250.0)  # delta_ms 单位毫秒
        ]
        for k in expired:
            batch = self.pending.pop(k)
            try:
                self._flush_batch(batch)
            except Exception as e:  # 单批次失败不影响其他批次
                _bump("errors")
                sys.stderr.write(f"[hfc-bridge] flush batch error: {e}\n")

    def _flush_all(self) -> None:
        while self.pending:
            k, b = self.pending.popitem()
            try:
                self._flush_batch(b)
            except Exception as e:  # 单批次失败不影响其他批次
                _bump("errors")
                sys.stderr.write(f"[hfc-bridge] flush batch error: {e}\n")

    def _flush_batch(self, batch: _PendingBatch) -> None:
        if not batch.items:
            return
        engine = self._get_engine(batch.cfg)
        if engine is None:
            _bump("dropped")
            return
        for ev in batch.items:
            # 单事件异常隔离：一个坏事件不杀整批（批次内顺序保持）
            try:
                engine.handle_event(ev)
                _bump("handled")
            except Exception as e:
                _bump("errors")
                sys.stderr.write(f"[hfc-bridge] handle event error: {e}\n")
        if len(batch.items) > 1:
            _bump("coalesced")

    def _queue_near_full(self) -> bool:
        # 待合并事件也在队列里占位：接近满载时提前冲刷，避免队满丢事件
        try:
            return self.q.qsize() >= max(1, self.q.maxsize - 8)
        except Exception:
            return False

    # ------------------------------------------------------------ 直发路径

    def _direct(self, ev: CardEvent, cfg: Any) -> None:
        engine = self._get_engine(cfg)
        if engine is None:
            _bump("dropped")
            return
        try:
            engine.handle_event(ev)
            _bump("handled")
        except Exception as e:
            _bump("errors")
            sys.stderr.write(f"[hfc-bridge] handle event error: {e}\n")

    def _get_engine(self, cfg: Any) -> Any:
        if self.engine is not None:
            return self.engine
        with self.lock:
            if self.engine is None:
                self.engine = _make_engine(cfg)
            return self.engine

    def put(self, ev: CardEvent, cfg: Any) -> None:
        try:
            self.q.put_nowait((ev, cfg))
            _bump("emitted")
        except queue.Full:  # 队列满：丢弃保活（fail-open）
            _bump("dropped")
            sys.stderr.write("[hfc-bridge] queue full, event dropped (fail-open)\n")

    def shutdown(self, timeout: float = 1.0) -> None:
        try:
            self.q.put_nowait(_SENTINEL)
        except queue.Full:
            pass
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=timeout)


_BRIDGE: Optional[_Bridge] = None
_BRIDGE_LOCK = threading.Lock()


def _host_sdk_transport(module: Any = None) -> Any:
    """从宿主前端适配已鉴权 SDK；不读取、复制或记录任何凭据。"""
    try:
        if module is None:
            module = sys.modules.get("__main__")
        send_raw = getattr(module, "_send_raw", None)
        patch_card = getattr(module, "_patch_card", None)
        if not callable(send_raw) or not callable(patch_card):
            return None
        from .transport import CallableTransport

        def send(chat_id: str, card: Dict[str, Any]) -> Any:
            content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
            # 飞书用户 open_id 以 ``ou_`` 开头；会话 chat_id 通常以 ``oc_``
            # 开头。宿主 SDK 同时支持两者，按目标前缀选择 receive_id_type，
            # 使无 chat_id 的单聊部署也能复用既有授权用户映射。
            receive_id_type = "open_id" if str(chat_id).startswith("ou_") else "chat_id"
            return send_raw(chat_id, content, "interactive", receive_id_type)

        def update(message_id: str, card: Dict[str, Any]) -> bool:
            content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
            return bool(patch_card(message_id, content))

        return CallableTransport(send, update)
    except Exception:
        return None


def _make_engine(cfg: Any) -> Any:
    """按 cfg 惰性构建引擎；engine.py 未就绪或导入失败 → None（事件丢弃，fail-open）。"""
    try:
        from . import engine as _engine_mod

        transport = _host_sdk_transport() if getattr(cfg, "transport", None) == "http" else None
        if transport is None and hasattr(_engine_mod, "build_transport"):
            transport = _engine_mod.build_transport(cfg)
        elif transport is None and getattr(cfg, "transport", None) == "http" and hasattr(_engine_mod, "build_http_transport"):
            transport = _engine_mod.build_http_transport(cfg.http)
        if transport is None:
            from .transport import FakeTransport

            transport = FakeTransport()
        return _engine_mod.CardEngine(cfg, transport)
    except Exception as e:
        sys.stderr.write(f"[hfc-bridge] engine unavailable: {e} (fail-open)\n")
        return None


def _make_sdk_engine() -> Any:
    """宿主直发命令路径（未传 cfg、桥未预热）的引擎兜底。

    仅依赖宿主主模块已注入的 ``_send_raw``/``_patch_card``（T8 宿主 SDK
    适配），不依赖事件流预热与配置对象；失败 → None（fail-open，调用方
    回退纯文本，绝不抛异常）。
    """
    try:
        from . import engine as _engine_mod

        transport = _host_sdk_transport()
        if transport is None:
            return None
        return _engine_mod.CardEngine(None, transport)
    except Exception as e:
        sys.stderr.write(f"[hfc-bridge] sdk engine unavailable: {e} (fail-open)\n")
        return None


def emit_from_ga_locals_threadsafe(ctx: Dict[str, Any], cfg: Any) -> None:
    """同步调用、内部线程+队列桥；绝不抛异常（fail-open，异常记 stderr/metrics）。"""
    try:
        enabled = getattr(cfg, "enabled", True) if cfg is not None else True
        if not enabled:
            return
        events = map_ga_ctx(ctx)
        if not events:
            return
        global _BRIDGE
        if _BRIDGE is None:
            with _BRIDGE_LOCK:
                if _BRIDGE is None:
                    max_pending = 128
                    try:
                        max_pending = int(cfg.coalesce.max_pending)
                    except Exception:
                        pass
                    _BRIDGE = _Bridge(max_pending=max_pending)
        for ev in events:
            _BRIDGE.put(ev, cfg)
    except Exception as e:
        _bump("errors")
        sys.stderr.write(f"[hfc-bridge] emit error: {e}\n")


def shutdown(timeout: float = 1.0) -> None:
    """停线程桥（hfc_bridge._uninstall 调用）。幂等、绝不抛异常。"""
    global _BRIDGE
    with _BRIDGE_LOCK:
        b = _BRIDGE
        _BRIDGE = None
    if b is not None:
        try:
            b.shutdown(timeout=timeout)
        except Exception:
            pass


def reset_for_test() -> None:
    """测试专用：停桥并清空会话/工具状态（不用于生产路径）。"""
    shutdown()
    with _CHAT_LOCK:
        _CHAT_BY_SESSION.clear()
    with _TOOL_LOCK:
        _TOOL_STARTED.clear()


def send_command_result_card(
    chat_id: str,
    command: str,
    content: str,
    reply_to: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    cfg: Any = None,
    current_model: Optional[str] = None,
) -> bool:
    """同步发送命令结果卡（命令路径，不经事件桥/流式合并）。绝不抛异常。

    语义（T4 命令层设计 3.1/3.2）：
    - HFC 关闭（cfg.enabled=False）→ False，调用方回退文本（fail-open，不拦截）；
    - 无可用引擎（桥未激活且 _make_engine 失败）→ False；
    - 发送成功（transport 返回 delivered）→ True；其余（unknown/异常）→ False。
    - 标题/模板由 command 映射表决定（metadata.title/template 可覆盖）；
      content 经 render_command_result_card 安全渲染（永不抛，超限降级安全卡）。
    - current_model（T27）：按钮行「切换模型」文案动态显示当前模型名，由宿主
      fsapp._reply 注入 self.agent.get_llm_name()，None 时回退静态文案。

    reply_to/metadata 为宿主侧扩展位（当前 transport 不携带），保持与一期
    事件契约一致，不泄露工具名/参数/结果。
    """
    try:
        if cfg is not None and not getattr(cfg, "enabled", True):
            return False
        from . import command as _cmd

        has_arg = bool(str(command or "").strip().split()[1:])
        if isinstance(metadata, dict):
            title = metadata.get("title") or _cmd.title_for(command, has_arg=has_arg)
            template = metadata.get("template") or _cmd.template_for(command, has_arg=has_arg)
        else:
            title = _cmd.title_for(command, has_arg=has_arg)
            template = _cmd.template_for(command, has_arg=has_arg)
        card = _cmd.render_command_result_card(
            str(content or ""), title, template, current_model=current_model
        )

        engine = getattr(_BRIDGE, "engine", None) if _BRIDGE is not None else None
        if engine is None:
            # 懒创建：显式 cfg 优先（事件桥语义）；无 cfg（宿主直发命令路径，
            # 如重启后未发普通消息直接 /new）→ 宿主 SDK transport 兜底引擎。
            engine = _make_engine(cfg) if cfg is not None else _make_sdk_engine()
        if engine is None:
            return False
        result = engine.transport.send_card(chat_id, card)
        return bool(getattr(result, "ok", False))
    except Exception:
        return False


def send_card(chat_id: str, card: Dict[str, Any], cfg: Any = None,
              reply_to: Optional[str] = None) -> bool:
    """发送宿主侧预构造的任意卡片（T27：/settings 二级菜单卡、信息文本卡）。

    与 send_command_result_card 同引擎获取语义（桥预热 / cfg 懒创建 / SDK 兜底），
    仅负责发送：卡片 JSON 由调用方（fsapp 经 render_settings_menu_card /
    render_plain_info_card 等）构造，本函数不解析内容、不落任何命令/工具信息。
    绝不抛异常（fail-open，失败返回 False 由调用方决定是否回退）。
    reply_to 为宿主侧扩展位（当前 transport 不携带），保持与
    send_command_result_card 一致签名（T27-E：修复 fsapp hfc_bridge 透传
    reply_to 时的 TypeError）。
    """
    try:
        if cfg is not None and not getattr(cfg, "enabled", True):
            return False
        engine = getattr(_BRIDGE, "engine", None) if _BRIDGE is not None else None
        if engine is None:
            engine = _make_engine(cfg) if cfg is not None else _make_sdk_engine()
        if engine is None:
            return False
        result = engine.transport.send_card(chat_id, card)
        return bool(getattr(result, "ok", False))
    except Exception:
        return False

"""卡片引擎（独立实现）。

设计约定：
- CardEngine.handle_event(ev: CardEvent) -> EventResult(applied, reason)；单个 CardEngine 实例内串行处理事件（内部锁保护状态与终态去重），不同实例可并行；
- sequence 防乱序：旧/重复 sequence 丢弃；
- 终态去重：同一 message 的 completed/failed 只应用一次；
- fallback_message_id：确定性生成（chat/conversation/thread 派生）；
- UPDATE_MAX_ATTEMPTS=3：update 结果 unknown 时重试，不无限重试；
- disposition card/native：native 直接跳过卡片投递；
- fail-open：send/update 失败不阻塞状态应用（applied=True + reason 标注）。

依赖：events.CardEvent / session.CardSession（apply_event）。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .config import EngineConfig
from .delivery_policy import DeliveryPolicy
from .events import CardEvent, EventType
from .lifecycle import CleanupPolicy, cleanup_expired
from .render import render_card
from .session import CardSession
from .transport import CardTransport, FakeTransport, SendResult, UpdateResult

logger = logging.getLogger(__name__)

TERMINAL_EVENTS = frozenset({EventType.MESSAGE_COMPLETED, EventType.MESSAGE_FAILED})


@dataclass
class EventResult:
    """事件处理结果：applied 表示已应用（含仅投递失败的情形）；reason 为原因/备注。"""

    applied: bool = False
    reason: Optional[str] = None

    def __bool__(self) -> bool:
        return self.applied


class CardEngine:
    """会话编排 + 投递。transport 结果三态语义：
    - delivered/updated：成功；
    - not_sent/not_found：明确失败，不重试；
    - unknown：重试至 UPDATE_MAX_ATTEMPTS 后放弃（不无限重试）。
    """

    UPDATE_MAX_ATTEMPTS = 3

    def __init__(
        self,
        cfg: Optional[EngineConfig] = None,
        transport: Optional[CardTransport] = None,
        gc_interval_seconds: float = 60.0,
        gc_policy: Optional[CleanupPolicy] = None,
    ) -> None:
        self.cfg = cfg or EngineConfig()
        self.transport = transport or FakeTransport()
        self.sessions: Dict[str, CardSession] = {}
        self._last_seq: Dict[str, int] = {}
        self._terminal_applied: Dict[str, Set[str]] = {}
        # handle_event 是单引擎实例的串行事务：sequence、会话状态、投递和终态
        # 登记必须原子执行，避免并发 completed/failed 重复投递。
        self._event_lock = threading.Lock()
        # GC 接线：惰性周期清理（默认 60s；0 表示每次 handle_event 都尝试清理）
        self.gc_interval_seconds = float(gc_interval_seconds)
        self.gc_policy = gc_policy
        self._last_gc_at: float = time.time()
        self.gc_removed: List[str] = []  # 最近一次清理回收的会话键（metrics 反映）
        self.gc_removed_total: int = 0

    def remove_session(self, key: str) -> None:
        """移除会话（GC 使用；记录 metrics 供观察）。同步清理防乱序状态，
        避免会话回收后新事件被旧 sequence 误判为 stale。"""
        self.sessions.pop(key, None)
        self._last_seq.pop(key, None)
        self._terminal_applied.pop(key, None)

    def _maybe_gc(self, now: Optional[float] = None) -> List[str]:
        """惰性周期清理：距上次清理 >= gc_interval_seconds 且存在会话时执行。
        返回本次回收的会话键列表（无会话或未到间隔返回空列表）。"""
        if not self.sessions:
            return []
        now = now if now is not None else time.time()
        if now - self._last_gc_at < self.gc_interval_seconds:
            return []
        self._last_gc_at = now
        policy = self.gc_policy or CleanupPolicy(
            retention_seconds=self.cfg.limits.retention_seconds,
            zombie_grace_seconds=self.cfg.limits.zombie_grace_seconds,
            history_limit=self.cfg.limits.history_limit,
        )
        removed = cleanup_expired(self, policy, now=now)
        self.gc_removed = list(removed)
        self.gc_removed_total += len(removed)
        return removed

    # ---------- 定位辅助 ----------

    def _key(self, ev: CardEvent) -> str:
        if ev.conversation_id:
            return ev.conversation_id
        if ev.chat_id:
            return f"chat:{ev.chat_id}"
        return "global"

    def fallback_message_id(self, ev: CardEvent) -> str:
        """确定性回退 message_id：由 chat/conversation/thread 派生（同会话稳定）。"""
        raw = "|".join(filter(None, (ev.chat_id, ev.conversation_id, ev.thread_id)))
        if not raw:
            raw = f"{ev.type.value}|{ev.sequence}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        return f"fallback-{digest}"

    def _msg_key(self, ev: CardEvent) -> str:
        return ev.message_id or self.fallback_message_id(ev)

    def _get_or_create(self, ev: CardEvent) -> CardSession:
        key = self._key(ev)
        session = self.sessions.get(key)
        if session is None:
            session = CardSession(
                conversation_id=ev.conversation_id or key,
                chat_id=ev.chat_id or "",
            )
            self.sessions[key] = session
        return session

    # ---------- 投递 ----------

    def _render_safe(self, session: CardSession) -> Dict[str, Any]:
        """渲染卡片（fail-open：render 异常 → 最小降级卡，不阻塞投递）。

        降级卡只含固定错误码（render_error: code=RC01），不拼接原始异常；
        异常详情仅进受控诊断通道（logger.debug），不进入卡片内容。
        """
        try:
            return render_card(session, self.cfg.card_limits)
        except Exception as e:
            logger.debug("render degraded (RC01): %r", e, exc_info=True)
            return {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "red",
                    "title": {"tag": "plain_text", "content": "卡片渲染失败（已降级）"},
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": "render_error: code=RC01"}}
                ],
            }

    def _send_started(self, session: CardSession, ev: CardEvent) -> str:
        """发送首卡。返回投递备注（None 语义用空串表示成功）。"""
        if session.message_id is not None:
            # 事件已带 message_id（消息已存在）→ 走更新而非重复发送
            return self._update_session_card(session, ev)
        card = self._render_safe(session)
        try:
            result: SendResult = self.transport.send_card(session.chat_id, card)
        except Exception:
            result = SendResult("unknown")
        if result.outcome == "delivered":
            if result.message_id:
                session.message_id = result.message_id
            elif session.message_id is None:
                session.message_id = self.fallback_message_id(ev)
            return ""
        if session.message_id is None:
            # fail-open：保留确定性回退 id，后续更新仍可尝试
            session.message_id = self.fallback_message_id(ev)
        return f"delivery:send_{result.outcome}"

    def _update_with_retry(self, message_id: str, card: Dict[str, Any]) -> str:
        """update 带重试（unknown 重试，not_found 停止）。返回 outcome 字符串。"""
        last_outcome = "unknown"
        for _ in range(self.UPDATE_MAX_ATTEMPTS):
            try:
                result: UpdateResult = self.transport.update_card(message_id, card)
            except Exception:
                result = UpdateResult.unknown()
            last_outcome = result.outcome
            if result.outcome in ("updated", "not_found"):
                break
        return last_outcome

    def _update_session_card(self, session: CardSession, ev: CardEvent) -> str:
        if not session.chat_id:
            return "no_chat_target"  # 全局 notice 无投递对象
        message_id = session.message_id or self.fallback_message_id(ev)
        card = self._render_safe(session)
        outcome = self._update_with_retry(message_id, card)
        if outcome == "updated":
            return ""
        return f"delivery:update_{outcome}"

    # ---------- 主入口 ----------

    def handle_event(self, ev: CardEvent) -> EventResult:
        """串行处理单一事件，保证终态去重检查与登记属于同一原子事务。"""
        with self._event_lock:
            return self._handle_event_unlocked(ev)

    def _handle_event_unlocked(self, ev: CardEvent) -> EventResult:
        """处理单个事件。返回 EventResult(applied, reason)。"""
        if not isinstance(ev, CardEvent):
            return EventResult(False, "invalid_event")
        if not self.cfg.enabled:
            return EventResult(False, "disabled")

        # 惰性周期清理：距上次清理 >= gc_interval_seconds 时回收过期会话
        # （活动会话豁免；fail-open：清理异常不阻塞事件处理）
        try:
            self._maybe_gc()
        except Exception:
            pass

        key = self._key(ev)

        # 1) sequence 防乱序（旧 seq / 重复 seq 一律丢弃）
        last = self._last_seq.get(key, -1)
        if ev.sequence <= last:
            return EventResult(False, "stale_sequence")

        # 2) 投递形态判定（native 跳过卡片投递，但仍记录 seq 防重复）
        if self.cfg.delivery.decide_disposition(ev.chat_id, ev) == "native":
            self._last_seq[key] = ev.sequence
            return EventResult(False, "native_disposition")

        # 3) 终态去重（同一 message 的 completed/failed 只应用一次）
        is_terminal = ev.type in TERMINAL_EVENTS
        if is_terminal:
            mkey = self._msg_key(ev)
            if mkey in self._terminal_applied.setdefault(key, set()):
                return EventResult(False, "terminal_already_applied")

        # 4) 应用事件到会话（apply_event 内置 last_sequence/touch；防御：单事件失败跳过，不中断）
        session = self._get_or_create(ev)
        try:
            session.apply_event(ev)
        except Exception:
            return EventResult(False, "apply_error")
        self._last_seq[key] = ev.sequence

        # 5) 投递（fail-open：投递失败不阻塞状态应用）
        note = ""
        if ev.type is EventType.MESSAGE_STARTED:
            note = self._send_started(session, ev)
        else:
            note = self._update_session_card(session, ev)

        # 6) 应用成功后登记终态（防重复应用）
        if is_terminal:
            self._terminal_applied[key].add(self._msg_key(ev))

        return EventResult(True, note or None)

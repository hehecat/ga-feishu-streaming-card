"""GC 接线测试：engine 惰性周期清理（lifecycle.cleanup_expired 的调用方）。

覆盖：
- handle_event 内惰性触发（默认 60s 间隔）；
- gc_interval_seconds=0 → 每次事件都尝试清理（测试强制触发）；
- 过期终态会话被回收、metrics(gc_removed/gc_removed_total) 反映；
- 活动 thinking 会话豁免（updated_at 新）；
- history_limit 收缩生效（engine 配置驱动）；
- 清理异常 fail-open（不阻塞事件处理）；
- 回收后新事件不因旧 _last_seq 被误判 stale。
"""
import time

from ga_feishu_streaming_card.config import EngineConfig, LimitsConfig
from ga_feishu_streaming_card.engine import CardEngine
from ga_feishu_streaming_card.events import CardEvent, EventType
from ga_feishu_streaming_card.lifecycle import CleanupPolicy
from ga_feishu_streaming_card.transport import FakeTransport


def _ev(
    type_: EventType,
    seq: int,
    conversation_id: str = "c1",
    chat_id: str = "oc_1",
    message_id=None,
    data=None,
) -> CardEvent:
    return CardEvent(
        type=type_,
        sequence=seq,
        created_at=100.0 + seq,
        conversation_id=conversation_id,
        chat_id=chat_id,
        message_id=message_id,
        data=data or {},
    )


def _engine(**kw) -> CardEngine:
    cfg = EngineConfig(
        limits=LimitsConfig(
            retention_seconds=60.0,
            zombie_grace_seconds=120.0,
            history_limit=50,
        )
    )
    return CardEngine(cfg=cfg, transport=FakeTransport(), **kw)


def _expire(eng: CardEngine, key: str, age: float = 9999.0, status: str = "completed") -> None:
    """构造一个已过期会话（created_at 与 updated_at 都很旧）。"""
    ev = _ev(EventType.MESSAGE_STARTED, 1, conversation_id=key)
    eng.handle_event(ev)
    s = eng.sessions[key]
    s.status = status
    s.created_at = time.time() - age
    s.updated_at = time.time() - age


class TestEngineGcWiring:
    def test_gc_triggered_by_interval(self):
        eng = _engine(gc_interval_seconds=0)  # 每次事件都尝试清理
        _expire(eng, "old", age=9999)
        assert "old" in eng.sessions
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1, conversation_id="fresh"))
        assert "old" not in eng.sessions  # 已被回收
        assert eng.gc_removed == ["old"]
        assert eng.gc_removed_total == 1

    def test_gc_not_triggered_before_interval(self):
        eng = _engine(gc_interval_seconds=3600)
        _expire(eng, "old", age=9999)
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1, conversation_id="fresh"))
        assert "old" in eng.sessions  # 未到间隔，不清理

    def test_gc_forced_by_rewinding_last_run(self):
        eng = _engine(gc_interval_seconds=60)
        _expire(eng, "old", age=9999)
        eng._last_gc_at = time.time() - 120  # 模拟距上次清理已过 120s
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1, conversation_id="fresh"))
        assert "old" not in eng.sessions

    def test_active_thinking_session_survives_gc(self):
        eng = _engine(gc_interval_seconds=0)
        _expire(eng, "old_done", age=9999, status="completed")
        # 活动会话：创建很久但持续有事件（updated_at 保持新）
        ev = _ev(EventType.MESSAGE_STARTED, 1, conversation_id="active")
        eng.handle_event(ev)
        s = eng.sessions["active"]
        s.created_at = time.time() - 9999  # 创建于很久前
        s.updated_at = time.time() - 1     # 但 1 秒前仍活动
        s.status = "thinking"
        eng.handle_event(_ev(EventType.THINKING_DELTA, 2, conversation_id="active",
                             data={"text": "x"}))
        assert "active" in eng.sessions  # 活动会话豁免
        assert "old_done" not in eng.sessions

    def test_history_limit_shrink_from_engine_config(self):
        cfg = EngineConfig(
            limits=LimitsConfig(
                retention_seconds=3600.0,
                zombie_grace_seconds=120.0,
                history_limit=2,
            )
        )
        eng = CardEngine(cfg=cfg, transport=FakeTransport(), gc_interval_seconds=0)
        # 先有一个正常会话（活动，豁免收缩），再压入 4 个已完成过期会话
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1, conversation_id="live"))
        for i in range(4):
            _expire(eng, f"s{i}", age=100.0, status="completed")
        eng.handle_event(_ev(EventType.THINKING_DELTA, 2, conversation_id="live",
                             data={"text": "x"}))
        assert len(eng.sessions) <= 2  # 收缩到 history_limit
        assert "live" in eng.sessions  # 活动会话优先保留
        assert eng.gc_removed_total >= 3

    def test_gc_fail_open_does_not_block_event(self):
        eng = _engine(gc_interval_seconds=0)
        eng.sessions["x"] = object()  # 非 CardSession → cleanup 可能异常

        def boom(*a, **k):
            raise RuntimeError("gc boom")

        eng._maybe_gc = boom
        r = eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1, conversation_id="c1"))
        assert r.applied is True  # 清理异常不阻塞事件处理

    def test_reclaimed_session_new_events_not_stale(self):
        eng = _engine(gc_interval_seconds=0)
        _expire(eng, "c1", age=9999)  # seq=1 已记录
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 2, conversation_id="other"))  # 触发 GC
        assert "c1" not in eng.sessions
        # 会话被回收后，新事件从 seq=1 重新开始不应被误判 stale
        r = eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1, conversation_id="c1"))
        assert r.applied is True
        assert "c1" in eng.sessions

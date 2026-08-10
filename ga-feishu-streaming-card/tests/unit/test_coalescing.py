"""coalescing 单元测试：_Bridge 增量事件合并/冲刷/fail-open。"""
import time
from types import SimpleNamespace

import pytest

from ga_feishu_streaming_card import bridge
from ga_feishu_streaming_card.events import CardEvent, EventType


def _cfg(delta_ms=250, delta_chars=600, max_pending=128):
    return SimpleNamespace(
        coalesce=SimpleNamespace(
            delta_ms=delta_ms, delta_chars=delta_chars, max_pending=max_pending
        )
    )


def _ev(type_, conv, text=None, seq=1, **data):
    d = dict(data)
    if text is not None:
        d["text"] = text
    return CardEvent(
        type=type_,
        conversation_id=conv,
        chat_id=f"chat_{conv}",
        sequence=seq,
        created_at=1000.0 + seq,
        data=d,
    )


def _wait_for(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


class FakeEngine:
    def __init__(self):
        self.events = []

    def handle_event(self, ev):
        self.events.append(ev)
        return SimpleNamespace(applied=True, reason=None)


@pytest.fixture(autouse=True)
def _clean_bridge(monkeypatch):
    bridge.reset_for_test()
    yield
    bridge.reset_for_test()


@pytest.fixture
def fake_engine(monkeypatch):
    fe = FakeEngine()
    monkeypatch.setattr(bridge, "_make_engine", lambda cfg: fe)
    return fe


def _make_bridge(cfg):
    b = bridge._Bridge(max_pending=cfg.coalesce.max_pending)
    return b


# ---------------- 合并：突发批量 ----------------

def test_burst_update_calls_with_fake_transport(monkeypatch):
    """规格验收：真实引擎+FakeTransport，20×100字符突发 → update_card 调用数 ≤5、文本全送达。"""
    from ga_feishu_streaming_card.config import EngineConfig
    from ga_feishu_streaming_card.engine import CardEngine
    from ga_feishu_streaming_card.transport import FakeTransport

    ft = FakeTransport()
    eng = CardEngine(cfg=EngineConfig(), transport=ft)
    monkeypatch.setattr(bridge, "_make_engine", lambda cfg: eng)
    cfg = _cfg(delta_ms=10000, delta_chars=600)  # 时间窗口放大：仅字符阈值触发
    b = _make_bridge(cfg)
    try:
        texts = ["y" * 100 for _ in range(20)]
        for i, t in enumerate(texts):
            b.put(_ev(EventType.ANSWER_DELTA, "s1", text=t, seq=i + 1), cfg)
        updates = lambda: sum(1 for c in ft.calls if c["op"] == "update_card")
        assert _wait_for(lambda: updates() >= 3), "字符阈值应触发 ≥3 次更新"
        b.shutdown(timeout=2.0)
        n = updates()
        assert n <= 5, f"update_card 调用 {n} 次，超过合并上限 ≤5"
        # 卡片渲染为累计全文（600+1200+1800+2000）；取最后一次更新核对完整送达
        last_card = [c["card"] for c in ft.calls if c["op"] == "update_card"][-1]
        elements = (last_card.get("body") or {}).get("elements") or last_card.get("elements") or []
        final_text = "".join(
            el.get("content", "") for el in elements
        )
        assert final_text.count("y") == 2000, "合并后全部文本必须最终送达"
    finally:
        b.shutdown(timeout=1.0)


def test_burst_coalesces_into_few_updates(fake_engine):
    """20×100字符突发：字符阈值600 → 引擎更新次数≤5，文本顺序完整。"""
    cfg = _cfg()
    b = _make_bridge(cfg)
    try:
        texts = [f"chunk-{i:03d}-" + "x" * 90 for i in range(20)]
        for i, t in enumerate(texts):
            b.put(_ev(EventType.ANSWER_DELTA, "s1", text=t, seq=i + 1), cfg)
        assert _wait_for(lambda: fake_engine.events and len(fake_engine.events) >= 3)
        assert _wait_for(lambda: not b.pending)
        assert len(fake_engine.events) <= 5, f"got {len(fake_engine.events)} updates"
        joined = "".join(
            e.data.get("text", "") for e in fake_engine.events if e.type is EventType.ANSWER_DELTA
        )
        assert joined == "".join(texts), "合并后文本必须完整且保序"
    finally:
        b.shutdown(timeout=1.0)


def test_shutdown_flushes_pending_tail(fake_engine):
    """shutdown 冲刷暂存：3个增量合并为1次引擎调用，不丢尾事件。"""
    cfg = _cfg(delta_ms=10000, delta_chars=100000)  # 窗口远大于测试时长
    b = _make_bridge(cfg)
    try:
        for i, t in enumerate(["aa", "bb", "cc"]):
            b.put(_ev(EventType.ANSWER_DELTA, "s1", text=t, seq=i + 1), cfg)
        b.shutdown(timeout=2.0)
        assert len(fake_engine.events) == 1
        assert fake_engine.events[0].data["text"] == "aabbcc"
    finally:
        b.shutdown(timeout=1.0)


# ---------------- 禁用与退化 ----------------

def test_disabled_coalescing_passthrough(fake_engine):
    """delta_ms=0 → 合并禁用：20事件逐一直发。"""
    cfg = _cfg(delta_ms=0, delta_chars=600)
    b = _make_bridge(cfg)
    try:
        for i in range(20):
            b.put(_ev(EventType.ANSWER_DELTA, "s1", text=f"t{i}", seq=i + 1), cfg)
        assert _wait_for(lambda: len(fake_engine.events) == 20)
        assert b.coalescing is False
        assert [e.data["text"] for e in fake_engine.events][:3] == ["t0", "t1", "t2"]
    finally:
        b.shutdown(timeout=1.0)


def test_absorb_failure_falls_back_direct(fake_engine, monkeypatch):
    """合并逻辑异常 → 退化直发：事件不丢、不重复。"""
    def boom(self, batch, ev):
        raise RuntimeError("merge boom")

    monkeypatch.setattr(bridge._Bridge, "_merge_into", boom)
    cfg = _cfg()
    b = _make_bridge(cfg)
    try:
        for i in range(5):
            b.put(_ev(EventType.ANSWER_DELTA, "s1", text=f"t{i}", seq=i + 1), cfg)
        b.put(_ev(EventType.MESSAGE_STARTED, "s1", seq=99), cfg)
        assert _wait_for(lambda: len(fake_engine.events) == 6)
        assert b.coalescing is False
    finally:
        b.shutdown(timeout=1.0)


# ---------------- 会话隔离 / tool 去重 / 屏障保序 ----------------

def test_sessions_isolated(fake_engine):
    """跨会话独立合并：交错突发后各会话仅1次更新，文本不混。"""
    cfg = _cfg(delta_ms=10000, delta_chars=100000)
    b = _make_bridge(cfg)
    try:
        for i in range(3):
            b.put(_ev(EventType.ANSWER_DELTA, "sa", text=f"A{i}", seq=i + 1), cfg)
            b.put(_ev(EventType.ANSWER_DELTA, "sb", text=f"B{i}", seq=i + 1), cfg)
        b.shutdown(timeout=2.0)
        texts = sorted(e.data["text"] for e in fake_engine.events)
        assert texts == ["A0A1A2", "B0B1B2"]
    finally:
        b.shutdown(timeout=1.0)


def test_tool_dedup_keeps_latest(fake_engine):
    """同一 tool_id 多次更新只保留最新快照。"""
    cfg = _cfg(delta_ms=10000, delta_chars=100000)
    b = _make_bridge(cfg)
    try:
        b.put(_ev(EventType.TOOL_UPDATED, "s1", seq=1, tool_id="t1", status="running"), cfg)
        b.put(_ev(EventType.TOOL_UPDATED, "s1", seq=2, tool_id="t1", status="running", detail="50%"), cfg)
        b.put(_ev(EventType.TOOL_UPDATED, "s1", seq=3, tool_id="t1", status="completed", detail="done"), cfg)
        b.shutdown(timeout=2.0)
        assert len(fake_engine.events) == 1
        ev = fake_engine.events[0]
        assert ev.data["status"] == "completed" and ev.data["detail"] == "done"
    finally:
        b.shutdown(timeout=1.0)


def test_terminal_event_flushes_immediately(fake_engine):
    """终态事件（MESSAGE_COMPLETED）到达 → 暂存批次立即冲刷，不等待窗口。"""
    cfg = _cfg(delta_ms=10000, delta_chars=100000)
    b = _make_bridge(cfg)
    try:
        b.put(_ev(EventType.ANSWER_DELTA, "s1", text="final-answer", seq=1), cfg)
        b.put(_ev(EventType.MESSAGE_COMPLETED, "s1", seq=2), cfg)
        assert _wait_for(lambda: len(fake_engine.events) == 2)
        assert fake_engine.events[0].data["text"] == "final-answer"
        assert fake_engine.events[1].type is EventType.MESSAGE_COMPLETED
        assert not b.pending
    finally:
        b.shutdown(timeout=1.0)


def test_barrier_flushes_pending_before_direct(fake_engine):
    """屏障事件（MESSAGE_STARTED）先冲刷暂存批次再直发，顺序保持。"""
    cfg = _cfg(delta_ms=10000, delta_chars=100000)
    b = _make_bridge(cfg)
    try:
        b.put(_ev(EventType.ANSWER_DELTA, "s1", text="hello ", seq=1), cfg)
        b.put(_ev(EventType.TOOL_UPDATED, "s1", seq=2, tool_id="t9", status="completed"), cfg)
        b.put(_ev(EventType.MESSAGE_STARTED, "s1", seq=3), cfg)
        # 批次=[ANSWER_DELTA, TOOL_UPDATED] 冲刷2次引擎调用 + 屏障直发1次 = 3
        assert _wait_for(lambda: len(fake_engine.events) == 3)
        assert fake_engine.events[0].type is EventType.ANSWER_DELTA
        assert fake_engine.events[0].data["text"] == "hello "
        assert fake_engine.events[1].type is EventType.TOOL_UPDATED
        assert fake_engine.events[1].data["status"] == "completed"
        assert fake_engine.events[2].type is EventType.MESSAGE_STARTED
    finally:
        b.shutdown(timeout=1.0)

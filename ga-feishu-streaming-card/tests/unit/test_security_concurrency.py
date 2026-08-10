"""安全审查测试：并发与批次隔离（fail-open）。"""
import threading
import time
from types import SimpleNamespace

import pytest

from ga_feishu_streaming_card import bridge


CFG = SimpleNamespace(enabled=True, coalesce=SimpleNamespace(max_pending=128))


def _ctx(**kw):
    base = dict(
        handler=SimpleNamespace(
            name="GenericAgent",
            parent=SimpleNamespace(task_dir="/tmp/ga/t1"),
        ),
        user_input="hello",
        max_turns=40,
        turn=1,
        chat_id="oc_conc",
    )
    base.update(kw)
    return base


def _wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class RecorderEngine:
    def __init__(self, raise_on=None):
        self.events = []
        self.lock = threading.Lock()
        self.raise_on = raise_on

    def handle_event(self, ev):
        with self.lock:
            self.events.append(ev)
            if self.raise_on is not None and self.raise_on(ev):
                raise RuntimeError("injected failure")
        return SimpleNamespace(applied=True, reason=None)

    def count(self):
        with self.lock:
            return len(self.events)


@pytest.fixture(autouse=True)
def _clean_bridge():
    bridge.reset_for_test()
    yield
    bridge.reset_for_test()


def test_batch_single_bad_event_does_not_kill_batch(monkeypatch):
    """批次内一个坏事件 → 其他事件仍被处理（隔离）。"""
    eng = RecorderEngine(raise_on=lambda ev: ev.data.get("index") == 2)
    monkeypatch.setattr(bridge, "_make_engine", lambda cfg: eng)
    for i in range(1, 4):
        bridge.emit_from_ga_locals_threadsafe(
            _ctx(_hfc_event="tool_before", tool_name=f"t{i}", index=i), CFG
        )
    assert _wait_for(lambda: eng.count() >= 3), f"got {eng.count()}"
    assert eng.count() == 3  # 三个都尝试处理（第二个抛错被隔离）


def test_direct_bad_event_isolated(monkeypatch):
    eng = RecorderEngine(raise_on=lambda ev: True)
    monkeypatch.setattr(bridge, "_make_engine", lambda cfg: eng)
    # agent_before 走直发路径
    bridge.emit_from_ga_locals_threadsafe(_ctx(_hfc_event="agent_before"), CFG)
    assert _wait_for(lambda: eng.count() == 1)


def test_concurrent_hostile_hammer(monkeypatch):
    """多线程灌畸形帧：无异常泄漏、无死锁、事件不丢（fail-open）。"""
    eng = RecorderEngine()
    monkeypatch.setattr(bridge, "_make_engine", lambda cfg: eng)

    errors = []
    barrier = threading.Barrier(6)

    def worker(seed):
        try:
            barrier.wait(timeout=5)
            for i in range(40):
                bridge.emit_from_ga_locals_threadsafe(
                    _ctx(
                        _hfc_event="tool_before" if i % 2 == 0 else "llm_after",
                        tool_name=None if i % 3 == 0 else f"w{seed}_{i}",
                        index="x",
                        response=SimpleNamespace(content="z" * 1000, tool_calls=[]),
                    ),
                    CFG,
                )
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "worker 卡死"
    assert errors == []
    assert _wait_for(lambda: eng.count() >= 100), f"got {eng.count()}"
    # 全部事件被处理（40*6=240；tool_before 直发或批次，llm_after 合并但均尝试）
    assert eng.count() <= 240


def test_register_session_chat_capped():
    for i in range(5000):
        bridge.register_session_chat(f"session_{i}", f"oc_{i}")
    assert len(bridge._CHAT_BY_SESSION) <= 4096
    # 最新登记仍在（淘汰最旧）
    assert bridge._CHAT_BY_SESSION.get("session_4999") == "oc_4999"

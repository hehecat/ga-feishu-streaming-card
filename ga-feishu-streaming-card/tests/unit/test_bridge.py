"""bridge 单元测试：map_ga_ctx 映射 + 线程桥 fail-open。"""
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from ga_feishu_streaming_card import bridge
from ga_feishu_streaming_card.events import CardEvent, EventType

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
        client=object(),
    )
    base.update(kw)
    return base


def _wait_for(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
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


# ---------------- 映射：agent_before ----------------

def test_map_agent_before_started():
    evs = bridge.map_ga_ctx(_ctx())
    assert len(evs) == 1
    ev = evs[0]
    assert ev.type == EventType.MESSAGE_STARTED
    assert ev.conversation_id == "/tmp/ga/t1"
    assert ev.chat_id and ev.chat_id.startswith("chat_")
    assert ev.turn_id == "/tmp/ga/t1#t1"
    assert ev.data["input_preview"] == "hello"
    assert ev.data["max_turns"] == 40


def test_map_agent_before_explicit_event():
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="agent_before"))
    assert len(evs) == 1 and evs[0].type == EventType.MESSAGE_STARTED


def test_explicit_task_conversation_and_chat_override_fallbacks():
    ev = bridge.map_ga_ctx(_ctx(
        _hfc_event="agent_before",
        _hfc_conversation_id="default:fs-task-2",
        _hfc_chat_id="ou_receiver",
    ))[0]
    assert ev.conversation_id == "default:fs-task-2"
    assert ev.chat_id == "ou_receiver"
    assert ev.turn_id == "default:fs-task-2#t1"


# ---------------- 映射：tool ----------------

def test_map_tool_before_running():
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="tool_before", tool_name="code_run",
                                  args={"script": "print(1)", "_index": 3}, index=0, tool_num=1))
    assert len(evs) == 1
    ev = evs[0]
    assert ev.type == EventType.TOOL_UPDATED
    assert ev.data["status"] == "running"
    assert ev.data["tool_id"] == "tool_code_run_0"
    assert ev.data["args"] == {"script": "print(1)"}  # GA 注入键被剔除


def test_map_tool_after_completed():
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="tool_after", tool_name="code_run",
                                  index=0, ret="ok", error=None))
    ev = evs[0]
    assert ev.type == EventType.TOOL_UPDATED
    assert ev.data["status"] == "completed"
    assert ev.data["detail"] == "ok"
    assert ev.data["tool_id"] == "tool_code_run_0"


def test_map_tool_after_failed_on_error():
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="tool_after", tool_name="bash",
                                  index=1, ret=None, error="boom"))
    assert evs[0].data["status"] == "failed"
    assert evs[0].data["detail"] == "boom"


def test_map_tool_after_failed_on_exception_ret():
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="tool_after", tool_name="bash",
                                  index=1, ret=RuntimeError("x"), error=None))
    assert evs[0].data["status"] == "failed"


def test_map_tool_duration_from_monotonic():
    bridge.map_ga_ctx(_ctx(_hfc_event="tool_before", tool_name="t", index=0))
    time.sleep(0.03)
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="tool_after", tool_name="t", index=0, ret="ok"))
    assert evs[0].data["duration_ms"] is not None and evs[0].data["duration_ms"] >= 20


# ---------------- 真实 GA 工具帧键路径 ----------------
# GA dispatch 的 locals() 只有 tool_name/args/index/tool_num（agent_loop.py:78 不传 tid），
# 无 tool_id/tid/id 键 → bridge 必须以确定性 fallback tool_{name}_{index} 兜底，
# before/after 同帧计算同 id，原地更新语义成立。

def test_map_tool_real_ga_frame_keys_fallback():
    """真实 GA tool 帧（无任何 id 键）→ tool_id 为确定性 fallback，且 before/after 同 id"""
    before = bridge.map_ga_ctx(_ctx(_hfc_event="tool_before", tool_name="code_run",
                                    args={"script": "x"}, index=0, tool_num=1))[0]
    after = bridge.map_ga_ctx(_ctx(_hfc_event="tool_after", tool_name="code_run",
                                   index=0, ret="ok", error=None))[0]
    assert before.data["tool_id"] == "tool_code_run_0"
    assert after.data["tool_id"] == before.data["tool_id"]  # 同 id → 原地更新
    assert after.data["status"] == "completed"


def test_map_tool_tid_key_preferred():
    """防御：若 GA 未来在帧中提供调用 id（tid/id），bridge 优先采用真实 id"""
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="tool_before", tool_name="code_run",
                                 index=2, tid="call_abc123"))
    assert evs[0].data["tool_id"] == "call_abc123"
    evs2 = bridge.map_ga_ctx(_ctx(_hfc_event="tool_after", tool_name="code_run",
                                  index=2, id="call_abc123", ret="ok"))
    assert evs2[0].data["tool_id"] == "call_abc123"


# ---------------- 映射：llm / turn / agent ----------------

def test_map_llm_after_delta():
    resp = SimpleNamespace(content="你好，世界", tool_calls=[1, 2], model="gpt-x")
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="llm_after", response=resp))
    ev = evs[0]
    assert ev.type == EventType.ANSWER_DELTA
    assert ev.data["text"] == "你好，世界"
    assert ev.data["summary"]["tool_calls"] == 2


def test_map_turn_after_notice_continue():
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="turn_after", tool_calls=[], tool_results=[],
                                  next_prompt="继续", exit_reason=None))
    ev = evs[0]
    assert ev.type == EventType.SYSTEM_NOTICE
    assert ev.data["status"] == "continue"


def test_map_turn_after_notice_exit():
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="turn_after", tool_calls=[], tool_results=[],
                                  next_prompt=None, exit_reason={"result": "CURRENT_TASK_DONE"}))
    assert evs[0].data["status"] == "exit"


def test_map_agent_after_completed():
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="agent_after",
                                 exit_reason={"result": "CURRENT_TASK_DONE"}))
    assert evs[0].type == EventType.MESSAGE_COMPLETED


def test_map_agent_after_failed_exceeded():
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="agent_after",
                                 exit_reason={"result": "MAX_TURNS_EXCEEDED"}))
    assert evs[0].type == EventType.MESSAGE_FAILED


def test_map_agent_after_failed_exhausted_inferred():
    # while 循环耗尽（hook 时 exit_reason 仍为 {}）→ 由 turn >= max_turns 推断失败
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="agent_after", turn=40, max_turns=40, exit_reason={}))
    assert evs[0].type == EventType.MESSAGE_FAILED
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="agent_after", turn=5, max_turns=40, exit_reason={}))
    assert evs[0].type == EventType.MESSAGE_COMPLETED


def test_map_unknown_event_empty():
    assert bridge.map_ga_ctx(_ctx(_hfc_event="foo")) == []
    assert bridge.map_ga_ctx(_ctx(_hfc_event="turn_before")) == []
    assert bridge.map_ga_ctx(_ctx(_hfc_event="llm_before")) == []


def test_map_non_dict_empty():
    assert bridge.map_ga_ctx(None) == []
    assert bridge.map_ga_ctx("x") == []


def test_map_infer_event_best_effort():
    # 无 _hfc_event 时的兜底推断
    evs = bridge.map_ga_ctx({"tool_name": "t", "index": 0, "ret": "ok"})
    assert evs[0].type == EventType.TOOL_UPDATED and evs[0].data["status"] == "completed"


# ---------------- chat_id 优先级（§2） ----------------

def test_chat_id_ctx_wins():
    ev = bridge.map_ga_ctx(_ctx(chat_id="ctx_chat"))[0]
    assert ev.chat_id == "ctx_chat"


def test_chat_id_env(monkeypatch):
    monkeypatch.setenv("HFC_CHAT_ID", "env_chat")
    ev = bridge.map_ga_ctx(_ctx())[0]
    assert ev.chat_id == "env_chat"


def test_chat_id_registered_mapping():
    bridge.register_session_chat("/tmp/ga/t1", "map_chat")
    ev = bridge.map_ga_ctx(_ctx())[0]
    assert ev.chat_id == "map_chat"


def test_task_conversation_uses_base_session_mapping():
    bridge.register_session_chat("default", "ou_receiver")
    ev = bridge.map_ga_ctx(_ctx(_hfc_conversation_id="default:fs-task-2"))[0]
    assert ev.conversation_id == "default:fs-task-2"
    assert ev.chat_id == "ou_receiver"


def test_chat_id_fallback_deterministic():
    a = bridge.map_ga_ctx(_ctx(handler=SimpleNamespace(parent=SimpleNamespace(task_dir="/x"))))[0]
    b = bridge.map_ga_ctx(_ctx(handler=SimpleNamespace(parent=SimpleNamespace(task_dir="/x"))))[0]
    c = bridge.map_ga_ctx(_ctx(handler=SimpleNamespace(parent=SimpleNamespace(task_dir="/y"))))[0]
    assert a.chat_id == b.chat_id != c.chat_id


# ---------------- 序列单调 ----------------

def test_sequence_monotonic():
    s1 = bridge.map_ga_ctx(_ctx(_hfc_event="agent_before"))[0].sequence
    s2 = bridge.map_ga_ctx(_ctx(_hfc_event="tool_before", tool_name="t", index=0))[0].sequence
    assert s2 > s1

# ---------------- 线程+队列桥 emit ----------------

def test_emit_threadsafe_delivers(fake_engine):
    bridge.emit_from_ga_locals_threadsafe(
        _ctx(_hfc_event="tool_before", tool_name="code_run", args={}, index=0), CFG)
    assert _wait_for(lambda: len(fake_engine.events) == 1)
    assert fake_engine.events[0].type == EventType.TOOL_UPDATED
    assert fake_engine.events[0].data["status"] == "running"


def test_emit_preserves_order(fake_engine):
    for ev_name, kw in (("agent_before", {}), ("tool_before", {"tool_name": "t", "index": 0}),
                        ("agent_after", {"exit_reason": {"result": "CURRENT_TASK_DONE"}})):
        bridge.emit_from_ga_locals_threadsafe(_ctx(_hfc_event=ev_name, **kw), CFG)
    assert _wait_for(lambda: len(fake_engine.events) == 3)
    assert [e.type for e in fake_engine.events] == [
        EventType.MESSAGE_STARTED, EventType.TOOL_UPDATED, EventType.MESSAGE_COMPLETED]


def test_emit_disabled_no_events(fake_engine):
    off = SimpleNamespace(enabled=False, coalesce=SimpleNamespace(max_pending=128))
    bridge.emit_from_ga_locals_threadsafe(_ctx(_hfc_event="agent_before"), off)
    time.sleep(0.2)
    assert fake_engine.events == []


def test_emit_never_raises(fake_engine):
    bridge.emit_from_ga_locals_threadsafe(None, CFG)          # 非 dict ctx → 不投递
    bridge.emit_from_ga_locals_threadsafe(_ctx(_hfc_event="agent_before"), None)  # cfg=None → fail-open 默认启用
    bridge.emit_from_ga_locals_threadsafe(_ctx(_hfc_event="agent_before"), CFG)
    assert _wait_for(lambda: len(fake_engine.events) >= 1)    # 后两次调用至少投递 1 条


def test_host_sdk_transport_serializes_card_and_uses_chat_id():
    calls = []
    host = SimpleNamespace(
        _send_raw=lambda *args: calls.append(("send", args)) or "om_prod",
        _patch_card=lambda *args: calls.append(("patch", args)) or True,
    )
    transport = bridge._host_sdk_transport(host)
    card = {"header": {"title": {"content": "真实卡片"}}}

    sent = transport.send_card("oc_prod", card)
    sent_to_user = transport.send_card("ou_prod", card)
    updated = transport.update_card("om_prod", card)

    assert sent.message_id == "om_prod" and sent.outcome == "delivered"
    assert sent_to_user.message_id == "om_prod" and sent_to_user.outcome == "delivered"
    assert updated.outcome == "updated"
    assert calls[0][1][0] == "oc_prod"
    assert calls[0][1][2:] == ("interactive", "chat_id")
    assert '"真实卡片"' in calls[0][1][1]
    assert calls[1][1][0] == "ou_prod"
    assert calls[1][1][2:] == ("interactive", "open_id")
    assert calls[2][1][0] == "om_prod"
    assert '"真实卡片"' in calls[2][1][1]


def test_host_sdk_transport_requires_both_functions():
    assert bridge._host_sdk_transport(SimpleNamespace()) is None
    assert bridge._host_sdk_transport(SimpleNamespace(_send_raw=lambda *_: "om")) is None


def test_emit_engine_error_fail_open(monkeypatch):
    fe = FakeEngine()

    def boom(cfg):
        raise RuntimeError("engine broken")

    monkeypatch.setattr(bridge, "_make_engine", boom)
    bridge.emit_from_ga_locals_threadsafe(_ctx(_hfc_event="agent_before"), CFG)
    time.sleep(0.3)
    assert fe.events == []
    assert bridge.metrics().get("errors", 0) >= 0


def test_shutdown_and_reemit(fake_engine):
    bridge.shutdown()
    bridge.emit_from_ga_locals_threadsafe(_ctx(_hfc_event="agent_before"), CFG)
    assert _wait_for(lambda: len(fake_engine.events) == 1)
    bridge.shutdown()
    bridge.shutdown()  # 幂等


def test_metrics_emitted_handled(fake_engine):
    bridge.emit_from_ga_locals_threadsafe(_ctx(_hfc_event="agent_before"), CFG)
    assert _wait_for(lambda: len(fake_engine.events) == 1)
    m = bridge.metrics()
    assert m["emitted"] >= 1 and m["handled"] >= 1


# ---------------- T5: llm_after usage → tokens 载荷 ----------------

def test_usage_payload_dict_passthrough():
    assert bridge._usage_payload({"input_tokens": 10, "output_tokens": 20}) == \
        {"input_tokens": 10, "output_tokens": 20}
    assert bridge._usage_payload({"prompt_tokens": 7, "completion_tokens": 8}) == \
        {"prompt_tokens": 7, "completion_tokens": 8}


def test_usage_payload_object_normalize():
    u = SimpleNamespace(input_tokens=11, output_tokens=22)
    assert bridge._usage_payload(u) == {"input_tokens": 11, "output_tokens": 22}
    u2 = SimpleNamespace(prompt_tokens=3, completion_tokens=4)
    assert bridge._usage_payload(u2) == {"input_tokens": 3, "output_tokens": 4}
    assert bridge._usage_payload(SimpleNamespace(foo=1)) is None


def test_usage_payload_none_and_empty():
    assert bridge._usage_payload(None) is None
    assert bridge._usage_payload({}) is None
    assert bridge._usage_payload(SimpleNamespace()) is None


def test_map_llm_after_includes_usage():
    resp = SimpleNamespace(content="hi", tool_calls=[], model="gpt-x",
                           usage=SimpleNamespace(input_tokens=1200, output_tokens=3400))
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="llm_after", response=resp))
    assert evs[0].data["usage"] == {"input_tokens": 1200, "output_tokens": 3400}
    assert evs[0].data["summary"]["model"] == "gpt-x"


def test_map_llm_after_no_usage_none():
    resp = SimpleNamespace(content="hi", tool_calls=[], model="gpt-x")
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="llm_after", response=resp))
    assert evs[0].data["usage"] is None

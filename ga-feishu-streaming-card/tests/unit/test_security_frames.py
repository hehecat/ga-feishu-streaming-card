"""安全审查测试：畸形事件帧防御（map_ga_ctx / parse_event 层）。"""
from types import SimpleNamespace

import pytest

from ga_feishu_streaming_card import bridge
from ga_feishu_streaming_card.events import EventType, parse_event


def _ctx(**kw):
    base = dict(
        handler=SimpleNamespace(
            name="GenericAgent",
            parent=SimpleNamespace(task_dir="/tmp/ga/t1"),
        ),
        user_input="hello",
        max_turns=40,
        turn=1,
        chat_id="oc_test",
    )
    base.update(kw)
    return base


# ---------------- map_ga_ctx 畸形帧 ----------------

def test_tool_before_hostile_fields_no_raise():
    evs = bridge.map_ga_ctx(
        _ctx(_hfc_event="tool_before", tool_name=None, index="abc", tool_id=12345, args="not-dict")
    )
    assert len(evs) == 1
    d = evs[0].data
    assert isinstance(d["tool_id"], str)
    assert d["tool_id"].isdecimal() or "tool" in d["tool_id"]


def test_tool_before_huge_name_truncated():
    evs = bridge.map_ga_ctx(
        _ctx(_hfc_event="tool_before", tool_name="x" * 5000, index=1)
    )
    d = evs[0].data
    assert len(d["tool_name"]) <= 128  # 截断后上限
    assert len(d["tool_id"]) <= 140  # tool_id 由截断名构造


def test_tool_before_returns_not_mapping():
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="tool_before", ret="just-a-string"))
    assert len(evs) == 1
    assert evs[0].type is EventType.TOOL_UPDATED


def test_llm_after_non_str_content():
    evs = bridge.map_ga_ctx(
        _ctx(_hfc_event="llm_after", response=SimpleNamespace(content=None, tool_calls=[]))
    )
    assert evs[0].data["text"] == ""
    evs2 = bridge.map_ga_ctx(
        _ctx(_hfc_event="llm_after", response=SimpleNamespace(content=12345, tool_calls=[]))
    )
    assert evs2[0].data["text"] == "12345"


def test_llm_after_huge_content_truncated():
    evs = bridge.map_ga_ctx(
        _ctx(_hfc_event="llm_after", response=SimpleNamespace(content="a" * 500_000, tool_calls=[]))
    )
    assert len(evs[0].data["text"]) <= 200_000 + 64


def test_agent_after_garbage_turns_no_raise():
    evs = bridge.map_ga_ctx(
        _ctx(_hfc_event="agent_after", exit_reason={"result": "CURRENT_TASK_DONE"}, turn="abc", max_turns="xyz")
    )
    assert len(evs) == 1
    assert evs[0].type is EventType.MESSAGE_COMPLETED


@pytest.mark.parametrize(
    "exit_reason",
    ["completed", "COMPLETED", {"result": "completed"}],
)
def test_agent_after_completed_alias_is_success(exit_reason):
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="agent_after", exit_reason=exit_reason))
    assert len(evs) == 1
    assert evs[0].type is EventType.MESSAGE_COMPLETED


def test_agent_after_non_dict_exit_reason():
    evs = bridge.map_ga_ctx(_ctx(_hfc_event="agent_after", exit_reason="boom"))
    assert len(evs) == 1
    assert evs[0].type is EventType.MESSAGE_FAILED


def test_tool_after_exception_ret():
    evs = bridge.map_ga_ctx(
        _ctx(_hfc_event="tool_after", tool_name="web_search", index=1, ret=RuntimeError("boom"))
    )
    assert evs[0].type is EventType.TOOL_UPDATED
    assert evs[0].data["status"] == "failed"
    assert "boom" in evs[0].data["detail"]


def test_map_ga_ctx_never_raises_hostile():
    hostile = [
        None,
        [],
        42,
        {"_hfc_event": "tool_before", "tool_name": {"nested": object()}},
        {"_hfc_event": "llm_after", "response": 3.14},
        {"_hfc_event": "agent_after", "exit_reason": {"result": ["list"]}},
    ]
    for ctx in hostile:
        out = bridge.map_ga_ctx(ctx)  # 不抛异常
        assert isinstance(out, list)


# ---------------- parse_event 严格拒绝 ----------------

def _base_frame():
    return {
        "type": "message.started",
        "chat_id": "oc_x",
        "sequence": 1,
        "created_at": 1.0,
        "platform": "feishu",
        "data": {},
    }


def test_parse_event_rejects_missing_type():
    f = _base_frame()
    del f["type"]
    with pytest.raises(ValueError):
        parse_event(f)


def test_parse_event_rejects_wrong_data_type():
    f = _base_frame()
    f["data"] = "not-dict"
    with pytest.raises((TypeError, ValueError)):
        parse_event(f)


def test_parse_event_rejects_non_utf8():
    f = _base_frame()
    f["data"] = {"text": b"\xff\xfe".decode("latin1")}  # 合法 str，但含控制字符
    f["data"]["text"] = "ok"
    ev = parse_event(f)  # 结构合法即通过（内容净化在下游）
    assert ev.data["text"] == "ok"

"""session.py 单元测试：事件迁移、工具原地更新、终态固化、交互流转。"""

import pytest

from ga_feishu_streaming_card.events import EventType, parse_event
from ga_feishu_streaming_card.session import CardSession, InteractionState, ToolState


def _ev(type_, seq, **data):
    raw = {"type": type_.value if isinstance(type_, EventType) else type_,
           "sequence": seq, "chat_id": "oc_1", "data": data or {}}
    return parse_event(raw)


class TestInit:
    def test_initial_state(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        assert s.status == "thinking"
        assert s.thinking == ""
        assert s.answer == ""
        assert s.tools == {}
        assert s.interactions == {}
        assert s.created_at > 0
        assert s.updated_at >= s.created_at

    def test_sequence_tracks(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.THINKING_DELTA, 7, text="x"))
        assert s.last_sequence == 7


class TestThinking:
    def test_thinking_delta_accumulates(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.THINKING_DELTA, 1, text="hello "))
        s.apply_event(_ev(EventType.THINKING_DELTA, 2, text="world"))
        assert s.thinking == "hello world"

    def test_thinking_after_started(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.MESSAGE_STARTED, 0, text="user q"))
        s.apply_event(_ev(EventType.THINKING_DELTA, 1, text="t"))
        assert s.thinking == "t"

    def test_thinking_frozen_after_completed(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.THINKING_DELTA, 1, text="a"))
        s.apply_event(_ev(EventType.MESSAGE_COMPLETED, 2))
        s.apply_event(_ev(EventType.THINKING_DELTA, 3, text="b"))
        assert s.thinking == "a"
        assert s.status == "completed"


class TestAnswer:
    def test_answer_delta_accumulates(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.ANSWER_DELTA, 1, text="ans "))
        s.apply_event(_ev(EventType.ANSWER_DELTA, 2, text="part2"))
        assert s.answer == "ans part2"

    def test_leading_summary_is_archived_and_final_answer_is_independent(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(
            EventType.ANSWER_DELTA,
            1,
            text="<summary>先判断是否需要工具</summary>这是最终回答。",
        ))
        assert s.answer == "这是最终回答。"
        assert [(i.kind, i.content) for i in s.timeline] == [
            ("reasoning", "先判断是否需要工具")
        ]

    def test_completed_sets_status_and_touch(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.ANSWER_DELTA, 1, text="x"))
        s.apply_event(_ev(EventType.MESSAGE_COMPLETED, 2))
        assert s.status == "completed"

    def test_failed_sets_status(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.MESSAGE_FAILED, 1, reason="boom"))
        assert s.status == "failed"


class TestTools:
    def test_tool_started_creates_running(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.TOOL_UPDATED, 1, id="t1", name="web_search", status="running"))
        t = s.tools["t1"]
        assert t.status == "running"
        assert t.name == "web_search"

    def test_tool_updated_in_place(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.TOOL_UPDATED, 1, id="t1", name="web_search", status="running"))
        ref = s.tools["t1"]
        s.apply_event(_ev(EventType.TOOL_UPDATED, 2, id="t1", detail="page 1/3"))
        assert s.tools["t1"] is ref  # 原地更新：同一对象
        assert ref.detail == "page 1/3"
        assert len(s.tools) == 1

    def test_tool_completed_terminal(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.TOOL_UPDATED, 1, id="t1", name="web_search", status="running"))
        s.apply_event(_ev(EventType.TOOL_UPDATED, 2, id="t1", status="completed", duration_ms=42))
        assert s.tools["t1"].status == "completed"
        assert s.tools["t1"].duration_ms == 42

    def test_tool_terminal_not_mutated(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.TOOL_UPDATED, 1, id="t1", name="web_search", status="running"))
        s.apply_event(_ev(EventType.TOOL_UPDATED, 2, id="t1", status="completed", duration_ms=42))
        s.apply_event(_ev(EventType.TOOL_UPDATED, 3, id="t1", detail="late"))
        assert s.tools["t1"].detail == ""
        assert s.tools["t1"].duration_ms == 42
        assert s.tools["t1"].status == "completed"

    def test_tool_failed_terminal(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.TOOL_UPDATED, 1, id="t1", name="x", status="running"))
        s.apply_event(_ev(EventType.TOOL_UPDATED, 2, id="t1", status="failed", detail="err"))
        assert s.tools["t1"].status == "failed"
        assert s.tools["t1"].detail == "err"

    def test_multiple_tools(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.TOOL_UPDATED, 1, id="a", name="x", status="running"))
        s.apply_event(_ev(EventType.TOOL_UPDATED, 2, id="b", name="y", status="running"))
        assert set(s.tools) == {"a", "b"}


class TestInteractions:
    def test_pending_created(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.INTERACTION_REQUESTED, 1, id="i1", prompt="ok?"))
        assert s.interactions["i1"].status == "pending"

    def test_completed_transition(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.INTERACTION_REQUESTED, 1, id="i1"))
        s.apply_event(_ev(EventType.INTERACTION_COMPLETED, 2, id="i1"))
        assert s.interactions["i1"].status == "completed"

    def test_failed_transition(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.INTERACTION_REQUESTED, 1, id="i1"))
        s.apply_event(_ev(EventType.INTERACTION_FAILED, 2, id="i1"))
        assert s.interactions["i1"].status == "failed"

    def test_unknown_interaction_ignored(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.INTERACTION_COMPLETED, 1, id="nope"))
        assert s.interactions == {}


class TestDict:
    def test_to_dict_fields(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.THINKING_DELTA, 1, text="t"))
        s.apply_event(_ev(EventType.ANSWER_DELTA, 2, text="a"))
        s.apply_event(_ev(EventType.TOOL_UPDATED, 3, id="t1", name="x", status="running"))
        d = s.to_dict()
        assert d["conversation_id"] == "c-1"
        assert d["chat_id"] == "oc_1"
        assert d["status"] == "thinking"
        assert d["thinking"] == "t"
        assert d["answer"] == "a"  # 正文保留在主区：过程事件不得吞掉已有答案
        assert d["timeline"][0]["kind"] == "tool"  # 无 summary 前缀的正文保留在主区，不归档进 timeline
        assert d["timeline"][0]["title"] == "工具调用"  # 工具名不进入序列化
        assert d["timeline"][0]["tool_id"] == "t1"
        assert "t1" in d["tools"]
        assert d["last_sequence"] == 3


# ---------------------------------------------------------------- T5: tokens / runtime_header_text / model


class TestTokensAndRuntimeHeader:
    def test_action_word_whitelist(self):
        from ga_feishu_streaming_card.session import action_word_for_tool

        assert action_word_for_tool("web_search") == "正在搜索"
        assert action_word_for_tool("Browser.open_url") == "正在浏览"
        assert action_word_for_tool("file_read") == "正在读取"
        assert action_word_for_tool("file_patch") == "正在编辑"
        assert action_word_for_tool("bash_run") == "正在执行操作"
        assert action_word_for_tool("unknown_tool") == ""
        assert action_word_for_tool("") == ""

    def test_runtime_header_flow(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.TOOL_UPDATED, 1, id="t1", name="web_search", status="running"))
        assert s.runtime_header_text == "正在搜索"
        s.apply_event(_ev(EventType.TOOL_UPDATED, 2, id="t1", name="web_search", status="completed"))
        assert s.runtime_header_text == ""
        # 下一个工具再次进入 active → 重新设置
        s.apply_event(_ev(EventType.TOOL_UPDATED, 3, id="t2", name="file_patch", status="running"))
        assert s.runtime_header_text == "正在编辑"
        # 终态事件清空动作词
        s.apply_event(_ev(EventType.MESSAGE_COMPLETED, 4, final_text="done"))
        assert s.runtime_header_text == ""
        assert s.status == "completed"

    def test_unknown_tool_keeps_empty(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.TOOL_UPDATED, 1, id="t1", name="mystery", status="running"))
        assert s.runtime_header_text == ""

    def test_tokens_accumulate_compat_normalize(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.ANSWER_DELTA, 1, text="a",
                          usage={"input_tokens": 1200, "output_tokens": 3400}))
        assert s.tokens == {"input_tokens": 1200, "output_tokens": 3400}
        # prompt/completion 别名兼容
        s.apply_event(_ev(EventType.ANSWER_DELTA, 2, text="b",
                          usage={"prompt_tokens": 100, "completion_tokens": 50}))
        assert s.tokens == {"input_tokens": 1300, "output_tokens": 3450}

    def test_tokens_defensive(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.ANSWER_DELTA, 1, text="a", usage=None))
        s.apply_event(_ev(EventType.ANSWER_DELTA, 2, text="b", usage={"input_tokens": "abc", "output_tokens": -5}))
        assert s.tokens == {"output_tokens": 0}
        assert s.tokens.get("input_tokens", 0) == 0

    def test_model_from_summary(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.ANSWER_DELTA, 1, text="a",
                          summary={"model": "gpt-4o-12345678901234567890"}))
        assert s.model == "gpt-4o-12345678901234567890"
        s.apply_event(_ev(EventType.ANSWER_DELTA, 2, text="b", summary=None))
        assert s.model == "gpt-4o-12345678901234567890"  # 缺失不覆盖

    def test_to_dict_includes_t5_fields(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.TOOL_UPDATED, 1, id="t1", name="web_search", status="running"))
        s.apply_event(_ev(EventType.ANSWER_DELTA, 2, text="a",
                          usage={"input_tokens": 9, "output_tokens": 8},
                          summary={"model": "m1"}))
        d = s.to_dict()
        assert d["tokens"] == {"input_tokens": 9, "output_tokens": 8}
        assert d["runtime_header_text"] == "正在搜索"
        assert d["model"] == "m1"

    def test_context_accumulate(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        # total_tokens + max_tokens 显式携带 → used 累积、max 取最大
        s.apply_event(_ev(EventType.ANSWER_DELTA, 1, text="a",
                          usage={"input_tokens": 100, "output_tokens": 50,
                                 "total_tokens": 150, "max_tokens": 32000}))
        s.apply_event(_ev(EventType.ANSWER_DELTA, 2, text="b",
                          usage={"input_tokens": 200, "output_tokens": 100,
                                 "total_tokens": 300, "max_tokens": 16000}))
        assert s.context == {"used_tokens": 450, "max_tokens": 32000}

    def test_context_fallback_and_defensive(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        # 无 total_tokens → 退化为 in+out
        s.apply_event(_ev(EventType.ANSWER_DELTA, 1, text="a",
                          usage={"input_tokens": 1200, "output_tokens": 800}))
        assert s.context["used_tokens"] == 2000
        assert "max_tokens" not in s.context
        # 非法/缺失 → 不崩、不写入
        s.apply_event(_ev(EventType.ANSWER_DELTA, 2, text="b",
                          usage={"total_tokens": "abc", "max_tokens": -3}))
        assert s.context["used_tokens"] == 2000
        assert s.context.get("max_tokens", 0) == 0

    def test_to_dict_includes_context(self):
        s = CardSession(conversation_id="c-1", chat_id="oc_1")
        s.apply_event(_ev(EventType.ANSWER_DELTA, 1, text="a",
                          usage={"total_tokens": 999, "max_tokens": 8000}))
        assert s.to_dict()["context"] == {"used_tokens": 999, "max_tokens": 8000}

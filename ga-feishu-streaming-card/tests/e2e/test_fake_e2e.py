"""离线 fake 端到端测试。

链路：GA locals() 快照序列 → bridge.map_ga_ctx → 协议事件流
      → CardEngine(FakeTransport) → render_card → transport.calls。

覆盖：
1. bridge 映射：GA 8 事件快照（键清单内嵌于 ``ga_frame``）→ 协议事件类型序列；
2. 引擎全链路：message.started → thinking.delta(累积) → tool.updated(同 id 原地)
   → answer.delta → message.completed，断言 FakeTransport 收到的卡片消息序列与卡片内容演化；
3. fail-open：transport 抛错不阻塞状态应用；
4. cancel / 失败路径：工具取消 + message.failed 渲染。

说明：当前 GA Hook 不提供流式 thinking 帧，bridge 因而不产 thinking.delta；
测试在协议层注入 THINKING_DELTA，以模拟后续流式扩展。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from ga_feishu_streaming_card import bridge
from ga_feishu_streaming_card.config import EngineConfig
from ga_feishu_streaming_card.engine import CardEngine
from ga_feishu_streaming_card.events import CardEvent, EventType
from ga_feishu_streaming_card.transport import FakeTransport

TASK_DIR = "e2e_task"
CHAT_ID = "e2e_chat"


# ---------------------------------------------------------------- 工具

def _handler(name: str = "demo") -> SimpleNamespace:
    return SimpleNamespace(name=name, parent=SimpleNamespace(task_dir=TASK_DIR))


def _resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=[], model="fake-model")


def ga_frame(event: str, **extra: Any) -> Dict[str, Any]:
    """构造真实形状的 GA ``locals()`` 快照。

    键集合根据 GA ``agent_loop.py`` 的实际 hook 调用点确定；工具事件来自 ``dispatch`` 作用域，
    其余事件来自 ``agent_runner_loop`` 作用域。``_hfc_event`` 是插件回调
    注入给 bridge 的唯一非 GA locals 键。
    """
    handler = _handler()
    if event in {"tool_before", "tool_after"}:
        frame: Dict[str, Any] = {
            "_hfc_event": event,
            "self": handler,
            "tool_name": "code_run",
            "args": {"script": "print(1)"},
            "response": _resp("调用工具"),
            "index": 0,
            "tool_num": 1,
        }
        if event == "tool_after":
            frame["ret"] = "1"
    else:
        frame = {
            "_hfc_event": event,
            "client": SimpleNamespace(name="fake-client"),
            "system_prompt": "你是助手",
            "user_input": "写个 Python 脚本",
            "handler": handler,
            "tools_schema": [],
            "max_turns": 3,
            "verbose": True,
            "initial_user_content": None,
            "yield_info": False,
            "messages": [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "写个 Python 脚本"},
            ],
        }
        if event in {"turn_before", "llm_before", "llm_after", "turn_after", "agent_after"}:
            frame.update({"turn": 1, "turnstr": "LLM Running (Turn 1) ...", "response": None})
        if event in {"llm_after", "turn_after", "agent_after"}:
            frame["response"] = _resp("答案内容")
        if event in {"turn_after", "agent_after"}:
            frame.update({
                "tool_calls": [{"name": "code_run"}],
                "tool_results": ["1"],
                "next_prompt": "继续",
                "exit_reason": None,
                "next_prompts": {"继续"},
            })
        if event == "agent_after":
            frame.update({"next_prompt": "", "exit_reason": {"result": "CURRENT_TASK_DONE"}})
    frame.update(extra)
    return frame


def proto_ev(type_: EventType, seq: int, data: Dict[str, Any]) -> CardEvent:
    """构造协议事件（引擎层直喂，模拟 bridge 输出）。"""
    return CardEvent(
        type=type_,
        sequence=seq,
        created_at=100.0 + seq,
        conversation_id=TASK_DIR,
        chat_id=CHAT_ID,
        data=data,
    )


def _card_text(card: Dict[str, Any]) -> str:
    """拼接卡片顶层 markdown 元素文本（终答正文层；兼容 1.0 elements / 2.0 body.elements）。"""
    elements = (card.get("body") or {}).get("elements") or card.get("elements") or []
    return "\n".join(
        el.get("content", "") for el in elements if el.get("tag") == "markdown"
    )


def _all_text(card: Dict[str, Any]) -> str:
    """递归拼接卡片全部 markdown 文本（含折叠面板内），用于内容断言。"""
    parts: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") == "markdown" and isinstance(node.get("content"), str):
                parts.append(node["content"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(card)
    return "\n".join(parts)


def _header(card: Dict[str, Any]) -> str:
    return (card.get("header") or {}).get("title", {}).get("content", "")


def _ops(t: FakeTransport) -> List[str]:
    return [c["op"] for c in t.calls]


def _update_msg_ids(t: FakeTransport) -> List[str]:
    return [c["message_id"] for c in t.calls if c["op"] == "update_card"]


# ---------------------------------------------------------------- bridge 映射

class TestBridgeMapping:
    """GA locals 快照 → 协议事件流（bridge.map_ga_ctx 纯函数）。"""

    def test_agent_before_maps_to_message_started(self):
        evs = bridge.map_ga_ctx(ga_frame("agent_before"))
        assert [e.type for e in evs] == [EventType.MESSAGE_STARTED]
        assert evs[0].data["input_preview"] == "写个 Python 脚本"
        assert evs[0].data["max_turns"] == 3

    def test_tool_before_after_map_to_tool_updated_inplace(self):
        before = bridge.map_ga_ctx(ga_frame("tool_before"))[0]
        after = bridge.map_ga_ctx(ga_frame("tool_after"))[0]
        assert before.type is EventType.TOOL_UPDATED and before.data["status"] == "running"
        assert after.type is EventType.TOOL_UPDATED and after.data["status"] == "completed"
        assert before.data["tool_id"] == after.data["tool_id"]  # 同 id → 原地更新
        assert after.data["detail"] == "1"

    def test_llm_after_maps_to_answer_delta(self):
        evs = bridge.map_ga_ctx(ga_frame("llm_after"))
        assert [e.type for e in evs] == [EventType.ANSWER_DELTA]
        assert evs[0].data["text"] == "答案内容"

    def test_turn_after_maps_to_system_notice(self):
        evs = bridge.map_ga_ctx(ga_frame("turn_after"))
        assert [e.type for e in evs] == [EventType.SYSTEM_NOTICE]
        assert evs[0].data["status"] == "continue"

    def test_agent_after_maps_to_completed(self):
        evs = bridge.map_ga_ctx(ga_frame("agent_after"))
        assert [e.type for e in evs] == [EventType.MESSAGE_COMPLETED]

    def test_agent_after_failure_maps_to_failed(self):
        frame = ga_frame("agent_after", exit_reason={"result": "MAX_TURNS_EXCEEDED"})
        evs = bridge.map_ga_ctx(frame)
        assert [e.type for e in evs] == [EventType.MESSAGE_FAILED]

    def test_full_ga_sequence_maps_expected_protocol_sequence(self):
        """GA 事件序列（agent_before/turn_before/llm_after/tool_updated×2/answer_delta/turn_after/agent_after）。"""
        seq = [
            ga_frame("agent_before"),
            ga_frame("turn_before"),  # 不产生协议事件
            ga_frame("llm_after"),
            ga_frame("tool_before"),  # tool.updated running
            ga_frame("tool_after"),  # tool.updated completed
            ga_frame("llm_after"),  # answer.delta
            ga_frame("turn_after"),  # system.notice
            ga_frame("agent_after"),  # message.completed
        ]
        types = []
        for frame in seq:
            types.extend(e.type for e in bridge.map_ga_ctx(frame))
        assert types == [
            EventType.MESSAGE_STARTED,
            EventType.ANSWER_DELTA,
            EventType.TOOL_UPDATED,
            EventType.TOOL_UPDATED,
            EventType.ANSWER_DELTA,
            EventType.SYSTEM_NOTICE,
            EventType.MESSAGE_COMPLETED,
        ]
        # 同一会话 chat_id 确定性一致
        all_ids = {e.chat_id for e in bridge.map_ga_ctx(ga_frame("agent_before"))}
        assert all_ids and all(chat_id.startswith("chat_") for chat_id in all_ids)


# ---------------------------------------------------------------- 真正的 bridge → engine 联合链路

class TestBridgeEngineIntegratedChain:
    def test_real_ga_locals_flow_through_bridge_engine_and_transport(self):
        """不手工构造协议事件，验证 GA locals → bridge → engine → card。"""
        frames = [
            ga_frame("agent_before"),
            ga_frame("llm_after", response=_resp("<summary>第一段过程</summary>")),
            ga_frame("tool_before"),
            ga_frame("tool_after"),
            ga_frame("llm_after", response=_resp("<summary>第二段过程</summary>第二段正文")),
            ga_frame("agent_after"),
        ]
        transport = FakeTransport()
        engine = CardEngine(cfg=EngineConfig(), transport=transport)
        mapped: List[CardEvent] = []
        for frame in frames:
            events = bridge.map_ga_ctx(frame)
            mapped.extend(events)
            for event in events:
                result = engine.handle_event(event)
                assert result.applied is True, (event.type, result.reason)

        assert [event.type for event in mapped] == [
            EventType.MESSAGE_STARTED,
            EventType.ANSWER_DELTA,
            EventType.TOOL_UPDATED,
            EventType.TOOL_UPDATED,
            EventType.ANSWER_DELTA,
            EventType.MESSAGE_COMPLETED,
        ]
        assert _ops(transport) == ["send_card"] + ["update_card"] * 5
        assert _update_msg_ids(transport) == ["fake_msg_1"] * 5
        final_text = _card_text(transport.calls[-1]["card"])
        # 过程文本进入折叠面板，顶层只保留独立终答
        assert "第二段" in final_text
        assert "第一段" not in final_text
        assert "第一段第二段" not in final_text
        all_text = _all_text(transport.calls[-1]["card"])
        assert "第一段" in all_text and "第二段" in all_text
        assert "工具调用" in all_text  # 通用文案可见
        assert "code_run" not in all_text  # 工具名不泄露
        assert "print(1)" not in all_text  # 工具参数不泄露
        assert "<summary>" not in all_text
        assert "tool_code_run_0" not in final_text  # 工具状态只供内部生命周期使用
        assert _header(transport.calls[-1]["card"]) == "已完成"

    def test_two_fsapp_tasks_each_create_and_complete_a_new_card(self):
        """连续两条用户消息必须各用新会话，不能复用首轮 completed CardSession。"""
        transport = FakeTransport()
        engine = CardEngine(cfg=EngineConfig(), transport=transport)

        for task_id, answer in (("fs-task-1", "第一轮答案"), ("fs-task-2", "第二轮答案")):
            overrides = {
                "_hfc_conversation_id": f"default:{task_id}",
                "_hfc_chat_id": CHAT_ID,
            }
            frames = [
                ga_frame("agent_before", **overrides),
                ga_frame("llm_after", response=_resp(answer), **overrides),
                ga_frame("agent_after", response=_resp(answer), **overrides),
            ]
            for frame in frames:
                for event in bridge.map_ga_ctx(frame):
                    result = engine.handle_event(event)
                    assert result.applied is True, (task_id, event.type, result.reason)

        assert _ops(transport) == [
            "send_card", "update_card", "update_card",
            "send_card", "update_card", "update_card",
        ]
        assert set(engine.sessions) == {"default:fs-task-1", "default:fs-task-2"}
        assert all(session.status == "completed" for session in engine.sessions.values())
        first_final = _card_text(transport.calls[2]["card"])
        second_final = _card_text(transport.calls[5]["card"])
        assert "第一轮答案" in first_final and "第二轮答案" not in first_final
        assert "第二轮答案" in second_final and "第一轮答案" not in second_final


# ---------------------------------------------------------------- 引擎全链路

class TestEngineFakeTransportChain:
    """协议事件流 → CardEngine(FakeTransport) → 断言消息序列与卡片演化。"""

    def test_message_sequence_started_think_tool_answer_completed(self):
        t = FakeTransport()
        eng = CardEngine(cfg=EngineConfig(), transport=t)
        events = [
            proto_ev(EventType.MESSAGE_STARTED, 1, {"event": "agent_before"}),
            proto_ev(EventType.THINKING_DELTA, 2, {"text": "第一步思考"}),
            proto_ev(EventType.THINKING_DELTA, 3, {"text": "第二步思考"}),
            proto_ev(EventType.TOOL_UPDATED, 4, {"tool_id": "t1", "tool_name": "code_run", "status": "running"}),
            proto_ev(EventType.TOOL_UPDATED, 5, {"tool_id": "t1", "tool_name": "code_run", "status": "completed", "detail": "ok"}),
            proto_ev(EventType.ANSWER_DELTA, 6, {"text": "最终答案"}),
            proto_ev(EventType.MESSAGE_COMPLETED, 7, {"event": "agent_after", "exit_reason": {"result": "CURRENT_TASK_DONE"}}),
        ]
        for ev in events:
            r = eng.handle_event(ev)
            assert r.applied is True, f"seq={ev.sequence} not applied: {r.reason}"

        # 消息序列：1 次 send（started）+ 6 次 update（后续全部原地更新）
        assert _ops(t) == ["send_card"] + ["update_card"] * 6
        # 同 message_id 原地更新（send 返回的 fake_msg_1）
        assert _update_msg_ids(t) == ["fake_msg_1"] * 6

        # 卡片内容演化：thinking 累积；工具驱动状态但不暴露；随后 answer → 完成
        texts = [_card_text(c["card"]) for c in t.calls]
        assert "第一步思考" in texts[1]
        assert "第一步思考第二步思考" in texts[2]  # thinking.delta 累积
        assert all("t1" not in text and "code_run" not in text and "ok" not in text for text in texts)
        assert next(iter(eng.sessions.values())).tools["t1"].status == "completed"
        assert "最终答案" in texts[5]
        assert "最终答案" in texts[6]
        # header：思考中 → 运行中（工具 running）→ 思考中（工具终态、agent 继续）→ 已完成
        headers = [_header(c["card"]) for c in t.calls]
        assert headers[0] == "思考中…"
        assert headers[3] == "运行中…"
        assert headers[4] == "思考中…"  # 工具终态不派发"等待输入"：agent 仍在思考下一轮
        assert headers[6] == "已完成"

    def test_thinking_delta_accumulates_in_card(self):
        t = FakeTransport()
        eng = CardEngine(cfg=EngineConfig(), transport=t)
        eng.handle_event(proto_ev(EventType.MESSAGE_STARTED, 1, {}))
        eng.handle_event(proto_ev(EventType.THINKING_DELTA, 2, {"text": "aa"}))
        eng.handle_event(proto_ev(EventType.THINKING_DELTA, 3, {"text": "bb"}))
        cards = [c["card"] for c in t.calls if c["op"] == "update_card"]
        # footer 现为 markdown 块（T10），正文取首行
        assert _card_text(cards[0]).splitlines()[0] == "aa"
        assert _card_text(cards[1]).splitlines()[0] == "aabb"  # 累积而非替换

    def test_tool_updated_same_id_inplace_but_not_exposed(self):
        t = FakeTransport()
        eng = CardEngine(cfg=EngineConfig(), transport=t)
        started = proto_ev(EventType.MESSAGE_STARTED, 1, {})
        eng.handle_event(started)
        eng.handle_event(proto_ev(EventType.TOOL_UPDATED, 2, {"tool_id": "t1", "tool_name": "code_run", "status": "running"}))
        eng.handle_event(proto_ev(EventType.TOOL_UPDATED, 3, {"tool_id": "t1", "tool_name": "code_run", "status": "completed", "detail": "ok"}))
        msgs = _update_msg_ids(t)
        assert len(msgs) == 2 and msgs[0] == msgs[1] == "fake_msg_1"  # 同 id 原地更新
        tool = next(iter(eng.sessions.values())).tools["t1"]
        assert tool.status == "completed" and tool.detail == "ok"
        text = _all_text(t.calls[-1]["card"])
        assert "code_run" not in text and "ok" not in text  # 工具名/结果永不泄露
        assert "工具调用" in text and "✅" in text  # 通用文案+状态可见（生产验收标准）
        assert _header(t.calls[-1]["card"]) == "思考中…"  # 工具全终态不派发"等待输入"：agent 仍在思考

    def test_answer_delta_replaces_thinking_display(self):
        t = FakeTransport()
        eng = CardEngine(cfg=EngineConfig(), transport=t)
        eng.handle_event(proto_ev(EventType.MESSAGE_STARTED, 1, {}))
        eng.handle_event(proto_ev(EventType.THINKING_DELTA, 2, {"text": "思考草稿"}))
        eng.handle_event(proto_ev(EventType.ANSWER_DELTA, 3, {"text": "正式答案"}))
        assert "正式答案" in _card_text(t.calls[-1]["card"])
        assert "思考草稿" not in _card_text(t.calls[-1]["card"])  # answer 接管显示


# ---------------------------------------------------------------- fail-open

class TestFailOpen:
    def test_transport_exception_does_not_block_state(self):
        t = FakeTransport(send_plan=[RuntimeError("boom")])
        eng = CardEngine(cfg=EngineConfig(), transport=t)
        r1 = eng.handle_event(proto_ev(EventType.MESSAGE_STARTED, 1, {}))
        assert r1.applied is True  # 投递失败不阻塞状态应用
        assert r1.reason == "delivery:send_unknown"
        r2 = eng.handle_event(proto_ev(EventType.THINKING_DELTA, 2, {"text": "x"}))
        assert r2.applied is True
        # 抛错后回退到确定性 message_id，后续更新仍可尝试
        assert t.calls[0]["op"] == "send_card"
        assert _update_msg_ids(t)[0].startswith("fallback-")

    def test_update_unknown_retries_then_succeeds(self):
        t = FakeTransport(update_plan=["unknown", "unknown", "updated"])
        eng = CardEngine(cfg=EngineConfig(), transport=t)
        eng.handle_event(proto_ev(EventType.MESSAGE_STARTED, 1, {}))
        r = eng.handle_event(proto_ev(EventType.ANSWER_DELTA, 2, {"text": "答案"}))
        assert r.applied is True
        assert r.reason is None  # 重试后 updated → 无备注

    def test_cancelled_tool_state_is_internal_only(self):
        t = FakeTransport()
        eng = CardEngine(cfg=EngineConfig(), transport=t)
        started = proto_ev(EventType.MESSAGE_STARTED, 1, {})
        eng.handle_event(started)
        eng.handle_event(proto_ev(EventType.TOOL_UPDATED, 2, {"tool_id": "t1", "tool_name": "search", "status": "running"}))
        eng.handle_event(proto_ev(EventType.TOOL_UPDATED, 3, {"tool_id": "t1", "tool_name": "search", "status": "cancelled"}))
        assert next(iter(eng.sessions.values())).tools["t1"].status == "cancelled"
        text = _all_text(t.calls[-1]["card"])
        assert "search" not in text  # 工具名永不泄露
        assert "工具调用" in text and "⏹️" in text  # 取消态通用文案可见

    def test_failed_message_renders_error(self):
        t = FakeTransport()
        eng = CardEngine(cfg=EngineConfig(), transport=t)
        eng.handle_event(proto_ev(EventType.MESSAGE_STARTED, 1, {}))
        r = eng.handle_event(proto_ev(EventType.MESSAGE_FAILED, 2, {"reason": "模拟失败"}))
        assert r.applied is True
        card = t.calls[-1]["card"]
        assert _header(card) == "执行失败"
        assert "模拟失败" in _card_text(card)

    def test_failed_message_ignores_later_delta(self):
        """终态后 delta 不再累积（会话冻结）。"""
        t = FakeTransport()
        eng = CardEngine(cfg=EngineConfig(), transport=t)
        eng.handle_event(proto_ev(EventType.MESSAGE_STARTED, 1, {}))
        eng.handle_event(proto_ev(EventType.MESSAGE_FAILED, 2, {"reason": "boom"}))
        eng.handle_event(proto_ev(EventType.ANSWER_DELTA, 3, {"text": "迟到的答案"}))
        assert "迟到的答案" not in _card_text(t.calls[-1]["card"])


# ---------------------------------------------------------------- T5: header subtitle / footer 元信息

class TestT5SubtitleAndFooterE2E:
    """协议事件流全链路：动作词 subtitle + tokens/model footer。"""

    def test_subtitle_action_word_and_footer_tokens(self):
        t = FakeTransport()
        eng = CardEngine(cfg=EngineConfig(), transport=t)
        events = [
            proto_ev(EventType.MESSAGE_STARTED, 1, {"event": "agent_before"}),
            proto_ev(EventType.TOOL_UPDATED, 2, {"tool_id": "t1", "tool_name": "web_search", "status": "running"}),
            proto_ev(EventType.TOOL_UPDATED, 3, {"tool_id": "t1", "tool_name": "web_search", "status": "completed"}),
            proto_ev(EventType.ANSWER_DELTA, 4, {
                "text": "搜索中…",
                "usage": {"input_tokens": 1200, "output_tokens": 3400},
                "summary": {"model": "fake-model"},
            }),
            proto_ev(EventType.ANSWER_DELTA, 5, {"text": "最终答案"}),
            proto_ev(EventType.MESSAGE_COMPLETED, 6, {"event": "agent_after", "exit_reason": {"result": "CURRENT_TASK_DONE"}}),
        ]
        for ev in events:
            r = eng.handle_event(ev)
            assert r.applied is True, f"seq={ev.sequence}: {r.reason}"

        cards = [c["card"] for c in t.calls]
        # 工具 running → header subtitle 显示白名单动作词（不含工具名/参数）
        sub = cards[1]["header"].get("subtitle", {}).get("content", "")
        assert sub == "正在搜索"
        # 工具终态 → 动作词清除，回退阶段文案（subtitle 仍在，运行中）
        sub2 = cards[2]["header"].get("subtitle", {}).get("content", "")
        assert sub2 != "正在搜索"
        # 终态 → 无 subtitle
        assert "subtitle" not in cards[-1]["header"]
        # footer：⏱ 耗时 + model + tokens 缩写（T10：footer 用 markdown 块，生产拒 plain_text 元素）
        footer = [e["content"] for e in cards[-1]["body"]["elements"] if e.get("tag") == "markdown"][-1]
        assert "⏱" in footer and "fake-model" in footer and "↑1.2k/↓3.4k tokens" in footer
        # 全卡片无工具名泄露
        assert "web_search" not in _all_text(cards[-1])

    def test_ga_frames_flow_usage_to_footer(self):
        """真实 GA 帧形状：tool_before(code_run) → llm_after(带 usage) → agent_after。"""
        t = FakeTransport()
        eng = CardEngine(cfg=EngineConfig(), transport=t)
        for frame in [
            ga_frame("agent_before"),
            ga_frame("tool_before", tool_name="code_run"),
            ga_frame("llm_after", response=SimpleNamespace(
                content="答案内容", tool_calls=[], model="fake-model",
                usage=SimpleNamespace(input_tokens=800, output_tokens=200))),
            ga_frame("tool_after"),
            ga_frame("agent_after"),
        ]:
            for ev in bridge.map_ga_ctx(frame):
                r = eng.handle_event(ev)
                assert r.applied is True, f"{frame['_hfc_event']}: {r.reason}"

        final = t.calls[-1]["card"]
        # 运行中动作词出现过（code_run → 正在执行操作）
        subs = [c["card"]["header"].get("subtitle", {}).get("content", "") for c in t.calls]
        assert "正在执行操作" in subs
        # 终态卡无 subtitle、有 tokens footer
        assert "subtitle" not in final["header"]
        footer = [e["content"] for e in final["body"]["elements"] if e.get("tag") == "markdown"][-1]
        assert "fake-model" in footer and "↑800/↓200 tokens" in footer

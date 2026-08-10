"""engine 模块测试（编排/防乱序/去重/重试/fail-open）。"""

from __future__ import annotations

import threading
import time

import pytest

from ga_feishu_streaming_card.config import EngineConfig
from ga_feishu_streaming_card.delivery_policy import DeliveryPolicy
from ga_feishu_streaming_card.engine import CardEngine, EventResult
from ga_feishu_streaming_card.events import CardEvent, EventType
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


def _engine(transport=None, cfg=None) -> CardEngine:
    return CardEngine(cfg=cfg or EngineConfig(), transport=transport or FakeTransport())


def _op_count(t: FakeTransport, op: str) -> int:
    return sum(1 for c in t.calls if c["op"] == op)


class TestGuardRails:
    def test_disabled_engine(self):
        t = FakeTransport()
        eng = _engine(t, EngineConfig(enabled=False))
        r = eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1))
        assert r.applied is False and r.reason == "disabled"
        assert t.calls == []

    def test_invalid_event(self):
        eng = _engine()
        assert eng.handle_event("nope").reason == "invalid_event"

    def test_stale_sequence_discarded(self):
        t = FakeTransport()
        eng = _engine(t)
        assert eng.handle_event(_ev(EventType.MESSAGE_STARTED, 5)).applied is True
        r = eng.handle_event(_ev(EventType.ANSWER_DELTA, 3, data={"text": "x"}))
        assert r.applied is False and r.reason == "stale_sequence"
        # 同 sequence 重复也丢弃
        r2 = eng.handle_event(_ev(EventType.ANSWER_DELTA, 5, data={"text": "x"}))
        assert r2.reason == "stale_sequence"

    def test_terminal_dedup_same_message(self):
        t = FakeTransport()
        eng = _engine(t)
        assert eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1)).applied is True
        assert eng.handle_event(
            _ev(EventType.MESSAGE_COMPLETED, 2, message_id="om_1", data={"final_text": "ok"})
        ).applied is True
        r = eng.handle_event(
            _ev(EventType.MESSAGE_COMPLETED, 3, message_id="om_1", data={"final_text": "ok"})
        )
        assert r.applied is False and r.reason == "terminal_already_applied"

    def test_terminal_dedup_via_fallback_id(self):
        t = FakeTransport()
        eng = _engine(t)
        # 无 message_id 的事件：fallback id 确定性 → 同样去重
        ev1 = _ev(EventType.MESSAGE_COMPLETED, 1, message_id=None)
        ev2 = _ev(EventType.MESSAGE_COMPLETED, 2, message_id=None)
        assert eng.handle_event(ev1).applied is True
        assert eng.handle_event(ev2).reason == "terminal_already_applied"

    def test_native_disposition_skips_delivery(self):
        t = FakeTransport()
        cfg = EngineConfig(delivery=DeliveryPolicy(default="card", native_chats=["oc_native_*"]))
        eng = _engine(t, cfg)
        r = eng.handle_event(
            _ev(EventType.MESSAGE_STARTED, 1, chat_id="oc_native_1", conversation_id="cn")
        )
        assert r.applied is False and r.reason == "native_disposition"
        assert t.calls == []
        # 后续同会话事件同样 native（seq 已记录，更高 seq 继续判 native）
        r2 = eng.handle_event(
            _ev(EventType.MESSAGE_COMPLETED, 2, chat_id="oc_native_1", conversation_id="cn")
        )
        assert r2.reason == "native_disposition"


class TestDelivery:
    def test_started_sends_card_and_sets_message_id(self):
        t = FakeTransport()
        eng = _engine(t)
        r = eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1))
        assert r.applied is True and r.reason is None
        assert _op_count(t, "send_card") == 1
        session = eng.sessions["c1"]
        assert session.message_id == "fake_msg_1"

    def test_delta_updates_card(self):
        t = FakeTransport()
        eng = _engine(t)
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1))
        r = eng.handle_event(_ev(EventType.ANSWER_DELTA, 2, data={"text": "hi"}))
        assert r.applied is True
        assert _op_count(t, "update_card") == 1
        assert t.calls[-1]["message_id"] == "fake_msg_1"

    def test_update_unknown_retries_up_to_max(self):
        t = FakeTransport(update_plan=["unknown"] * 5)
        eng = _engine(t)
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1))
        r = eng.handle_event(_ev(EventType.ANSWER_DELTA, 2, data={"text": "hi"}))
        assert r.applied is True
        assert r.reason == "delivery:update_unknown"
        assert _op_count(t, "update_card") == CardEngine.UPDATE_MAX_ATTEMPTS  # 3 次，不无限重试

    def test_update_not_found_stops_retry(self):
        t = FakeTransport(update_plan=["not_found"] * 5)
        eng = _engine(t)
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1))
        r = eng.handle_event(_ev(EventType.ANSWER_DELTA, 2, data={"text": "hi"}))
        assert r.reason == "delivery:update_not_found"
        assert _op_count(t, "update_card") == 1  # 明确失败不重试

    def test_update_exception_treated_as_unknown(self):
        t = FakeTransport(update_plan=[RuntimeError("boom"), "updated"])
        eng = _engine(t)
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1))
        r = eng.handle_event(_ev(EventType.ANSWER_DELTA, 2, data={"text": "hi"}))
        assert r.reason is None  # 第二次重试成功
        assert _op_count(t, "update_card") == 2

    def test_send_fail_open_keeps_applied_with_fallback(self):
        t = FakeTransport(send_plan=["unknown"])
        eng = _engine(t)
        r = eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1))
        assert r.applied is True and r.reason == "delivery:send_unknown"
        session = eng.sessions["c1"]
        assert session.message_id == eng.fallback_message_id(_ev(EventType.MESSAGE_STARTED, 1))

    def test_send_exception_fail_open(self):
        t = FakeTransport(send_plan=[RuntimeError("boom")])
        eng = _engine(t)
        r = eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1))
        assert r.applied is True and r.reason == "delivery:send_unknown"

    def test_started_message_id_from_event(self):
        t = FakeTransport()
        eng = _engine(t)
        eng.handle_event(
            _ev(EventType.MESSAGE_STARTED, 1, message_id="om_event", data={"message_id": "om_data"})
        )
        # session.apply_event 优先取 data.message_id；消息已存在 → 走更新而非重复发送
        assert eng.sessions["c1"].message_id == "om_data"
        assert _op_count(t, "send_card") == 0
        assert _op_count(t, "update_card") == 1

    def test_system_notice_global_no_chat_skips_transport(self):
        t = FakeTransport()
        eng = _engine(t)
        ev = CardEvent(
            type=EventType.SYSTEM_NOTICE,
            sequence=1,
            created_at=100.0,
            chat_id=None,
            data={"text": "global"},
        )
        r = eng.handle_event(ev)
        assert r.applied is True and r.reason == "no_chat_target"
        assert t.calls == []

    def test_completed_flow_end_to_end(self):
        t = FakeTransport()
        eng = _engine(t)
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1))
        eng.handle_event(_ev(EventType.THINKING_DELTA, 2, data={"text": "think"}))
        eng.handle_event(_ev(EventType.ANSWER_DELTA, 3, data={"text": "ans"}))
        r = eng.handle_event(_ev(EventType.MESSAGE_COMPLETED, 4, data={"final_text": "final"}))
        assert r.applied is True
        assert _op_count(t, "send_card") == 1
        assert _op_count(t, "update_card") == 3
        assert eng.sessions["c1"].status == "completed"


class TestEventSerialization:
    def test_handle_event_is_serial_per_engine(self, monkeypatch):
        eng = _engine()
        active = 0
        maximum = 0
        guard = threading.Lock()
        original = eng._handle_event_unlocked

        def observed(ev):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            try:
                return original(ev)
            finally:
                with guard:
                    active -= 1

        monkeypatch.setattr(eng, "_handle_event_unlocked", observed)
        threads = [threading.Thread(target=eng.handle_event, args=(_ev(EventType.MESSAGE_STARTED, i + 1, conversation_id=f"c{i}"),)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert maximum == 1


class TestFallbackMessageId:
    def test_deterministic_same_conversation(self):
        eng = _engine()
        a = eng.fallback_message_id(_ev(EventType.MESSAGE_STARTED, 1))
        b = eng.fallback_message_id(_ev(EventType.ANSWER_DELTA, 9))
        assert a == b  # 同会话稳定
        assert a.startswith("fallback-")

    def test_differs_across_conversations(self):
        eng = _engine()
        a = eng.fallback_message_id(_ev(EventType.MESSAGE_STARTED, 1, chat_id="oc_1", conversation_id="c1"))
        b = eng.fallback_message_id(_ev(EventType.MESSAGE_STARTED, 1, chat_id="oc_2", conversation_id="c2"))
        assert a != b

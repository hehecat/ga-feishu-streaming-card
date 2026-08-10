"""events.py 单元测试：解析、校验、10 事件类型覆盖。"""

import pytest

from ga_feishu_streaming_card.events import CardEvent, EventType, REQUIRES_CHAT_ID, parse_event


def _base(**over):
    raw = {
        "type": "message.started",
        "sequence": 0,
        "chat_id": "oc_test",
        "created_at": 100.0,
        "data": {"text": "hi"},
    }
    raw.update(over)
    return raw


class TestParseValid:
    def test_parses_valid_event(self):
        ev = parse_event(_base())
        assert isinstance(ev, CardEvent)
        assert ev.type is EventType.MESSAGE_STARTED
        assert ev.sequence == 0
        assert ev.chat_id == "oc_test"
        assert ev.platform == "feishu"

    def test_parses_optional_turn_id_and_exposes_fields(self):
        ev = parse_event(_base(turn_id="t-1", conversation_id="c-1", message_id="m-1"))
        assert ev.turn_id == "t-1"
        assert ev.conversation_id == "c-1"
        assert ev.message_id == "m-1"

    def test_parses_all_ten_event_types(self):
        for t in EventType:
            raw = _base(type=t.value)
            if t in REQUIRES_CHAT_ID:
                raw["chat_id"] = "oc_x"
            ev = parse_event(raw)
            assert ev.type is t

    def test_missing_turn_id_is_none(self):
        ev = parse_event(_base())
        assert ev.turn_id is None

    def test_platform_defaults_to_feishu(self):
        ev = parse_event({k: v for k, v in _base().items() if k != "platform"})
        assert ev.platform == "feishu"

    def test_created_at_accepts_int(self):
        ev = parse_event(_base(created_at=5))
        assert ev.created_at == 5.0

    def test_data_optional(self):
        raw = _base()
        raw.pop("data")
        ev = parse_event(raw)
        assert ev.data == {}


class TestParseReject:
    def test_rejects_invalid_type(self):
        with pytest.raises(ValueError):
            parse_event(_base(type="bogus.event"))

    def test_rejects_missing_type(self):
        raw = _base()
        raw.pop("type")
        with pytest.raises(ValueError):
            parse_event(raw)

    def test_rejects_non_feishu_platform(self):
        with pytest.raises(ValueError):
            parse_event(_base(platform="wecom"))

    def test_rejects_negative_sequence(self):
        with pytest.raises(ValueError):
            parse_event(_base(sequence=-1))

    def test_rejects_bool_sequence(self):
        with pytest.raises(ValueError):
            parse_event(_base(sequence=True))

    def test_rejects_non_int_sequence(self):
        with pytest.raises(ValueError):
            parse_event(_base(sequence="0"))

    def test_rejects_missing_chat_id_for_required_events(self):
        raw = _base(type="answer.delta")
        raw.pop("chat_id")
        with pytest.raises(ValueError):
            parse_event(raw)

    def test_rejects_empty_chat_id_for_required_events(self):
        with pytest.raises(ValueError):
            parse_event(_base(type="tool.updated", chat_id=""))

    def test_system_notice_allows_missing_chat_id(self):
        raw = _base(type="system.notice")
        raw.pop("chat_id")
        ev = parse_event(raw)
        assert ev.type is EventType.SYSTEM_NOTICE
        assert ev.chat_id is None

    def test_rejects_non_dict_event(self):
        with pytest.raises(ValueError):
            parse_event("message.started")  # type: ignore[arg-type]


class TestRoundTrip:
    def test_to_dict_roundtrip(self):
        ev = parse_event(_base(sequence=3, data={"text": "x"}))
        d = ev.to_dict()
        assert d["type"] == "message.started"
        assert d["sequence"] == 3
        assert d["chat_id"] == "oc_test"
        ev2 = parse_event(d)
        assert ev2.type is ev.type
        assert ev2.sequence == ev.sequence
        assert ev2.data == ev.data

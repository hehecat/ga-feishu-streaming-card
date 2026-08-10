"""delivery_policy 模块测试。"""

from __future__ import annotations

from ga_feishu_streaming_card.delivery_policy import DeliveryPolicy
from ga_feishu_streaming_card.events import CardEvent, EventType


def _ev(chat_id=None) -> CardEvent:
    return CardEvent(
        type=EventType.MESSAGE_STARTED,
        sequence=1,
        created_at=100.0,
        chat_id=chat_id,
        conversation_id="c1",
    )


def test_default_is_card():
    p = DeliveryPolicy()
    assert p.decide_disposition("oc_1", _ev("oc_1")) == "card"


def test_native_chats_glob_match():
    p = DeliveryPolicy(native_chats=["oc_native_*", "ou_2", "c?t_*"])
    assert p.decide_disposition("oc_native_abc", _ev("oc_native_abc")) == "native"
    assert p.decide_disposition("ou_2", _ev("ou_2")) == "native"
    assert p.decide_disposition("cat_x", _ev("cat_x")) == "native"
    assert p.decide_disposition("oc_other", _ev("oc_other")) == "card"
    assert p.decide_disposition("dog_y", _ev("dog_y")) == "card"


def test_default_native_without_patterns():
    p = DeliveryPolicy(default="native")
    assert p.decide_disposition("oc_1", _ev("oc_1")) == "native"


def test_policy_unavailable_falls_back_to_card():
    # 有 native 名单但缺 chat_id：策略不可判 → card
    p = DeliveryPolicy(native_chats=["oc_native_*"])
    assert p.decide_disposition(None, _ev(None)) == "card"
    assert p.decide_disposition("", _ev(None)) == "card"
    # 非法 default → card
    assert DeliveryPolicy(default="bogus").decide_disposition("oc_1", _ev("oc_1")) == "card"
    # 非法 event → card
    assert p.decide_disposition("oc_native_1", None) == "card"


def test_from_dict():
    p = DeliveryPolicy.from_dict({"default": "native", "native_chats": ["ou_*"]})
    assert p.decide_disposition("ou_1", _ev("ou_1")) == "native"
    p2 = DeliveryPolicy.from_dict({})
    assert p2.decide_disposition("ou_1", _ev("ou_1")) == "card"
    assert DeliveryPolicy.from_dict(None) == DeliveryPolicy()

"""T13-D：公开渲染入口面对契约外损坏会话仍 fail-open。"""

import pytest

from ga_feishu_streaming_card.render import render_card, render_card_result
from ga_feishu_streaming_card.session import CardSession, ToolState


def _session():
    return CardSession("t13-d", "oc_t13-d", thinking="thinking")


def _assert_card(value):
    card = value.card if hasattr(value, "card") else value
    assert card["config"]["wide_screen_mode"] is True
    assert isinstance(card["header"], dict)
    assert isinstance(card["body"]["elements"], list)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tools", None),
        ("interactions", None),
        ("notices", None),
        ("thinking", 12345),
        ("tools", {"bad": {"status": "running"}}),
    ],
    ids=["tools-none", "interactions-none", "notices-none", "thinking-non-str", "tool-not-toolstate"],
)
def test_render_card_result_fail_open_for_corrupt_session_field(field, value):
    session = _session()
    setattr(session, field, value)
    _assert_card(render_card_result(session))


def test_render_card_fail_open_for_non_cardsession():
    _assert_card(render_card({"conversation_id": "not-a-session"}))

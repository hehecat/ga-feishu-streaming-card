"""T11-A 行为对齐验收：D1/D3/D4/D6 边界场景。"""

import json

from ga_feishu_streaming_card.config import CardLimitsConfig
from ga_feishu_streaming_card.events import CardEvent, EventType
from ga_feishu_streaming_card.limits import card_json_bytes
from ga_feishu_streaming_card.render import render_card, render_card_result
from ga_feishu_streaming_card.session import CardSession
from ga_feishu_streaming_card.status import DisplayStatus, resolve_display_status
from ga_feishu_streaming_card.text import normalize_stream_text


def _session(**kwargs):
    base = {"conversation_id": "t11-c", "chat_id": "oc_t11"}
    base.update(kwargs)
    return CardSession(**base)


def _event(kind, data, sequence=1):
    return CardEvent(
        type=kind,
        sequence=sequence,
        created_at=0.0,
        chat_id="oc_t11",
        data=data,
    )


class TestT11D1DisplayStatus:
    def test_answer_derives_in_progress(self):
        session = _session(answer="partial answer")
        assert resolve_display_status(session) is DisplayStatus.IN_PROGRESS
        assert render_card(session)["header"]["title"]["content"] == "生成中…"

    def test_explicit_display_status_overrides_derived_answer(self):
        session = _session(answer="partial answer")
        session.apply_event(_event(EventType.ANSWER_DELTA, {"display_status": "waiting", "text": ""}))
        assert session.display_status == "waiting"
        assert resolve_display_status(session) is DisplayStatus.WAITING
        assert render_card(session)["header"]["title"]["content"] == "等待中…"

    def test_invalid_explicit_status_does_not_override(self):
        session = _session(answer="partial answer")
        session.apply_event(_event(EventType.ANSWER_DELTA, {"display_status": "bogus", "text": ""}))
        assert resolve_display_status(session) is DisplayStatus.IN_PROGRESS


class TestT11D3HtmlThink:
    def test_complete_html_think_block_is_removed(self):
        out = normalize_stream_text("before <think>secret</think> after")
        assert out == "before  after"
        assert "secret" not in out

    def test_html_think_attributes_and_case_are_removed(self):
        out = normalize_stream_text("<THINK mode='hidden'>secret</THINK>visible")
        assert out == "visible"

    def test_unclosed_html_think_is_fail_closed(self):
        out = normalize_stream_text("visible\n<think>secret across chunk")
        assert out == "visible"
        assert "secret" not in out


class TestT11D4CompletedPostscript:
    def test_exact_64_answer_and_short_postscript_merge(self):
        session = _session(answer="a" * 64)
        session.apply_event(_event(EventType.MESSAGE_COMPLETED, {"final_text": "b"}, 2))
        assert session.answer == "a" * 64 + "\n\n---\n\n" + "b"

    def test_exact_240_postscript_and_three_to_one_ratio_merge(self):
        session = _session(answer="a" * 720)
        session.apply_event(_event(EventType.MESSAGE_COMPLETED, {"final_text": "b" * 240}, 2))
        assert session.answer == "a" * 720 + "\n\n---\n\n" + "b" * 240

    def test_ratio_below_three_or_postscript_over_240_keeps_final_only(self):
        short_ratio = _session(answer="a" * 719)
        short_ratio.apply_event(_event(EventType.MESSAGE_COMPLETED, {"final_text": "b" * 240}, 2))
        assert short_ratio.answer == "b" * 240
        long_postscript = _session(answer="a" * 720)
        long_postscript.apply_event(_event(EventType.MESSAGE_COMPLETED, {"final_text": "b" * 241}, 2))
        assert long_postscript.answer == "b" * 241


class TestT11D6DegradedCard:
    def test_over_limit_is_orange_native_handoff(self):
        # 默认限额（28KB）下 200KB 答案 → 不静默截断，整体降级为橙色交接卡
        card = render_card(_session(answer="x" * 200_000), CardLimitsConfig(safe_bytes=28_000))
        assert card["header"]["template"] == "orange"
        payload = json.dumps(card, ensure_ascii=False)
        assert "超出展示限制" in payload
        assert "等待原生消息" in payload
        assert card_json_bytes(card) <= 28_000

    def test_normal_card_is_not_degraded(self):
        card = render_card(_session(answer="normal"), CardLimitsConfig(safe_bytes=28_000))
        assert card["header"]["template"] != "orange"

    def test_thinking_over_limit_result_is_deferred_native(self):
        # 非终态 200k thinking → disposition=deferred_native（等待原生消息交接）
        s = _session(thinking="t" * 200_000, status="thinking")
        res = render_card_result(s, CardLimitsConfig(safe_bytes=28_000))
        assert res.disposition == "deferred_native"
        assert res.card["header"]["template"] == "orange"
        assert "等待原生消息" in json.dumps(res.card, ensure_ascii=False)

    def test_completed_over_limit_result_is_native(self):
        # 终态 200k 答案 → disposition=native（直接原生交接）
        s = _session(answer="a" * 200_000, status="completed")
        res = render_card_result(s, CardLimitsConfig(safe_bytes=28_000))
        assert res.disposition == "native"
        assert res.card["header"]["template"] == "orange"

    def test_normal_result_disposition_is_card(self):
        res = render_card_result(_session(answer="normal"), CardLimitsConfig(safe_bytes=28_000))
        assert res.disposition == "card"
        assert res.card["header"]["template"] != "orange"

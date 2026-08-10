"""T27-A 模型选择卡验收：/llm /llms 列表文本 → 一个 select_static。"""

import json

from ga_feishu_streaming_card.render import _model_selector_element, _parse_llms_selector, render_card
from ga_feishu_streaming_card.session import CardSession

LLMS_TEXT = "LLMs:\n→ [0] model-a\n  [1] model-b\n  [2] model-c"


def _session(answer):
    return CardSession(conversation_id="t16a-c", chat_id="oc_t16a", answer=answer)


def _model_selectors(card):
    # T27-G：2.0 卡片中 select_static 是块级元素，直接位于 body.elements
    return [
        element for element in card["body"]["elements"]
        if element.get("tag") == "select_static"
        and (element.get("value") or {}).get("action") == "/model"
    ]


class TestT16aParse:
    def test_parse_full_list(self):
        models, current = _parse_llms_selector(LLMS_TEXT)
        assert current == "model-a"
        assert models == [("model-a", True), ("model-b", False), ("model-c", False)]

    def test_parse_invalid_lists_returns_none(self):
        assert _parse_llms_selector("普通回答文本，不是模型列表") is None
        assert _parse_llms_selector("LLMs:") is None
        assert _parse_llms_selector("LLMs:\n（暂无可用模型）") is None
        assert _parse_llms_selector("  [0] model-a\n  [1] model-b") is None


class TestT27AModelSelect:
    def test_one_select_includes_all_models_and_current_initial(self):
        selectors = _model_selectors(render_card(_session(LLMS_TEXT)))
        assert len(selectors) == 1
        selector = selectors[0]
        # T27-G6: option.value = 渠道索引（与 /llms 列表 [i] 一致）
        assert [option["value"] for option in selector["options"]] == ["0", "1", "2"]
        assert selector["initial_option"] == "0"  # T27-G2/G6: 2.0 要求字符串=当前索引

    def test_controlled_value_and_full_option_protocol(self):
        selector = _model_selectors(render_card(_session(LLMS_TEXT)))[0]
        assert selector["value"] == {"hfc": 1, "action": "/model"}
        assert selector["placeholder"]["content"] == "选择模型"
        assert selector["options"][1] == {
            "text": {"tag": "plain_text", "content": "model-b"}, "value": "1"
        }

    def test_single_current_model_is_selectable(self):
        selector = _model_selectors(render_card(_session("LLMs:\n→ [0] only-model")))[0]
        assert selector["options"][0]["value"] == "0"
        assert selector["initial_option"] == "0"  # T27-G2/G6: 2.0 要求字符串=当前索引

    def test_no_selector_for_plain_or_empty_list(self):
        assert _model_selectors(render_card(_session("这是普通回答"))) == []
        assert _model_selectors(render_card(_session("LLMs:"))) == []

    def test_command_buttons_still_present(self):
        card = render_card(_session(LLMS_TEXT))
        # T27-G：命令按钮位于顶层 column_set（每行两列各 1 个 button 块）
        command_buttons = [
            b
            for c in card["body"]["elements"] if c.get("tag") == "column_set"
            for col in c["columns"] for b in col["elements"]
        ]
        assert any((b.get("value") or {}).get("action") == "/settings"
                   for b in command_buttons)
        assert len(_model_selectors(card)) == 1

    def test_card_json_serializable(self):
        payload = json.dumps(render_card(_session(LLMS_TEXT)), ensure_ascii=False)
        assert "model-b" in payload and "model-c" in payload


class TestT16aHelper:
    def test_selector_element_protocol(self):
        element = _model_selector_element(LLMS_TEXT)
        assert element is not None
        assert element["tag"] == "select_static"
        assert element["value"] == {"hfc": 1, "action": "/model"}

    def test_selector_element_none_for_non_list(self):
        assert _model_selector_element("普通文本") is None
        assert _model_selector_element("") is None


class TestT16aCommandCard:
    def test_command_card_has_model_select(self):
        from ga_feishu_streaming_card.command import render_command_result_card
        selector = _model_selectors(render_command_result_card(LLMS_TEXT, "/llm"))[0]
        assert [option["value"] for option in selector["options"]] == ["0", "1", "2"]

    def test_command_card_no_selector_for_plain_text(self):
        from ga_feishu_streaming_card.command import render_command_result_card
        assert _model_selectors(render_command_result_card("hello world", "/llm")) == []

    def test_command_card_limits_still_enforced(self):
        from ga_feishu_streaming_card.command import render_command_result_card
        payload = json.dumps(render_command_result_card(LLMS_TEXT, "/llm"), ensure_ascii=False)
        assert "model-b" in payload and "model-c" in payload

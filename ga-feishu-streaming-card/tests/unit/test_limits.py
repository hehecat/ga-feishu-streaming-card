"""limits.py 单元测试：限额常量、统计、enforce 抛错。"""

import pytest

from ga_feishu_streaming_card.limits import (
    FEISHU_MAX_ELEMENTS,
    FEISHU_MAX_TABLES,
    SAFE_CARD_JSON_BYTES,
    CardLimitExceeded,
    card_json_bytes,
    count_elements,
    count_tables,
    enforce_card_limits,
)


class TestConstants:
    def test_contract_values(self):
        assert FEISHU_MAX_TABLES == 5
        assert FEISHU_MAX_ELEMENTS == 200
        assert SAFE_CARD_JSON_BYTES == 28000


class TestCounts:
    def test_count_elements_flat(self):
        card = {"elements": [{"type": "div"}, {"type": "text"}]}
        assert count_elements(card) == 2

    def test_count_elements_nested(self):
        card = {
            "elements": [
                {"type": "div", "elements": [{"type": "text"}]},
                {"type": "column_set", "columns": [[{"type": "text"}]]},
            ]
        }
        assert count_elements(card) == 4

    def test_count_tables(self):
        card = {"elements": [{"type": "table"}, {"type": "div"}, {"type": "table"}]}
        assert count_tables(card) == 2

    def test_count_tables_nested(self):
        card = {"elements": [{"type": "div", "elements": [{"type": "table"}]}]}
        assert count_tables(card) == 1

    def test_json_bytes_utf8(self):
        card = {"elements": [{"text": "中文😀"}]}
        assert card_json_bytes(card) > len(str(card))


class TestEnforce:
    def test_small_card_ok(self):
        enforce_card_limits({"elements": [{"type": "text"}]})

    def test_many_elements_raise(self):
        card = {"elements": [{"type": "text"} for _ in range(FEISHU_MAX_ELEMENTS + 1)]}
        with pytest.raises(CardLimitExceeded) as ei:
            enforce_card_limits(card)
        assert ei.value.limit_name == "elements"

    def test_many_tables_raise(self):
        card = {"elements": [{"type": "table"} for _ in range(FEISHU_MAX_TABLES + 1)]}
        with pytest.raises(CardLimitExceeded) as ei:
            enforce_card_limits(card)
        assert ei.value.limit_name == "tables"

    def test_oversize_json_raise(self):
        card = {"elements": [{"type": "text", "content": "x" * SAFE_CARD_JSON_BYTES}]}
        with pytest.raises(CardLimitExceeded) as ei:
            enforce_card_limits(card)
        assert ei.value.limit_name == "json_bytes"

    def test_exception_carries_current_and_max(self):
        card = {"elements": [{"type": "text"} for _ in range(201)]}
        with pytest.raises(CardLimitExceeded) as ei:
            enforce_card_limits(card)
        assert ei.value.current == 201
        assert ei.value.maximum == 200

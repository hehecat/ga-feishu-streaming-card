"""内容安全限额（引擎核心，独立实现）。

设计约定：FEISHU_MAX_TABLES=5 / FEISHU_MAX_ELEMENTS=200 / SAFE_CARD_JSON_BYTES=28000，
超限抛 CardLimitExceeded（含超限名称/当前值/上限，便于 render 层降级）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

FEISHU_MAX_TABLES = 5
FEISHU_MAX_ELEMENTS = 200
SAFE_CARD_JSON_BYTES = 28000


class CardLimitExceeded(Exception):
    """卡片内容超出飞书限额。"""

    def __init__(self, limit_name: str, current: int, maximum: int):
        self.limit_name = limit_name
        self.current = current
        self.maximum = maximum
        super().__init__(
            f"card limit exceeded: {limit_name} {current} > {maximum}"
        )


def _iter_elements(elements: Any) -> List[Dict[str, Any]]:
    """展平嵌套元素：column_set/table 等容器内的元素也计入。"""
    out: List[Dict[str, Any]] = []
    if isinstance(elements, list):
        for el in elements:
            if isinstance(el, list):  # column_set.columns 是 列表的列表
                out.extend(_iter_elements(el))
                continue
            if not isinstance(el, dict):
                continue
            out.append(el)
            # 容器内嵌元素递归展平
            for key in ("elements", "columns", "rows"):
                if key in el:
                    out.extend(_iter_elements(el[key]))
    return out


def _card_elements(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    """兼容 1.0/2.0 卡片：2.0 的元素位于 body.elements。"""
    body = card.get("body") or {}
    return body.get("elements") or card.get("elements") or []


def count_elements(card: Dict[str, Any]) -> int:
    """统计卡片元素总数（elements 数组 + 嵌套容器内元素）。"""
    elements = card.get("elements", [])
    return len(_iter_elements(elements))


def count_tables(card: Dict[str, Any]) -> int:
    """统计 type == 'table' 的顶层/嵌套元素数量。"""
    n = 0
    for el in _iter_elements(_card_elements(card)):
        if isinstance(el, dict) and el.get("type") == "table":
            n += 1
    return n


def card_json_bytes(card: Dict[str, Any]) -> int:
    """卡片 JSON 的 UTF-8 字节数（ensure_ascii=False 贴近真实传输）。"""
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))


def enforce_card_limits(
    card: Dict[str, Any],
    safe_bytes: int = SAFE_CARD_JSON_BYTES,
    max_elements: int = FEISHU_MAX_ELEMENTS,
    max_tables: int = FEISHU_MAX_TABLES,
) -> None:
    """按限额规格校验卡片；任一超限抛 CardLimitExceeded（第一个违规为准）。

    限额默认取飞书常量；调用方可传入渲染配置（CardLimitsConfig），
    保证“渲染限额”与“校验限额”一致（D6 降级判定可达）。
    """
    n_elements = count_elements(card)
    if n_elements > max_elements:
        raise CardLimitExceeded("elements", n_elements, max_elements)
    n_tables = count_tables(card)
    if n_tables > max_tables:
        raise CardLimitExceeded("tables", n_tables, max_tables)
    n_bytes = card_json_bytes(card)
    if n_bytes > safe_bytes:
        raise CardLimitExceeded("json_bytes", n_bytes, safe_bytes)

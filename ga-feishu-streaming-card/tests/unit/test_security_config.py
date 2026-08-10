"""安全审查测试：畸形配置防御（config 层）。"""
import math

import pytest

from ga_feishu_streaming_card import config
from ga_feishu_streaming_card.config import (
    DEFAULT_BASE_URL,
    EngineConfig,
    load_config,
    valid_base_url,
)


def test_top_level_non_dict_falls_back():
    cfg = EngineConfig.from_dict("not a dict")
    assert cfg.limits.retention_seconds == 3600
    assert cfg.card_limits.max_elements == 200


def test_unknown_sub_keys_ignored():
    cfg = EngineConfig.from_dict(
        {
            "limits": {"unknown_field": 1, "retention_seconds": 123},
            "card_limits": {"zzz": True},
            "bogus_section": {"a": 1},
        }
    )
    assert cfg.limits.retention_seconds == 123
    assert cfg.card_limits.max_elements == 200


def test_wrong_numeric_types_coerced_or_defaulted():
    cfg = EngineConfig.from_dict(
        {
            "limits": {"retention_seconds": "abc", "history_limit": None},
            "card_limits": {"max_elements": "300", "safe_bytes": 3.5},
            "http": {"timeout_ms": "not-a-number"},
        }
    )
    assert cfg.limits.retention_seconds == 3600.0  # 非法 → 默认
    assert cfg.limits.history_limit == 50
    assert cfg.card_limits.max_elements == 300  # 数字串 → 强转
    assert cfg.card_limits.safe_bytes == 3  # 小数 → 截断
    assert cfg.http.timeout_ms == 800  # 非法 → 默认


def test_nan_inf_rejected():
    cfg = EngineConfig.from_dict(
        {
            "limits": {"retention_seconds": float("nan"), "zombie_grace_seconds": float("inf")},
        }
    )
    assert cfg.limits.retention_seconds == 3600
    assert cfg.limits.zombie_grace_seconds == 120


def test_base_url_scheme_validation():
    assert valid_base_url("https://open.feishu.cn")
    assert valid_base_url("http://127.0.0.1:8080")
    assert not valid_base_url("file:///etc/passwd")
    assert not valid_base_url("javascript:alert(1)")
    assert not valid_base_url("ftp://x.com")
    assert not valid_base_url("https://")


def test_bad_base_url_falls_back_to_default():
    cfg = EngineConfig.from_dict({"http": {"base_url": "file:///etc/passwd"}})
    assert cfg.http.base_url == DEFAULT_BASE_URL
    cfg2 = EngineConfig.from_dict({"http": {"base_url": "https://ok.feishu.cn"}})
    assert cfg2.http.base_url == "https://ok.feishu.cn"


def test_native_chats_coerced_and_capped():
    from ga_feishu_streaming_card.delivery_policy import DeliveryPolicy
    from ga_feishu_streaming_card.events import CardEvent, EventType

    p = DeliveryPolicy.from_dict({"native_chats": [123, None, "oc_abc", 45.6] * 50})
    assert all(isinstance(c, str) for c in p.native_chats)
    assert len(p.native_chats) <= 100
    ev = CardEvent(type=EventType.MESSAGE_STARTED, sequence=1, created_at=0.0)
    assert p.decide_disposition("oc_abc", ev) == "native"
    assert p.decide_disposition("oc_xyz", ev) == p.default  # 未命中 → 默认形态


def test_env_bad_base_url_falls_back(monkeypatch):
    monkeypatch.setenv("HFC_BASE_URL", "file:///etc/shadow")
    cfg = load_config(None)
    assert cfg.http.base_url == DEFAULT_BASE_URL


def test_env_good_base_url_kept(monkeypatch):
    monkeypatch.setenv("HFC_BASE_URL", "https://example.feishu.cn")
    cfg = load_config(None)
    assert cfg.http.base_url == "https://example.feishu.cn"

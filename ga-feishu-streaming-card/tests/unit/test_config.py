"""config 模块测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ga_feishu_streaming_card.config import (
    EngineConfig,
    HttpConfig,
    load_config,
)


def test_defaults_conform_to_contract():
    cfg = EngineConfig()
    assert cfg.enabled is True
    assert cfg.transport == "fake"
    assert cfg.limits.retention_seconds == 3600.0
    assert cfg.limits.zombie_grace_seconds == 120.0
    assert cfg.limits.history_limit == 50
    assert cfg.card_limits.max_elements == 200
    assert cfg.card_limits.max_tables == 5
    assert cfg.card_limits.safe_bytes == 28000
    assert cfg.delivery.default == "card"
    assert cfg.delivery.native_chats == []
    assert cfg.http.base_url == "https://open.feishu.cn"
    assert cfg.http.timeout_ms == 800
    assert cfg.coalesce.delta_ms == 250
    assert cfg.coalesce.delta_chars == 600
    assert cfg.coalesce.max_pending == 128


def test_from_dict_nested_and_partial():
    cfg = EngineConfig.from_dict(
        {
            "enabled": False,
            "transport": "http",
            "limits": {"retention_seconds": 7200, "history_limit": 10},
            "delivery": {"default": "native", "native_chats": ["oc_*", "ou_2"]},
            "http": {"base_url": "https://example.com", "timeout_ms": 1500},
            "coalesce": {"delta_ms": 500},
        }
    )
    assert cfg.enabled is False
    assert cfg.transport == "http"
    assert cfg.limits.retention_seconds == 7200
    assert cfg.limits.zombie_grace_seconds == 120.0  # 未指定取默认
    assert cfg.limits.history_limit == 10
    assert cfg.delivery.default == "native"
    assert cfg.delivery.native_chats == ["oc_*", "ou_2"]
    assert cfg.http.base_url == "https://example.com"
    assert cfg.http.timeout_ms == 1500
    assert cfg.coalesce.delta_ms == 500
    assert cfg.coalesce.delta_chars == 600


def test_invalid_transport_falls_back_to_fake():
    cfg = EngineConfig.from_dict({"transport": "gopher"})
    assert cfg.transport == "fake"


def test_enabled_string_forms():
    for raw in ("1", "true", "yes", "on", "TRUE"):
        assert EngineConfig.from_dict({"enabled": raw}).enabled is True
    for raw in ("0", "false", "no", "off", "FALSE"):
        assert EngineConfig.from_dict({"enabled": raw}).enabled is False


def test_load_from_yaml_file(tmp_path: Path):
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "transport: http\n"
        "http:\n"
        "  base_url: https://open.feishu.cn\n"
        "  timeout_ms: 1200\n"
        "delivery:\n"
        "  native_chats:\n"
        "    - oc_native_*\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.transport == "http"
    assert cfg.http.timeout_ms == 1200
    assert cfg.delivery.native_chats == ["oc_native_*"]
    assert cfg.enabled is True  # 文件未写 enabled 取默认


def test_env_overrides(monkeypatch, tmp_path: Path):
    p = tmp_path / "cfg.yaml"
    p.write_text("transport: http\n", encoding="utf-8")
    monkeypatch.setenv("HFC_ENABLED", "0")
    monkeypatch.setenv("HFC_TRANSPORT", "fake")
    monkeypatch.setenv("HFC_BASE_URL", "https://env.example.com/")
    monkeypatch.setenv("HFC_HTTP_TIMEOUT_MS", "999")
    cfg = load_config(p)
    assert cfg.enabled is False
    assert cfg.transport == "fake"
    assert cfg.http.base_url == "https://env.example.com"
    assert cfg.http.timeout_ms == 999


def test_env_default_enabled(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("HFC_ENABLED", raising=False)
    monkeypatch.delenv("HFC_TRANSPORT", raising=False)
    monkeypatch.delenv("HFC_BASE_URL", raising=False)
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.enabled is True  # env 缺省 = 1


def test_http_app_id_secret_are_refs_only():
    """app_id/app_secret 仅存引用：赋值保留、不参与任何读取/日志。"""
    cfg = EngineConfig(http=HttpConfig(app_id="cli_xxx", app_secret="secret_yyy"))
    assert cfg.http.app_id == "cli_xxx"
    assert cfg.http.app_secret == "secret_yyy"


def test_missing_file_returns_defaults(tmp_path: Path):
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg == EngineConfig()


def test_load_config_missing_warns_stderr(tmp_path, monkeypatch, capsys):
    """T13-E F2：无任何候选配置时 stderr 警告（不回退静默）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HFC_CONFIG", raising=False)
    cfg = load_config()
    assert cfg.transport == "fake"  # 默认回退仍可用
    err = capsys.readouterr().err
    assert "未找到配置" in err and "默认配置" in err

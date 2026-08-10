"""T11e: 命令结果卡引擎懒创建（宿主 SDK 兜底）。

背景（#47/#49 用户 P0）：重启后直接发 /new 只收到纯文本。根因：
fsapp._reply 调 send_command_result_card 时不传 cfg，而旧逻辑
``if engine is None and cfg is not None`` 使懒创建永不触发 → engine
None → fail-open 回退纯文本。

修复：engine None 时 cfg 显式传入走 _make_engine(cfg)；cfg=None 时
用宿主 SDK transport 兜底（_make_sdk_engine，依赖 __main__ 已注入的
_send_raw/_patch_card）；两者皆不可得 → None → fail-open（False）。
"""
from types import SimpleNamespace

import pytest

from ga_feishu_streaming_card import bridge
from ga_feishu_streaming_card.config import EngineConfig


class _RecordTransport:
    def __init__(self):
        self.calls = []

    def send_card(self, chat_id, card):
        self.calls.append((chat_id, card))
        return SimpleNamespace(ok=True)


class TestLazyEngineT11e:
    def test_no_cfg_no_bridge_builds_sdk_engine(self, monkeypatch):
        """冷启动（无 cfg、无桥、无事件预热）→ 宿主 SDK transport 兜底引擎并直发。"""
        monkeypatch.setattr(bridge, "_BRIDGE", None)
        rec = _RecordTransport()
        monkeypatch.setattr(bridge, "_host_sdk_transport", lambda: rec)
        ok = bridge.send_command_result_card("oc_1", "/new", "🆕 已开启新对话")
        assert ok is True
        assert len(rec.calls) == 1
        chat_id, card = rec.calls[0]
        assert chat_id == "oc_1"
        # 命令结果卡语义：/new 标题映射 + 模板命中
        assert card["header"]["title"]["content"] == "会话已重置"

    def test_no_cfg_no_bridge_sdk_unavailable_fail_open(self, monkeypatch):
        """宿主 SDK 不可用（如测试环境无 _send_raw）→ False，绝不抛。"""
        monkeypatch.setattr(bridge, "_BRIDGE", None)
        monkeypatch.setattr(bridge, "_host_sdk_transport", lambda: None)
        assert bridge.send_command_result_card("oc_1", "/new", "hi") is False

    def test_cfg_preferred_over_sdk(self, monkeypatch):
        """显式 cfg 优先：engine None 时仍走 _make_engine(cfg)，不触发 SDK 兜底。"""
        monkeypatch.setattr(bridge, "_BRIDGE", None)
        seen = {}

        def fake_make(cfg):
            seen["cfg"] = cfg
            return SimpleNamespace(transport=_RecordTransport())

        monkeypatch.setattr(bridge, "_make_engine", fake_make)
        monkeypatch.setattr(bridge, "_host_sdk_transport", lambda: _RecordTransport())
        cfg = EngineConfig()
        assert bridge.send_command_result_card("oc_1", "/new", "hi", cfg=cfg) is True
        assert seen.get("cfg") is cfg

    def test_bridge_engine_used_without_sdk(self, monkeypatch):
        """桥已预热（engine 非 None）→ 直接用，不构建 SDK 引擎。"""
        rec = _RecordTransport()
        monkeypatch.setattr(bridge, "_BRIDGE", SimpleNamespace(engine=SimpleNamespace(transport=rec)))
        monkeypatch.setattr(bridge, "_host_sdk_transport", lambda: _RecordTransport())
        assert bridge.send_command_result_card("oc_1", "/llms", "模型列表") is True
        assert len(rec.calls) == 1

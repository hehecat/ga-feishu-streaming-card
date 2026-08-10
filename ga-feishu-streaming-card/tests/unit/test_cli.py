"""hfc CLI 测试：安装/状态/停用/卸载 + 插件独立导入触发链路。"""
import importlib
import json
import sys
import time
from pathlib import Path

import pytest

from ga_feishu_streaming_card.cli import main

PLUGIN_MOD = "plugins.hfc_bridge"


def _assert_engine_root_is_package_root(engine_root: str) -> None:
    """engine_root 须为存在目录且指向引擎包根（源码树 src/ 或 wheel site-packages 包目录）。"""
    root = Path(engine_root)
    assert root.is_dir(), f"engine_root 必须是存在的目录: {engine_root}"
    assert engine_root.endswith("/src") or engine_root.endswith(
        "ga_feishu_streaming_card"
    ), f"engine_root 必须指向源码树 src/ 或包目录 ga_feishu_streaming_card: {engine_root}"
    assert (
        (root / "ga_feishu_streaming_card" / "__init__.py").is_file()
        or (root / "__init__.py").is_file()
    ), f"engine_root 下必须存在可导入的 ga_feishu_streaming_card 包: {engine_root}"


def _make_fake_ga_root(tmp_path):
    """构造迷你 GA 根：plugins 包 + 可注册的 hooks。"""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "__init__.py").write_text("", encoding="utf-8")
    hooks = (
        "_REGS = {}\n"
        "def register(event):\n"
        "    def deco(fn):\n"
        "        _REGS.setdefault(event, []).append(fn)\n"
        "        return fn\n"
        "    return deco\n"
        "def trigger(event, ctx):\n"
        "    for fn in _REGS.get(event, []):\n"
        "        fn(ctx)\n"
        "def unregister(event, fn):\n"
        "    try: _REGS[event].remove(fn)\n"
        "    except (KeyError, ValueError): pass\n"
        "def has(event):\n"
        "    return bool(_REGS.get(event))\n"
        "def clear(): _REGS.clear()\n"
    )
    (plugins / "hooks.py").write_text(hooks, encoding="utf-8")
    return tmp_path


def _fresh_plugin(monkeypatch, root, enabled="1", cfg=None):
    cfg = cfg or {"enabled": True, "engine_root": str(
        __import__("ga_feishu_streaming_card").__path__[0])}
    cfg_path = root / ".hfc_config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("HFC_ENABLED", enabled)
    monkeypatch.setenv("HFC_CONFIG_PATH", str(cfg_path))
    monkeypatch.syspath_prepend(str(root))
    for m in [m for m in list(sys.modules) if m == PLUGIN_MOD or m.startswith("plugins")]:
        del sys.modules[m]
    from plugins import hooks  # noqa: E402  (sys.modules 已清，重导入)
    hooks.clear()
    return importlib.import_module(PLUGIN_MOD), hooks


class FakeEngine:
    def __init__(self):
        self.events = []

    def handle_event(self, ev):
        self.events.append(ev)
        return None


def _wait_events(fe, n, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(fe.events) >= n:
            return True
        time.sleep(0.02)
    return False


# ---------------- CLI 基础 ----------------

def test_cli_install_status_stop_uninstall(tmp_path):
    root = _make_fake_ga_root(tmp_path)
    ga = tmp_path / "ga"
    ga.mkdir()

    assert main(["install", "--ga-root", str(ga)]) == 0
    plugin = ga / "plugins" / "hfc_bridge.py"
    cfg = ga / ".hfc_config.json"
    assert plugin.exists() and cfg.exists()
    conf = json.loads(cfg.read_text(encoding="utf-8"))
    assert conf["enabled"] is True
    _assert_engine_root_is_package_root(conf["engine_root"])

    assert main(["status", "--ga-root", str(ga)]) == 0
    assert main(["stop", "--ga-root", str(ga)]) == 0
    assert json.loads(cfg.read_text(encoding="utf-8"))["enabled"] is False
    assert main(["status", "--ga-root", str(ga)]) == 1  # 已停用

    assert main(["uninstall", "--ga-root", str(ga)]) == 0
    assert not plugin.exists() and not cfg.exists()
    assert main(["status", "--ga-root", str(ga)]) == 1  # 未安装


def test_cli_module_entry(tmp_path):
    ga = tmp_path / "ga"
    ga.mkdir()
    from ga_feishu_streaming_card import cli
    assert cli.main(["status", "--ga-root", str(ga)]) == 1
    assert cli.main(["uninstall", "--ga-root", str(ga)]) == 0


# ---------------- 插件独立链路 ----------------

def test_plugin_import_registers_and_fires(monkeypatch, tmp_path):
    from ga_feishu_streaming_card import bridge

    root = _make_fake_ga_root(tmp_path)   # root 自身即 GA 根（含 plugins 包）
    assert main(["install", "--ga-root", str(root)]) == 0

    fe = FakeEngine()
    monkeypatch.setattr(bridge, "_make_engine", lambda cfg: fe)
    bridge.reset_for_test()
    pl, hooks = _fresh_plugin(monkeypatch, root)

    for ev in ("agent_before", "tool_before", "tool_after", "llm_after",
               "turn_after", "agent_after"):
        assert hooks.has(ev), f"hooks 未注册 {ev}"

    parent = type("Parent", (), {
        "_fs_active_task_id": "fs-task-2",
        "_fs_active_receive_id": "ou_dynamic_receiver",
    })()
    handler = type("Handler", (), {"parent": parent})()
    ctx = {"_hfc_event": "tool_before", "tool_name": "code_run", "args": {},
           "index": 0, "handler": handler, "user_input": None}
    hooks.trigger("tool_before", ctx)
    assert _wait_events(fe, 1)
    assert fe.events[0].data["status"] == "running"
    assert fe.events[0].conversation_id == "default:fs-task-2"
    assert fe.events[0].chat_id == "ou_dynamic_receiver"

    pl._uninstall()
    for ev in list(hooks._REGS):
        assert not hooks.has(ev)


def test_plugin_disabled_env_no_register(monkeypatch, tmp_path):
    root = _make_fake_ga_root(tmp_path)
    assert main(["install", "--ga-root", str(root)]) == 0
    _fresh_plugin(monkeypatch, root, enabled="0")
    from plugins import hooks
    assert not hooks.has("agent_before")

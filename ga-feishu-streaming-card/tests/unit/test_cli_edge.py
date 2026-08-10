"""CLI 边角测试：幂等安装、未安装干净退出、坏配置诊断、禁用态 e2e。"""
from __future__ import annotations

import json

import pytest

from ga_feishu_streaming_card import cli
from ga_feishu_streaming_card.cli import main


def _install(root, ga):
    return main(["install", "--ga-root", str(ga)])


class TestIdempotentInstall:
    def test_install_twice_ok(self, tmp_path):
        ga = tmp_path / "ga"
        ga.mkdir()
        assert _install(tmp_path, ga) == 0
        plugin = ga / "plugins" / "hfc_bridge.py"
        cfg = ga / ".hfc_config.json"
        before = plugin.read_bytes(), cfg.read_text(encoding="utf-8")
        # 二次安装不报错
        assert _install(tmp_path, ga) == 0
        assert plugin.exists() and cfg.exists()
        assert plugin.read_bytes() == before[0]
        assert json.loads(cfg.read_text(encoding="utf-8"))["enabled"] is True


class TestInstallAndStatusFailures:
    def test_install_write_error_is_actionable(self, tmp_path, monkeypatch, capsys):
        ga = tmp_path / "ga"
        ga.mkdir()
        monkeypatch.setattr(cli.Path, "write_bytes", lambda *_: (_ for _ in ()).throw(OSError("read-only")))
        assert main(["install", "--ga-root", str(ga)]) == 1
        out = capsys.readouterr().out
        assert "安装写入失败" in out and "--ga-root" in out and "traceback" not in out.lower()

    def test_status_warns_invalid_configured_engine_root(self, tmp_path, capsys):
        ga = tmp_path / "ga"
        plugin = ga / "plugins" / "hfc_bridge.py"
        plugin.parent.mkdir(parents=True)
        plugin.write_text("# installed", encoding="utf-8")
        (ga / ".hfc_config.json").write_text(
            json.dumps({"enabled": True, "engine_root": str(tmp_path / "missing")}), encoding="utf-8"
        )
        assert main(["status", "--ga-root", str(ga)]) == 1
        assert "engine_root 有效" in capsys.readouterr().out

    def test_status_accepts_src_and_site_packages_engine_roots(self, tmp_path, capsys):
        ga = tmp_path / "ga"
        plugin = ga / "plugins" / "hfc_bridge.py"
        plugin.parent.mkdir(parents=True)
        plugin.write_text("# installed", encoding="utf-8")
        src_root = tmp_path / "src"
        (src_root / "ga_feishu_streaming_card").mkdir(parents=True)
        (ga / ".hfc_config.json").write_text(
            json.dumps({"enabled": True, "engine_root": str(src_root)}), encoding="utf-8"
        )
        assert main(["status", "--ga-root", str(ga)]) == 0  # 有效根：全部 OK
        assert "[OK] 配置 engine_root 有效" in capsys.readouterr().out
        pkg_root = tmp_path / "ga_feishu_streaming_card"
        pkg_root.mkdir()
        (ga / ".hfc_config.json").write_text(
            json.dumps({"enabled": True, "engine_root": str(pkg_root)}), encoding="utf-8"
        )
        assert main(["status", "--ga-root", str(ga)]) == 0  # site-packages 样式根：全部 OK
        assert "[OK] 配置 engine_root 有效" in capsys.readouterr().out


class TestUninstalledCleanExit:
    def test_uninstall_when_not_installed(self, tmp_path, capsys):
        ga = tmp_path / "ga"
        ga.mkdir()
        assert main(["uninstall", "--ga-root", str(ga)]) == 0
        out = capsys.readouterr().out
        assert "未找到" in out  # 可读提示，非 traceback

    def test_stop_when_not_installed(self, tmp_path, capsys):
        ga = tmp_path / "ga"
        ga.mkdir()
        assert main(["stop", "--ga-root", str(ga)]) == 1
        out = capsys.readouterr().out
        assert "未安装" in out  # 可读诊断，非异常


class TestDiagnoseBadConfig:
    def test_diagnose_bad_yaml(self, tmp_path, monkeypatch, capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text("limits: [unclosed\n  : : :\n", encoding="utf-8")
        monkeypatch.setenv("HFC_CONFIG", str(bad))
        assert main(["diagnose"]) == 1
        out = capsys.readouterr().out
        assert "配置解析失败" in out

    def test_diagnose_missing_fields(self, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("transport: fake\n", encoding="utf-8")
        monkeypatch.setenv("HFC_CONFIG", str(cfg))
        assert main(["diagnose"]) == 1
        out = capsys.readouterr().out
        assert "缺少关键字段" in out

    def test_diagnose_good_config(self, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(
            "limits: {retention_seconds: 60}\n"
            "card_limits: {safe_bytes: 1000}\n"
            "transport: fake\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HFC_CONFIG", str(cfg))
        assert main(["diagnose"]) == 0
        out = capsys.readouterr().out
        assert "配置可解析" in out


class TestDisabledEnvE2e:
    def test_fake_e2e_ok_when_disabled(self, tmp_path, monkeypatch):
        """HFC_ENABLED=0 时 e2e 正常（引擎禁用不抛异常、CLI 完成）。"""
        monkeypatch.setenv("HFC_ENABLED", "0")
        ga = tmp_path / "ga"
        ga.mkdir()
        assert main(["fake-e2e", "--ga-root", str(ga)]) == 0

    def test_engine_returns_disabled(self, monkeypatch):
        from ga_feishu_streaming_card.config import EngineConfig
        from ga_feishu_streaming_card.engine import CardEngine
        from ga_feishu_streaming_card.events import CardEvent, EventType
        from ga_feishu_streaming_card.transport import FakeTransport

        monkeypatch.setenv("HFC_ENABLED", "0")
        cfg = EngineConfig.load()
        assert cfg.enabled is False
        eng = CardEngine(cfg=cfg, transport=FakeTransport())
        r = eng.handle_event(CardEvent(
            type=EventType.MESSAGE_STARTED, sequence=1, created_at=1.0,
            conversation_id="x", chat_id="oc_1",
        ))
        assert r.applied is False and r.reason == "disabled"
        assert eng.transport.calls == []  # 禁用态不投递


class TestGaRootProbeT13E:
    """T13-E F1：默认 GA 根探测链（env HFC_GA_ROOT → GA_ROOT/GA_HOME(带标志) → cwd → 上级链）。"""

    @staticmethod
    def _clear_env(monkeypatch):
        for var in ("HFC_GA_ROOT", "GA_ROOT", "GA_HOME"):
            monkeypatch.delenv(var, raising=False)

    def test_probe_env_hfc_ga_root_trusted(self, tmp_path, monkeypatch):
        root = tmp_path / "ga"
        root.mkdir()
        monkeypatch.setenv("HFC_GA_ROOT", str(root))
        assert cli._probe_ga_root() == root.resolve()

    def test_probe_env_ga_root_needs_marker(self, tmp_path, monkeypatch):
        plain = tmp_path / "plain"
        plain.mkdir()
        isolated = tmp_path / "isolated"
        isolated.mkdir()
        monkeypatch.chdir(isolated)
        self._clear_env(monkeypatch)
        monkeypatch.setenv("GA_ROOT", str(plain))
        assert cli._probe_ga_root() is None  # 无 AGENTS.md/plugins → 不采纳

    def test_probe_env_ga_root_with_plugins(self, tmp_path, monkeypatch):
        ga = tmp_path / "ga"
        (ga / "plugins").mkdir(parents=True)
        monkeypatch.setenv("GA_ROOT", str(ga))
        assert cli._probe_ga_root() == ga.resolve()

    def test_probe_cwd_agents_md(self, tmp_path, monkeypatch):
        (tmp_path / "AGENTS.md").write_text("# GA\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        self._clear_env(monkeypatch)
        assert cli._probe_ga_root() == tmp_path.resolve()

    def test_probe_parent_chain(self, tmp_path, monkeypatch):
        parent = tmp_path / "ga_root"
        parent.mkdir()
        (parent / "plugins").mkdir()
        deep = parent / "a" / "b"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        self._clear_env(monkeypatch)
        assert cli._probe_ga_root() == parent.resolve()

    def test_probe_all_fail_none(self, tmp_path, monkeypatch):
        isolated = tmp_path / "iso"
        isolated.mkdir()
        monkeypatch.chdir(isolated)
        self._clear_env(monkeypatch)
        assert cli._probe_ga_root() is None

    def test_main_missing_ga_root_errors_exit_nonzero(self, tmp_path, monkeypatch, capsys):
        isolated = tmp_path / "iso2"
        isolated.mkdir()
        monkeypatch.chdir(isolated)
        self._clear_env(monkeypatch)
        assert main(["status"]) == 1
        err = capsys.readouterr().err
        assert "--ga-root" in err and "GA_ROOT" in err

    def test_main_probe_via_parent_chain(self, tmp_path, monkeypatch, capsys):
        parent = tmp_path / "ga_root"
        parent.mkdir()
        (parent / "plugins").mkdir()
        deep = parent / "c" / "d"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        self._clear_env(monkeypatch)
        # 探测命中上级链 GA 根 → status 输出针对该根（未安装 → exit 1 且路径含 parent）
        assert main(["status"]) == 1
        out = capsys.readouterr().out
        assert str(parent.resolve()) in out


class TestInstallConfigT13E:
    """T13-E F2：install --config 复制到 GA 根 config.yaml。"""

    def test_install_with_config_copies(self, tmp_path):
        ga = tmp_path / "ga"
        ga.mkdir()
        cfg_src = tmp_path / "myconfig.yaml"
        cfg_src.write_text("transport: fake\n", encoding="utf-8")
        assert main(["install", "--ga-root", str(ga), "--config", str(cfg_src)]) == 0
        dst = ga / "config.yaml"
        assert dst.exists()
        assert dst.read_text(encoding="utf-8") == "transport: fake\n"
        # 插件与状态文件照常
        assert (ga / "plugins" / "hfc_bridge.py").exists()
        assert (ga / ".hfc_config.json").exists()

    def test_install_config_missing_file_fails(self, tmp_path, capsys):
        ga = tmp_path / "ga"
        ga.mkdir()
        assert main(["install", "--ga-root", str(ga),
                     "--config", str(tmp_path / "nope.yaml")]) == 1
        assert "不存在" in capsys.readouterr().out
        assert not (ga / "config.yaml").exists()
        assert not (ga / "plugins" / "hfc_bridge.py").exists()
        assert not (ga / ".hfc_config.json").exists()

"""CLI 路径安全：恶意 ga_root 仅作文件路径处理，不进入 shell。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ga_feishu_streaming_card.cli import main


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


@pytest.mark.parametrize(
    "hostile_name",
    [
        "ga;touch SHOULD_NOT_EXIST",
        "ga$(touch SHOULD_NOT_EXIST)",
        "ga`touch SHOULD_NOT_EXIST`",
        "ga with spaces & symbols",
    ],
)
def test_hostile_ga_root_metacharacters_are_literal(tmp_path, hostile_name):
    """Shell 元字符必须保持为目录名，不能触发命令执行。"""
    ga_root = tmp_path / hostile_name
    marker = tmp_path / "SHOULD_NOT_EXIST"

    assert main(["install", "--ga-root", str(ga_root)]) == 0
    assert (ga_root / "plugins" / "hfc_bridge.py").is_file()
    config = json.loads((ga_root / ".hfc_config.json").read_text(encoding="utf-8"))
    _assert_engine_root_is_package_root(config["engine_root"])
    assert not marker.exists()

    assert main(["status", "--ga-root", str(ga_root)]) == 0
    assert main(["stop", "--ga-root", str(ga_root)]) == 0
    assert main(["uninstall", "--ga-root", str(ga_root)]) == 0
    assert not marker.exists()


def test_ga_root_dotdot_is_normalized_without_escape(tmp_path):
    """含 ``..`` 的等价路径应解析到预期临时目录，生命周期不写旁处。"""
    container = tmp_path / "container"
    container.mkdir()
    ga_root = container / "discarded" / ".." / "target-ga"

    assert main(["install", "--ga-root", str(ga_root)]) == 0
    resolved = container / "target-ga"
    assert (resolved / "plugins" / "hfc_bridge.py").is_file()
    assert (resolved / ".hfc_config.json").is_file()
    assert not (tmp_path / "plugins" / "hfc_bridge.py").exists()

    assert main(["uninstall", "--ga-root", str(ga_root)]) == 0
    assert not (resolved / "plugins" / "hfc_bridge.py").exists()
    assert not (resolved / ".hfc_config.json").exists()

"""元测例（T7 改进轮）：交付元数据/文档与实现一致性。

- a) pyproject.toml 元数据完整可解析（name/version/hfc 入口/许可证字段）；
- b) README「CLI 命令一览」子命令与 cli.py 实际注册一致（运行时 --help 提取）；
- c) config.example.yaml 可被 config.py 解析为默认配置不报错。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11：pyproject 已声明 tomli 依赖（API 兼容）
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
README = ROOT / "README.md"
EXAMPLE = ROOT / "config.example.yaml"


def test_pyproject_metadata_complete():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    proj = data["project"]
    assert proj["name"] == "ga-feishu-streaming-card"
    assert isinstance(proj["version"], str) and proj["version"]
    assert proj["requires-python"]
    assert proj.get("scripts", {}).get("hfc") == "ga_feishu_streaming_card.cli:main"
    lic = proj.get("license")
    assert lic is not None  # 许可证字段存在（text 或 file 形式均可）
    assert ("text" in lic and "MIT" in lic["text"]) or ("file" in lic)


def test_readme_cli_subcommands_match_registration():
    # 运行时提取 cli.py 实际注册的子命令（--help 输出中的 subcommands 块）
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "ga_feishu_streaming_card.cli", "--help"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    m = re.search(r"positional arguments:\s*\n\s*\{(?P<cmds>[^}]+)\}", proc.stdout)
    assert m, f"无法从 --help 提取 subcommands：{proc.stdout!r}"
    registered = {c.strip() for c in m.group("cmds").split(",") if c.strip()}
    assert registered

    # README「CLI 命令一览」表格内的 `hfc <name>` 命令集合
    readme = README.read_text(encoding="utf-8")
    section = readme.split("## CLI 命令一览", 1)[1].split("## 配置字段", 1)[0]
    doc_cmds = set(re.findall(r"`hfc ([a-z][a-z0-9-]*)", section))
    assert doc_cmds, "README CLI 一览表未提取到任何子命令"
    assert doc_cmds == registered


def test_config_example_parses(monkeypatch):
    from ga_feishu_streaming_card.config import load_config

    # 清掉环境变量覆盖，保证解析结果只来自示例文件
    for k in ("HFC_ENABLED", "HFC_TRANSPORT", "HFC_BASE_URL", "HFC_HTTP_TIMEOUT_MS", "HFC_CONFIG"):
        monkeypatch.delenv(k, raising=False)

    cfg = load_config(EXAMPLE)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.transport == "fake"
    assert cfg.delivery.default == "card"
    assert cfg.http.base_url == "https://open.feishu.cn"

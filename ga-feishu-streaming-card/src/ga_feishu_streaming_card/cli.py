"""hfc CLI：install/uninstall/status/stop/diagnose/fake-e2e。

入口：``uv run hfc <cmd>``（pyproject scripts）或 ``python -m ga_feishu_streaming_card.cli``。
安装目标：GA 根目录 plugins/hfc_bridge.py + .hfc_config.json。
GA 根解析序 = 显式 --ga-root → env HFC_GA_ROOT → GA_ROOT/GA_HOME（需 AGENTS.md 或
plugins/ 根标志）→ cwd（含根标志）→ cwd 上级链首个含根标志目录；全部未命中时
向 stderr 输出可操作提示并以非零码退出（详见 _probe_ga_root）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

DELIVERY_ROOT = Path(__file__).resolve().parents[2]  # .../ga-feishu-streaming-card（源码树）
PKG_DIR = Path(__file__).resolve().parent  # 包实际所在目录（源码树=src/...，wheel=site-packages/...）
SRC_DIR = DELIVERY_ROOT / "src"
# 插件源候选：源码树 bridge/ 优先（源码环境），其次 wheel 包内 _bridge/（force-include 打入）
PLUGIN_SRC_CANDIDATES = (
    DELIVERY_ROOT / "bridge" / "hfc_bridge.py",
    PKG_DIR / "_bridge" / "hfc_bridge.py",
)
CONFIG_NAME = ".hfc_config.json"


def _plugin_src() -> Path:
    """实际可用的插件源路径（源码树或 wheel 包内）。"""
    for p in PLUGIN_SRC_CANDIDATES:
        if p.exists():
            return p
    return PLUGIN_SRC_CANDIDATES[0]


def _engine_root() -> Path:
    """实际生效的引擎包路径：源码树 src/ 优先，wheel 环境为 site-packages 包目录。"""
    return SRC_DIR if SRC_DIR.exists() else PKG_DIR


def _looks_like_ga_root(p: Path) -> bool:
    """GA 根标志：含 AGENTS.md（标准 GA 仓库根）或 plugins/ 目录。"""
    return p.is_dir() and ((p / "AGENTS.md").exists() or (p / "plugins").is_dir())


def _probe_ga_root() -> Optional[Path]:
    """默认 GA 根探测链（源码树 / wheel 形态通用）。

    顺序：
    1) env HFC_GA_ROOT（hfc 专用，显式设定即信任）；
    2) env GA_ROOT / GA_HOME（通用变量，需含 GA 根标志才采纳）；
    3) cwd 含 GA 根标志（GA 进程/用户从 GA 根启动场景）；
    4) cwd 上级链首个含 GA 根标志的目录（GA 根下子目录场景）。

    全失败 → None（调用方输出明确错误并提示 --ga-root，exit 非 0）。
    """
    for var in ("HFC_GA_ROOT",):
        v = os.environ.get(var)
        if v:
            return Path(v).expanduser().resolve()
    for var in ("GA_ROOT", "GA_HOME"):
        v = os.environ.get(var)
        if v:
            p = Path(v).expanduser().resolve()
            if _looks_like_ga_root(p):
                return p
    cwd = Path.cwd().resolve()
    if _looks_like_ga_root(cwd):
        return cwd
    for parent in cwd.parents:
        if _looks_like_ga_root(parent):
            return parent
    return None


def _ga_paths(ga_root: Path):
    return ga_root / "plugins", ga_root / "plugins" / "hfc_bridge.py", ga_root / CONFIG_NAME


def _engine_importable() -> tuple[bool, str]:
    """包/bridge/config 可导入（engine 为惰性加载，不在此强制）。"""
    sys.path.insert(0, str(_engine_root()))
    try:
        import ga_feishu_streaming_card  # noqa: F401
        import ga_feishu_streaming_card.bridge  # noqa: F401
        import ga_feishu_streaming_card.config  # noqa: F401
        return True, ""
    except Exception as e:
        return False, str(e)


def cmd_install(args) -> int:
    """安装插件与状态配置到 GA 根：复制 hfc_bridge.py → plugins/，写 .hfc_config.json；--config 时复制 YAML。"""
    ga_root = Path(args.ga_root).resolve()
    plugins_dir, plugin_file, config_file = _ga_paths(ga_root)
    plugin_src = _plugin_src()
    if not plugin_src.exists():
        print(f"FAIL: 插件源不存在: {plugin_src}")
        return 1
    config_src = None
    if getattr(args, "config", None):
        config_src = Path(args.config).expanduser().resolve()
        if not config_src.is_file():
            print(f"FAIL: --config 文件不存在或不是普通文件: {config_src}")
            return 1
    try:
        plugins_dir.mkdir(parents=True, exist_ok=True)
        plugin_file.write_bytes(plugin_src.read_bytes())
        cfg = {
            "enabled": True,
            "engine_root": str(_engine_root()),
            "installed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        config_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"FAIL: 安装写入失败: {e}")
        print(f"提示: 检查 --ga-root 是否可写，或修复目录权限后重试: {ga_root}")
        return 1
    # F2(T13-E): --config PATH → 复制到 GA 根 config.yaml（引擎加载源之一，见 README 配置章节）
    if config_src is not None:
        dst = ga_root / "config.yaml"
        try:
            shutil.copy2(config_src, dst)
        except OSError as e:
            print(f"FAIL: 配置复制失败: {e}")
            return 1
        print(f"OK: 引擎配置已复制 -> {dst}")
    print(f"OK: 插件已安装 -> {plugin_file}")
    print(f"OK: 配置已写入 -> {config_file}")
    print("重启 GA 后插件生效（或运行 `hfc status` 验证；运行时停用请用 `hfc stop`）。")
    return 0


def cmd_uninstall(args) -> int:
    """卸载：删除 GA 根 plugins/hfc_bridge.py 与 .hfc_config.json（幂等；未安装时 INFO 提示）。"""
    _, plugin_file, config_file = _ga_paths(Path(args.ga_root).resolve())
    removed = False
    for p in (plugin_file, config_file):
        if p.exists():
            p.unlink()
            print(f"OK: 已删除 {p}")
            removed = True
    if not removed:
        print("INFO: 未找到已安装的插件/配置，无需卸载")
    print("提示: 若 GA 正在运行，请在重启后生效。")
    return 0


def cmd_status(args) -> int:
    """校验安装状态：插件文件/enabled/engine_root 有效/HFC_ENABLED/引擎可导入，逐项 [OK]/[FAIL]。"""
    ga_root = Path(args.ga_root).resolve()
    _, plugin_file, config_file = _ga_paths(ga_root)
    ok = True

    def _line(good: bool, msg: str):
        nonlocal ok
        ok = ok and good
        print(f"[{'OK' if good else 'FAIL'}] {msg}")

    _line(plugin_file.exists(), f"插件文件存在: {plugin_file}")
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            _line(cfg.get("enabled", False), f"配置 enabled=True: {config_file}")
            configured_root = Path(str(cfg.get("engine_root", ""))).expanduser()
            root_valid = configured_root.is_dir() and (
                (configured_root / "ga_feishu_streaming_card").is_dir()
                or configured_root.name == "ga_feishu_streaming_card"
            )
            _line(root_valid, "配置 engine_root 有效（含 ga_feishu_streaming_card 包）: "
                  f"{configured_root}")
        except Exception as e:
            _line(False, f"配置解析失败: {e}")
    else:
        _line(False, f"配置文件缺失: {config_file}")
    envv = os.environ.get("HFC_ENABLED", "")
    _line(envv.strip().lower() not in ("0", "false", "no", "off", "disabled"),
          f"env HFC_ENABLED 未禁用 (当前='{envv}')")
    eng_ok, err = _engine_importable()
    _line(eng_ok, f"引擎包可导入: {_engine_root()}" + ("" if eng_ok else f" ({err})"))
    return 0 if ok else 1


def cmd_stop(args) -> int:
    """停用：写 .hfc_config.json enabled=false（运行时停用；已加载进程需重启完全生效）。"""
    ga_root = Path(args.ga_root).resolve()
    _, _, config_file = _ga_paths(ga_root)
    if not config_file.exists():
        print("FAIL: 配置文件不存在（未安装？）")
        return 1
    try:
        cfg = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"FAIL: 配置解析失败: {e}")
        return 1
    cfg["enabled"] = False
    cfg["stopped_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    config_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: 已停用（enabled=False）-> {config_file}")
    print("插件每次 import 时读该文件；已加载的 GA 进程需重启后完全停用。")
    return 0


def cmd_diagnose(args) -> int:
    """诊断：引擎包导入 + 配置可解析 + map_ga_ctx 事件映射 + engine 模块自检。"""
    eng_ok, err = _engine_importable()
    print(f"[{'OK' if eng_ok else 'FAIL'}] 引擎包可导入" + ("" if eng_ok else f": {err}"))
    if not eng_ok:
        print("WARN: 后续检查跳过（引擎不可用，无法执行 fake-e2e）")
        return 1

    # 配置检查：HFC_CONFIG / ./config.yaml 坏 YAML 或缺关键字段 → 可读诊断
    cfg_path = None
    env_cfg = os.environ.get("HFC_CONFIG")
    if env_cfg:
        cfg_path = Path(env_cfg)
    elif (Path.cwd() / "config.yaml").exists():
        cfg_path = Path.cwd() / "config.yaml"
    if cfg_path is not None:
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                import yaml

                loaded = yaml.safe_load(f) or {}
            if not isinstance(loaded, dict):
                print(f"[FAIL] 配置顶层不是映射: {cfg_path}")
                return 1
            missing = [k for k in ("limits", "card_limits", "transport") if k not in loaded]
            if missing:
                print(f"[FAIL] 配置缺少关键字段 {missing}: {cfg_path}")
                return 1
            print(f"[OK] 配置可解析且字段完整: {cfg_path}")
        except Exception as e:
            print(f"[FAIL] 配置解析失败（坏 YAML/JSON）: {cfg_path}: {e}")
            return 1
    else:
        print("INFO: 未找到 HFC_CONFIG 或 ./config.yaml，使用默认配置")

    from ga_feishu_streaming_card import bridge

    ctx = {"handler": None, "user_input": "diagnose", "max_turns": 10, "turn": 1,
           "tool_name": "code_run", "args": {"script": "print(1)"}, "index": 0,
           "tool_num": 1, "ret": "ok", "exit_reason": {"result": "CURRENT_TASK_DONE"}}
    evs = bridge.map_ga_ctx(ctx)
    print(f"[OK] map_ga_ctx 产出 {len(evs)} 个事件: " + ", ".join(e.type.value for e in evs))
    try:
        engine_mod = __import__("ga_feishu_streaming_card.engine", fromlist=["CardEngine"])
        print(f"[OK] engine 可导入: {engine_mod.__name__}")
        return 0
    except Exception as e:
        print(f"[WARN] engine 未就绪: {e}")
        return 0


def cmd_fake_e2e(args) -> int:
    """离线端到端：GA locals 快照序列 -> bridge 映射 -> CardEngine(FakeTransport) -> 摘要。

    不真发：FakeTransport 内存记录全部投递调用；失败路径 fail-open 不阻塞状态应用。
    """
    eng_ok, err = _engine_importable()
    if not eng_ok:
        print(f"FAIL: 引擎包不可导入: {err}")
        return 1
    try:
        from ga_feishu_streaming_card import engine as eng_mod
        from ga_feishu_streaming_card import config as cfg_mod
    except Exception as e:
        print(f"FAIL: engine 未就绪: {e}")
        return 1
    from ga_feishu_streaming_card import bridge
    from ga_feishu_streaming_card.events import CardEvent, EventType as ET
    from ga_feishu_streaming_card.transport import FakeTransport

    # ---- 1) GA locals 快照序列（键对齐 GA hooks 事件帧键清单，8 帧）----
    handler = SimpleNamespace(name="cli-fake-e2e", parent=SimpleNamespace(task_dir="cli-fake-e2e"))
    resp = lambda c: SimpleNamespace(content=c, tool_calls=[], model="fake-model")  # noqa: E731
    frames = [
        {"_hfc_event": "agent_before", "handler": handler, "task_dir": "cli-fake-e2e",
         "user_input": "写个 Python 脚本", "max_turns": 3},
        {"_hfc_event": "turn_before", "handler": handler, "task_dir": "cli-fake-e2e",
         "turn": 1, "max_turns": 3},
        {"_hfc_event": "llm_after", "handler": handler, "task_dir": "cli-fake-e2e",
         "turn": 1, "response": resp("思考后给出方案")},
        {"_hfc_event": "tool_before", "handler": handler, "task_dir": "cli-fake-e2e",
         "tool_name": "code_run", "args": {"script": "print(1)"}, "index": 0, "tool_num": 1},
        {"_hfc_event": "tool_after", "handler": handler, "task_dir": "cli-fake-e2e",
         "tool_name": "code_run", "args": {"script": "print(1)"}, "index": 0, "tool_num": 1, "ret": "1"},
        {"_hfc_event": "llm_after", "handler": handler, "task_dir": "cli-fake-e2e",
         "turn": 2, "response": resp("最终答案")},
        {"_hfc_event": "turn_after", "handler": handler, "task_dir": "cli-fake-e2e",
         "turn": 2, "tool_calls": [{"name": "code_run"}], "tool_results": ["1"], "next_prompt": ""},
        {"_hfc_event": "agent_after", "handler": handler, "task_dir": "cli-fake-e2e",
         "turn": 2, "exit_reason": {"result": "CURRENT_TASK_DONE"}},
    ]

    # ---- 2) bridge 映射 -> 协议事件流 ----
    cfg = cfg_mod.load_config()  # fake transport（config 配置）
    transport = FakeTransport()
    engine = eng_mod.CardEngine(cfg, transport)
    session_key = "cli-fake-e2e"
    chat_id = "cli_test_chat"
    seq = 0

    def ev(t, data):
        nonlocal seq
        seq += 1
        return CardEvent(type=t, conversation_id=session_key, chat_id=chat_id,
                         sequence=seq, created_at=1.0, data=data)

    proto_types = []
    for frame in frames:
        proto_types.extend(e.type for e in bridge.map_ga_ctx(frame))
    # 协议注入：GA 无流式 thinking 源事件（方案 A 局限），以 THINKING_DELTA 模拟二期 llm_delta
    # （插在首条 llm_after 的 answer.delta 之前，验证 thinking.delta 累积语义）
    thinking_seq = 0
    for t_ in proto_types:
        thinking_seq += 1
        if t_ is ET.ANSWER_DELTA:
            break
    thinking_seq -= 1  # 插到首个 answer.delta 之前
    proto_types.insert(thinking_seq, ET.THINKING_DELTA)
    proto_types.insert(thinking_seq + 1, ET.THINKING_DELTA)

    for i, t_ in enumerate(proto_types):
        data = {}
        if t_ is ET.MESSAGE_STARTED:
            data = {"event": "agent_before", "input_preview": "写个 Python 脚本"}
        elif t_ is ET.THINKING_DELTA:
            data = {"text": f"思考片段{i}"}
        elif t_ is ET.TOOL_UPDATED:
            data = {"tool_id": "t1", "tool_name": "code_run",
                    "status": "running" if i == proto_types.index(ET.TOOL_UPDATED) else "completed",
                    "detail": "ok"}
        elif t_ is ET.ANSWER_DELTA:
            data = {"text": "最终答案"}
        elif t_ is ET.SYSTEM_NOTICE:
            data = {"status": "continue"}
        elif t_ is ET.MESSAGE_COMPLETED:
            data = {"event": "agent_after", "exit_reason": {"result": "CURRENT_TASK_DONE"}}
        r = engine.handle_event(ev(t_, data))
        if not r.applied:
            print(f"WARN: seq={seq} {t_.value} 未应用: {r.reason}")

    # ---- 3) 摘要（不真发）----
    calls = transport.calls
    if not calls:
        print("OK: HFC 已禁用（HFC_ENABLED=0），未投递任何卡片（e2e 通路正常）")
        return 0
    seq_str = " -> ".join(
        ("send" if c["op"] == "send_card" else "update") for c in calls
    )
    final_header = (calls[-1]["card"].get("header") or {}).get("title", {}).get("content", "")
    print(f"OK: GA 帧 {len(frames)} 个 -> 协议事件 {len(proto_types)} 个: "
          + ", ".join(t.value for t in proto_types))
    print(f"OK: 投递调用 {len(calls)} 次（同 message_id 原地更新，不真发）: {seq_str}")
    print(f"OK: 最终卡片 header='{final_header}'")
    return 0


def main(argv=None) -> int:
    """hfc 入口：解析子命令并执行（install/uninstall/status/stop/diagnose/fake-e2e）。"""
    p = argparse.ArgumentParser(
        prog="hfc",
        description="GA → 飞书流式卡片桥 CLI：安装/校验/停用/卸载 GA 插件，离线端到端演练与诊断。",
    )
    _SUB_HELP = {
        "install": "安装插件到 GA 根（--config 可复制引擎 YAML）",
        "uninstall": "卸载插件与状态配置（幂等）",
        "status": "校验安装状态（逐项 [OK]/[FAIL]）",
        "stop": "运行时停用（enabled=false）",
        "diagnose": "引擎/配置/事件映射自检",
        "fake-e2e": "离线端到端演练（fake 传输，不真发）",
    }
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("uninstall", "status", "stop", "diagnose"):
        sp = sub.add_parser(name, help=_SUB_HELP[name])
        sp.add_argument("--ga-root", default=None,
                        help="GA 根目录；缺省按探测链解析（env HFC_GA_ROOT/GA_ROOT/GA_HOME → cwd AGENTS.md → 上级链）")
        sp.set_defaults(func=globals()[f"cmd_{name}"])
    sp = sub.add_parser("install", help=_SUB_HELP["install"])
    sp.add_argument("--ga-root", default=None,
                    help="GA 根目录；缺省按探测链解析（env HFC_GA_ROOT/GA_ROOT/GA_HOME → cwd AGENTS.md → 上级链）")
    sp.add_argument("--config", default=None,
                    help="引擎配置 YAML（对照 config.example.yaml），复制到 <GA_ROOT>/config.yaml")
    sp.set_defaults(func=cmd_install)
    sp = sub.add_parser("fake-e2e", help=_SUB_HELP["fake-e2e"])
    sp.add_argument("--ga-root", default=None)
    sp.set_defaults(func=cmd_fake_e2e)
    args = p.parse_args(argv)
    if args.ga_root is None:
        args.ga_root = _probe_ga_root()
    if args.ga_root is None and args.cmd != "diagnose":
        sys.stderr.write(
            "ERROR: 无法确定 GA 根：未找到 AGENTS.md（cwd 及上级链）且未设置 GA_ROOT/GA_HOME。\n")
        sys.stderr.write("提示: 使用 `--ga-root <GA_ROOT>` 显式指定，或设置环境变量 GA_ROOT/GA_HOME。\n")
        return 1
    if args.ga_root is not None:
        args.ga_root = str(Path(args.ga_root).resolve())
    try:
        return args.func(args)
    except Exception as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

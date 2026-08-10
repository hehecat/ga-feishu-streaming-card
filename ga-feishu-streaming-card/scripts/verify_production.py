#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_production.py — T29 一键验收脚本（只读探测生产，不修改任何状态）。

用法（在 GA 根目录下，用部署环境解释器）:
    .venv/bin/python temp/ga-feishu-streaming-card/scripts/verify_production.py

检查项:
  1. PORT_LISTEN        8898 监听且为 fsapp 进程（/proc 只读核验 cwd+cmdline）
  2. WS_CONNECTED       fsapp 日志尾部出现飞书长连接 connected
  3. HTTP_ACTIONS       回调端点 GET /card/actions 可达（200 + JSON）
  4. URL_VERIFY_GUARD   回调端点安全守卫（无 token 请求应被拒 403，不落密钥）
  5. WHITELIST          命令白名单（ast 只读解析 fsapp.py 源码，含六命令+/settings+/status）
  6. SMOKE_SELECT       plaintext /llms → body.elements 顶层 select_static 下拉协议
  7. RENDER_2X2         命令结果卡按钮 2×2：两个顶层 column_set × 每个两列、每列一 button
                        （T27-G 飞书卡片 2.0 结构；存在 action 容器即判为 1.0 回退）

约束: 不落密钥明文（不读 mykey.py）、只读不写（不写任何文件/状态）。
退出码: 全 PASS=0，任一 FAIL=1。
"""
from __future__ import annotations

import ast
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
from collections import deque
from pathlib import Path

PORT = 8898
GA_ROOT_CANDIDATES = [
    Path(os.environ.get("HFC_GA_ROOT", "")),
    Path(__file__).resolve().parents[2],  # scripts/../.. = <GA>/temp/ga-feishu-streaming-card 的上级两跳
]
GA_ROOT = next((p for p in GA_ROOT_CANDIDATES if p and p.is_dir() and (p / "frontends" / "fsapp.py").exists()), None)
if GA_ROOT is None:
    sys.exit("ERROR: 未找到 GA 根目录（无 frontends/fsapp.py）。请传 HFC_GA_ROOT 或在 GA 根下运行本脚本。")
GA_ROOT = GA_ROOT.resolve()
FSA_APP_PY = GA_ROOT / "frontends" / "fsapp.py"
LOG = GA_ROOT / "temp" / "fsapp_prod_20260809.log"
# PID 不写死：PORT_LISTEN 以 ss 实测监听 PID + /proc cwd/cmdline 双重身份校验（见 check_port_listen）。

REQUIRED_WHITELIST = {"/new", "/reset", "/undo", "/llm", "/llms", "/model", "/settings", "/status"}
EXPECTED_BUTTONS = [("新会话", "/new"), ("切换模型", "/model"), ("设置", "/settings"), ("状态", "/status")]
LLMS_PLAINTEXT = "LLMs:\n→ [0] deepseek-v4-flash\n  [1] gpt-5.6-luna"
EXPECTED_MODELS = {"deepseek-v4-flash", "gpt-5.6-luna"}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- 1. 端口监听
def port_pid() -> str | None:
    try:
        out = subprocess.run(
            ["ss", "-H", "-ltnp", f"sport = :{PORT}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = re.search(r"pid=(\d+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def is_fsapp_process(pid: str) -> bool:
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        cmd = (Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace"))
        return cwd == str(GA_ROOT) and "frontends/fsapp.py" in cmd
    except Exception:
        return False


def check_port_listen() -> None:
    pid = port_pid()
    if not pid:
        check("PORT_LISTEN", False, f"8898 无监听（ss sport=:{PORT} 无 pid）")
        return
    if not is_fsapp_process(pid):
        check("PORT_LISTEN", False, f"8898 监听进程 pid={pid} 不是 fsapp（cwd/cmdline 不符），疑似被占用")
        return
    check("PORT_LISTEN", True, f"pid={pid} cwd={GA_ROOT}")


# ---------------------------------------------------------------- 2. WebSocket 长连接
def check_ws_connected() -> None:
    if not LOG.exists():
        check("WS_CONNECTED", False, f"日志不存在: {LOG}")
        return
    size = LOG.stat().st_size
    tail_bytes = 64 * 1024
    with LOG.open("rb") as f:
        f.seek(max(0, size - tail_bytes))
        tail = f.read().decode("utf-8", "replace")
    pat = r"connected to wss://msg-frontier\.feishu\.cn|飞书 Agent 已启动（长连接模式）"
    if re.search(pat, tail):
        check("WS_CONNECTED", True, "日志尾部见长连接 connected")
    else:
        check("WS_CONNECTED", False, "日志尾部未见 connected（长连接未建立，查 B1 排查）")


# ---------------------------------------------------------------- 3/4. 回调端点（只读 GET）
def http_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def check_http_actions() -> None:
    status, body = http_get(f"http://127.0.0.1:{PORT}/card/actions")
    if status == 200:
        try:
            json.loads(body)
            check("HTTP_ACTIONS", True, "GET /card/actions → 200 + JSON")
        except ValueError:
            check("HTTP_ACTIONS", False, f"200 但 body 非 JSON: {body[:80]!r}")
    else:
        check("HTTP_ACTIONS", False, f"GET /card/actions → {status} {body[:80]!r}")


def check_url_verification() -> None:
    # 安全守卫语义（不落密钥）：fs_verification_token 已配置时，无 token 的
    # url_verification 请求必须被拒（403 token mismatch）；若 200 回显 challenge
    # 说明 token 未配置（安全降级）。真实带 token 回显由飞书保存校验自动完成。
    challenge = "hfc-verify-probe"
    status, body = http_get(
        f"http://127.0.0.1:{PORT}/card/actions?type=url_verification&challenge={challenge}"
    )
    if status == 200 and "token mismatch" in body:
        check("URL_VERIFY_GUARD", True, "无 token 请求被拒（fs_verification_token 守卫生效）")
    elif status == 200 and challenge in body:
        check("URL_VERIFY_GUARD", False, "无 token 请求即回显 challenge——token 未配置，安全降级（按 A6 配置并重启 fsapp）")
    else:
        check("URL_VERIFY_GUARD", False, f"异常响应 status={status} body={body[:80]!r}")


# ---------------------------------------------------------------- 5. 命令白名单（ast 只读解析源码）
def check_whitelist() -> None:
    if not FSA_APP_PY.exists():
        check("WHITELIST", False, f"fsapp.py 不存在: {FSA_APP_PY}")
        return
    tree = ast.parse(FSA_APP_PY.read_text(encoding="utf-8"))
    whitelist: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "CARD_ACTIONS_WHITELIST" and isinstance(node.value, ast.Set):
                    whitelist = {e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    missing = REQUIRED_WHITELIST - whitelist
    if missing:
        check("WHITELIST", False, f"白名单缺: {sorted(missing)}")
    else:
        check("WHITELIST", True, f"含六命令+/settings+/status（共 {len(whitelist)} 项: {sorted(whitelist)}）")


# ---------------------------------------------------------------- 6/7. 渲染结构（进程内只读渲染）
def _render_llms_card() -> dict:
    try:
        from ga_feishu_streaming_card.command import render_command_result_card
    except Exception as e:
        raise RuntimeError(f"import ga_feishu_streaming_card 失败（未部署?）: {e}")
    card = render_command_result_card(LLMS_PLAINTEXT, "/llms")
    if not isinstance(card, dict):
        raise RuntimeError(f"render 返回非 dict: {type(card)}")
    return card


def _walk(elements, tags: set[str]):
    """按文档顺序遍历（deque FIFO），收集指定 tag 的元素。"""
    out = []
    stack = deque(elements)
    while stack:
        e = stack.popleft()
        if not isinstance(e, dict):
            continue
        if e.get("tag") in tags:
            out.append(e)
        for v in e.values():
            if isinstance(v, list):
                stack.extend(v)
            elif isinstance(v, dict):
                stack.append(v)
    return out


def _button_text(btn: dict) -> str:
    t = btn.get("text", {})
    if isinstance(t, dict):
        return str(t.get("content", ""))
    return str(t)


def _body_elements(card: dict) -> list:
    """T27-G 卡片 2.0 的唯一验收入口；拒绝回退到 1.0 顶层 elements。"""
    body = card.get("body")
    return body.get("elements", []) if isinstance(body, dict) else []


def check_smoke_select(card: dict) -> None:
    elements = _body_elements(card)
    selects = [e for e in elements if isinstance(e, dict) and e.get("tag") == "select_static"]
    if len(selects) != 1:
        check("SMOKE_SELECT", False, f"body.elements 顶层 select_static 数={len(selects)}（预期 1）")
        return
    options = selects[0].get("options", [])
    values = [o.get("value") for o in options if isinstance(o, dict)]
    texts = [o.get("text", {}).get("content") if isinstance(o.get("text"), dict) else o.get("text") for o in options if isinstance(o, dict)]
    ok = EXPECTED_MODELS.issubset(set(values)) and texts == values and len(values) == len(options)
    check("SMOKE_SELECT", ok, f"body.elements 顶层 values={values}（含当前模型，option文本==值）" if ok else f"协议不符 values={values} texts={texts}")


def check_render_2x2(card: dict, whitelist_ok: bool) -> None:
    # T27-G：飞书卡片 2.0 使用两个顶层 column_set，分别代表两行；每行恰两列、每列恰一 button。
    problems = []
    elements = _body_elements(card)
    if card.get("schema") != "2.0":
        problems.append(f"schema={card.get('schema')!r}（预期 '2.0'）")
    actions = _walk(elements, {"action"})
    if actions:
        problems.append(f"存在 action 容器 {len(actions)} 个（1.0 结构回退）")
    rows = [e for e in elements if isinstance(e, dict) and e.get("tag") == "column_set"]
    if len(rows) != 2:
        problems.append(f"顶层 column_set 行数={len(rows)}（预期 2）")
    flat_buttons = []
    for row_i, row in enumerate(rows):
        columns = row.get("columns", [])
        if len(columns) != 2:
            problems.append(f"行{row_i + 1}列数={len(columns)}（预期 2）")
            continue
        for col_i, column in enumerate(columns):
            if not isinstance(column, dict) or column.get("tag") != "column":
                problems.append(f"行{row_i + 1}列{col_i + 1}不是 column")
                continue
            buttons = column.get("elements", [])
            if len(buttons) != 1 or not isinstance(buttons[0], dict) or buttons[0].get("tag") != "button":
                problems.append(f"行{row_i + 1}列{col_i + 1}元素非恰1个 button")
                continue
            flat_buttons.append(buttons[0])
    got = [(_button_text(b), (b.get("value") or {}).get("action")) for b in flat_buttons]
    if got != EXPECTED_BUTTONS:
        problems.append(f"按钮序/文案不符 got={got} exp={EXPECTED_BUTTONS}")
    t11_bad = []
    for b in flat_buttons:
        v = b.get("value") or {}
        if v.get("hfc") != 1 or not isinstance(v.get("action"), str) or not v.get("action"):
            t11_bad.append(v)
    if t11_bad:
        problems.append(f"T11 value 协议不符: {t11_bad}")
    check(
        "RENDER_2X2",
        not problems,
        "; ".join(problems)
        if problems
        else "body.elements 两个 column_set×每行2列×每列1 button，文案/顺序/T11 value 全符合（无 action 容器）",
    )


# ---------------------------------------------------------------- main
def main() -> int:
    print(f"verify_production.py  GA_ROOT={GA_ROOT}  PORT={PORT}  (只读探测，不修改任何状态)\n")
    check_port_listen()
    check_ws_connected()
    check_http_actions()
    check_url_verification()
    check_whitelist()
    whitelist_ok = results[-1][1]
    try:
        card = _render_llms_card()
    except RuntimeError as e:
        check("SMOKE_SELECT", False, str(e))
        check("RENDER_2X2", False, str(e))
    else:
        check_smoke_select(card)
        check_render_2x2(card, whitelist_ok)
    print()
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print(f"SUMMARY: {len(results) - len(failed)}/{len(results)} PASS — FAIL: {failed}")
        print("排查入口: 见 docs/ACCEPTANCE_CHECKLIST.md 对应项；渲染相关 FAIL 多为部署版本未到 T27 系列。")
        return 1
    print(f"SUMMARY: {len(results)}/{len(results)} PASS — 生产就绪，可进行真实飞书验收（docs/ACCEPTANCE_CHECKLIST.md §B）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

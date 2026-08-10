"""T8-B 对抗性难测例（发布/对抗性测试工程师轮）。

覆盖四类此前未跑过的对抗场景：
a) 并发生命周期: 多线程并发对同/不同 session 调用生命周期（start/update/finish），
   无异常、状态机不破、终态后拒变（session.py:185,190 终态检查）。
b) 极端 unicode: emoji/零宽字符/RTL/超长文本（>100KB）累积与渲染不崩溃，
   产物 JSON 合法；超过 MAX_TEXT_CHARS 时按字符裁剪并带截断标记。
c) 超大工具状态: detail/name 超限裁剪（TOOL_DETAIL_MAX/TOOL_NAME_MAX），
   工具条数超 MAX_TOOLS 后忽略新工具（session.py:159-160 上限）。
d) 配置边界: 空 base_url / 无 scheme base_url / 畸形 limits 的 load_config
   不抛异常（畸形字段被 _coerce_field 拒绝回退默认）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ga_feishu_streaming_card.config import EngineConfig, load_config
from ga_feishu_streaming_card.engine import CardEngine
from ga_feishu_streaming_card.events import CardEvent, EventType
from ga_feishu_streaming_card.session import (
    MAX_TEXT_CHARS,
    MAX_TOOLS,
    TOOL_DETAIL_MAX,
    TOOL_NAME_MAX,
    _TRUNC_MARK,
)
from ga_feishu_streaming_card.transport import FakeTransport


def _ev(etype: EventType, seq: int, conv: str, chat: str, msg: str, data: dict) -> CardEvent:
    return CardEvent(
        type=etype,
        sequence=seq,
        created_at=time.time(),
        conversation_id=conv,
        chat_id=chat,
        message_id=msg,
        data=data,
    )


# ---------------------------------------------------------------- a) 并发生命周期

def test_concurrent_lifecycle_same_session_no_break():
    """意图: 8 线程对同一 session 并发 start/delta/complete，无异常且终态后拒变。"""
    eng = CardEngine(transport=FakeTransport())
    conv, chat, msg = "conv-race-1", "chat-1", "om_race_1"
    seq = 0
    seq_lock = threading.Lock()
    errors: list = []

    def send(etype: EventType, data: dict) -> None:
        nonlocal seq
        with seq_lock:
            seq += 1
            s = seq
        eng.handle_event(_ev(etype, s, conv, chat, msg, data))

    def worker(i: int) -> None:
        try:
            send(EventType.MESSAGE_STARTED, {"message_id": msg})
            for k in range(40):
                send(EventType.ANSWER_DELTA, {"text": f"w{i}-{k};"})
            send(EventType.MESSAGE_COMPLETED, {"final_text": f"done-{i}"})
        except Exception as e:  # noqa: BLE001 - 对抗测例：任何异常都记为失败
            errors.append(e)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(worker, range(8)))

    assert errors == [], f"并发生命周期出现异常: {errors}"
    sess = eng.sessions[conv]
    assert sess.status == "completed"  # 至少一个 complete 已应用 → 终态
    before = sess.answer
    send(EventType.ANSWER_DELTA, {"text": "late-after-terminal"})
    assert sess.answer == before, "终态后 delta 必须被拒绝（状态机不破）"


def test_concurrent_lifecycle_multi_session_isolated():
    """意图: 多 session 并发互相隔离，各自状态独立、无串扰异常。"""
    eng = CardEngine(transport=FakeTransport())
    seq = 0
    seq_lock = threading.Lock()
    errors: list = []

    def worker(i: int) -> None:
        nonlocal seq
        try:
            for j in range(30):
                with seq_lock:
                    seq += 1
                    s = seq
                ev = _ev(
                    EventType.ANSWER_DELTA, s, f"conv-m{i}", f"chat-{i}", f"om_m{i}",
                    {"text": f"{i}-{j};"},
                )
                eng.handle_event(ev)
            with seq_lock:
                seq += 1
                s = seq
            eng.handle_event(_ev(EventType.MESSAGE_COMPLETED, s, f"conv-m{i}", f"chat-{i}", f"om_m{i}", {}))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(worker, range(6)))

    assert errors == [], f"多 session 并发异常: {errors}"
    assert len(eng.sessions) == 6
    for i in range(6):
        assert eng.sessions[f"conv-m{i}"].status == "completed"


# ---------------------------------------------------------------- b) 极端 unicode

_UNICODE_CHUNK = ("A\U0001F600\u200b\u200c\u200d\u2060\u202eRTL\u202c" * 600)[:8000]


def test_extreme_unicode_render_valid_json():
    """意图: emoji/零宽/RTL 文本累积 >100KB 后渲染，不崩溃且 JSON 合法。"""
    eng = CardEngine(transport=FakeTransport())
    conv, chat, msg = "conv-u1", "chat-1", "om_u1"
    total = 0
    seq = 0
    while total < 120_000:  # >100KB
        seq += 1
        total += len(_UNICODE_CHUNK)
        r = eng.handle_event(_ev(EventType.ANSWER_DELTA, seq, conv, chat, msg, {"text": _UNICODE_CHUNK}))
        assert r.applied is True, r.reason
    sess = eng.sessions[conv]
    assert len(sess.answer) == total, "零宽/emoji 应按 code point 计数，长度应精确"
    card = eng._render_safe(sess)
    dumped = json.dumps(card, ensure_ascii=False)
    assert json.loads(dumped) == card, "渲染产物必须可被 JSON 往返（无非法 JSON）"


def test_over_limit_text_clipped_with_mark():
    """意图: 累计超过 MAX_TEXT_CHARS 的文本按字符裁剪并带截断标记，不产生非法 JSON。"""
    eng = CardEngine(transport=FakeTransport())
    conv, chat, msg = "conv-u2", "chat-1", "om_u2"
    seq = 0
    remaining = MAX_TEXT_CHARS + 60_000
    while remaining > 0:
        seq += 1
        chunk = _UNICODE_CHUNK[: min(len(_UNICODE_CHUNK), remaining)]
        remaining -= len(chunk)
        eng.handle_event(_ev(EventType.ANSWER_DELTA, seq, conv, chat, msg, {"text": chunk}))
    sess = eng.sessions[conv]
    assert len(sess.answer) == MAX_TEXT_CHARS, f"应裁剪到上限，实际 {len(sess.answer)}"
    assert sess.answer.endswith(_TRUNC_MARK)
    card = eng._render_safe(sess)
    json.loads(json.dumps(card, ensure_ascii=False))


# ---------------------------------------------------------------- c) 超大工具状态

def test_oversized_tool_payload_clipped():
    """意图: 工具 detail/name 超限裁剪、条数超 MAX_TOOLS 忽略新工具，会话不炸。"""
    eng = CardEngine(transport=FakeTransport())
    conv, chat, msg = "conv-t1", "chat-1", "om_t1"
    eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1, conv, chat, msg, {"message_id": msg}))
    seq = 1
    for i in range(MAX_TOOLS + 5):
        seq += 1
        r = eng.handle_event(_ev(EventType.TOOL_UPDATED, seq, conv, chat, msg, {
            "id": f"tool-{i}",
            "name": "n" * 300,       # > TOOL_NAME_MAX
            "detail": "d" * 5000,    # > TOOL_DETAIL_MAX
            "status": "running",
        }))
        assert r.applied is True, r.reason
    sess = eng.sessions[conv]
    assert len(sess.tools) == MAX_TOOLS, f"工具条数应封顶 {MAX_TOOLS}"
    sample = next(iter(sess.tools.values()))
    assert len(sample.name) <= TOOL_NAME_MAX
    assert len(sample.detail) <= TOOL_DETAIL_MAX
    assert all(len(t.name) <= TOOL_NAME_MAX and len(t.detail) <= TOOL_DETAIL_MAX
               for t in sess.tools.values())


# ---------------------------------------------------------------- d) 配置边界

@pytest.fixture()
def clean_hfc_env(monkeypatch):
    """清除 HFC_* 环境变量，避免 env 覆盖段干扰 load_config 行为观测。"""
    for k in list(os.environ):
        if k.startswith("HFC_"):
            monkeypatch.delenv(k, raising=False)


def test_config_boundary_load_config(tmp_path, clean_hfc_env):
    """意图: 空 base_url / 无 scheme base_url / 畸形 limits 的 load_config 均不抛异常。"""
    p_empty = tmp_path / "empty-base.yaml"
    p_empty.write_text("transport: fake\nhttp:\n  base_url: \"\"\n", encoding="utf-8")
    cfg1 = load_config(p_empty)
    assert isinstance(cfg1, EngineConfig)

    p_noscheme = tmp_path / "noscheme.yaml"
    p_noscheme.write_text("http:\n  base_url: localhost:8080\n", encoding="utf-8")
    cfg2 = load_config(p_noscheme)
    assert isinstance(cfg2, EngineConfig)

    p_bad = tmp_path / "bad-limits.yaml"
    p_bad.write_text(
        "limits:\n  retention_seconds: not-a-number\n  history_limit: -5\n"
        "  zombie_grace_seconds: [1,2]\n"
        "card_limits:\n  max_elements: -1\n",
        encoding="utf-8",
    )
    cfg3 = load_config(p_bad)
    assert isinstance(cfg3, EngineConfig)
    # 修复行为（P3b）：非数字/list 类型非法 → 回退默认；数值负数同样非法 → 回退默认
    assert cfg3.limits.retention_seconds == 3600.0
    assert cfg3.limits.zombie_grace_seconds == 120.0
    assert cfg3.limits.history_limit == 50  # -5 → 回退默认
    assert cfg3.card_limits.max_elements == 200  # -1 → 回退默认

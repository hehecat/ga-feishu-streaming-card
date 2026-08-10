"""硬测例：多会话隔离 / CJK·emoji·超长文本 / 并发突发 / http 载荷形状。

全部不真发网络：FakeTransport 内存记录；HttpFeishuTransport 注入 MockTransport。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from ga_feishu_streaming_card.config import EngineConfig
from ga_feishu_streaming_card.engine import CardEngine
from ga_feishu_streaming_card.events import CardEvent, EventType
from ga_feishu_streaming_card.limits import enforce_card_limits
from ga_feishu_streaming_card.render import render_card
from ga_feishu_streaming_card.transport import FakeTransport, HttpFeishuTransport


def _ev(
    type_: EventType,
    seq: int,
    conversation_id: str,
    chat_id: str = "oc_1",
    data=None,
    message_id: str | None = None,
) -> CardEvent:
    return CardEvent(
        type=type_,
        sequence=seq,
        created_at=1000.0 + seq,
        conversation_id=conversation_id,
        chat_id=chat_id,
        message_id=message_id,
        data=data or {},
    )


def _engine() -> CardEngine:
    return CardEngine(cfg=EngineConfig(), transport=FakeTransport())


def _cards_for(eng: CardEngine, conversation_id: str) -> list[dict]:
    """该会话投递过的卡片（send 按会话创建顺序对应，update 按 message_id 归属）。"""
    order = list(eng.sessions)
    idx = order.index(conversation_id)
    mid = eng.sessions[conversation_id].message_id
    out: list[dict] = []
    seen_send = 0
    for c in eng.transport.calls:
        if c["op"] == "send_card":
            seen_send += 1
            if seen_send == idx + 1:
                out.append(c["card"])
        elif c["op"] == "update_card" and c.get("message_id") == mid:
            out.append(c["card"])
    return out


class TestSessionIsolation:
    """多 session 交错事件：卡片内容互不串扰、各自顺序保持。"""

    def test_interleaved_sessions_do_not_mix(self):
        eng = _engine()
        # 交错：A.start → B.start → A.delta → B.delta → A.delta → B.completed → A.completed
        evs = [
            _ev(EventType.MESSAGE_STARTED, 1, "convA"),
            _ev(EventType.MESSAGE_STARTED, 1, "convB"),
            _ev(EventType.THINKING_DELTA, 2, "convA", data={"text": "AAA 第一个"}),
            _ev(EventType.THINKING_DELTA, 2, "convB", data={"text": "BBB 第二个"}),
            _ev(EventType.ANSWER_DELTA, 3, "convA", data={"text": "AAA-最终"}),
            _ev(EventType.MESSAGE_COMPLETED, 3, "convB"),
            _ev(EventType.MESSAGE_COMPLETED, 4, "convA"),
        ]
        for e in evs:
            assert eng.handle_event(e).applied is True

        assert set(eng.sessions) == {"convA", "convB"}
        assert eng.sessions["convA"].status == "completed"
        assert eng.sessions["convB"].status == "completed"
        assert eng.sessions["convA"].last_sequence == 4
        assert eng.sessions["convB"].last_sequence == 3

        a_text = json.dumps(_cards_for(eng, "convA"), ensure_ascii=False)
        b_text = json.dumps(_cards_for(eng, "convB"), ensure_ascii=False)
        # A 的卡片含 A 文本、不含 B 文本
        assert "AAA 第一个" in a_text and "AAA-最终" in a_text
        assert "BBB 第二个" not in a_text
        # B 的卡片含 B 文本、不含 A 文本
        assert "BBB 第二个" in b_text
        assert "AAA 第一个" not in b_text and "AAA-最终" not in b_text

    def test_same_conversation_new_message_after_completed(self):
        """同一会话终态后新消息：状态固化、新 delta 不混入已完成卡片（防串扰）。"""
        eng = _engine()
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1, "c1"))
        eng.handle_event(_ev(EventType.ANSWER_DELTA, 2, "c1", data={"text": "第一轮答案"}))
        eng.handle_event(_ev(EventType.MESSAGE_COMPLETED, 3, "c1"))
        assert eng.sessions["c1"].status == "completed"
        # 终态后再来事件：状态保持固化，新文本不残留进已完成会话
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 4, "c1", message_id="m2"))
        r = eng.handle_event(_ev(EventType.ANSWER_DELTA, 5, "c1", data={"text": "第二轮答案"}))
        assert r.applied is True  # 事件不抛异常、正常记录
        s = eng.sessions["c1"]
        assert s.status == "completed"  # 终态固化
        assert "第二轮答案" not in s.answer  # 新 delta 被终态拒收，不混入旧内容
        cards = json.dumps(_cards_for(eng, "c1"), ensure_ascii=False)
        assert "第一轮答案" in cards
        assert "第二轮答案" not in cards  # 已投递卡片不被串扰


class TestCjkEmojiAndHugeText:
    """中文+emoji 渲染合法 JSON；超长文本优雅降级不抛异常。"""

    def test_cjk_emoji_renders_valid_json(self):
        eng = _engine()
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1, "cn"))
        eng.handle_event(_ev(EventType.THINKING_DELTA, 2, "cn",
                             data={"text": "思考中… 🤔 正在分析 🌍 数据"}))
        eng.handle_event(_ev(EventType.ANSWER_DELTA, 3, "cn",
                             data={"text": "你好 👋 世界！测试 🚀 emoji"}))
        eng.handle_event(_ev(EventType.MESSAGE_COMPLETED, 4, "cn"))
        for card in _cards_for(eng, "cn"):
            # 卡片必须可序列化为合法 JSON（UTF-8 编码后字节合法）
            blob = json.dumps(card, ensure_ascii=False).encode("utf-8")
            json.loads(blob.decode("utf-8"))
        last = _cards_for(eng, "cn")[-1]
        blob = json.dumps(last, ensure_ascii=False)
        assert "你好 👋 世界" in blob
        assert "🚀" in blob

    def test_huge_text_degrades_gracefully(self):
        eng = _engine()
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1, "huge"))
        huge = "很长很长的文本。" * 4000  # ~4.4 万字符，远超限额
        r = eng.handle_event(_ev(EventType.ANSWER_DELTA, 2, "huge", data={"text": huge}))
        assert r.applied is True  # 不抛异常、事件仍应用
        assert eng.sessions["huge"].status == "thinking"
        # 渲染降级后卡片仍合法且通过限额
        card = render_card(eng.sessions["huge"], eng.cfg.card_limits)
        enforce_card_limits(card)  # 不抛 CardLimitExceeded
        json.loads(json.dumps(card, ensure_ascii=False))

    def test_huge_failed_event_no_uncaught(self):
        eng = _engine()
        eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1, "hf"))
        eng.handle_event(_ev(EventType.ANSWER_DELTA, 2, "hf",
                             data={"text": "x" * 60000}))
        eng.handle_event(_ev(EventType.MESSAGE_FAILED, 3, "hf", data={"error": "e" * 60000}))
        card = render_card(eng.sessions["hf"], eng.cfg.card_limits)
        enforce_card_limits(card)
        assert eng.sessions["hf"].status == "failed"


class TestConcurrentBurst:
    """并发突发：8 线程 × 50 事件混合 session → 无异常、顺序保持、计数=总数。"""

    THREADS = 8
    EVENTS_PER_SESSION = 50

    def _run_session(self, eng: CardEngine, conv: str) -> tuple[str, int, bool]:
        applied = 0
        for seq in range(1, self.EVENTS_PER_SESSION + 1):
            if seq == 1:
                e = _ev(EventType.MESSAGE_STARTED, seq, conv)
            elif seq == self.EVENTS_PER_SESSION:
                e = _ev(EventType.MESSAGE_COMPLETED, seq, conv)
            else:
                e = _ev(EventType.ANSWER_DELTA, seq, conv,
                        data={"text": f"{conv}-chunk-{seq}"})
            if eng.handle_event(e).applied:
                applied += 1
        return conv, applied, True

    def test_8x50_mixed_sessions(self):
        eng = _engine()
        convs = [f"conv{i}" for i in range(self.THREADS)]
        with ThreadPoolExecutor(max_workers=self.THREADS) as pool:
            futures = [pool.submit(self._run_session, eng, c) for c in convs]
            results = [f.result(timeout=60) for f in futures]  # 无异常即通过

        assert all(ok for _, _, ok in results)
        assert sum(n for _, n, _ in results) == self.THREADS * self.EVENTS_PER_SESSION
        assert len(eng.sessions) == self.THREADS
        for c in convs:
            s = eng.sessions[c]
            assert s.status == "completed"
            assert s.last_sequence == self.EVENTS_PER_SESSION  # 每 session 顺序保持
        # 每个 session 至少 send 一次（卡片确实投递）
        sends = sum(1 for call in eng.transport.calls if call["op"] == "send_card")
        assert sends == self.THREADS

    def test_250_single_session_burst(self):
        """单会话 250 事件突发：全部应用、无异常、终态正确。"""
        eng = _engine()
        conv = "burst"
        applied = 0
        for seq in range(1, 251):
            if seq == 1:
                e = _ev(EventType.MESSAGE_STARTED, seq, conv)
            elif seq == 250:
                e = _ev(EventType.MESSAGE_COMPLETED, seq, conv)
            else:
                e = _ev(EventType.ANSWER_DELTA, seq, conv, data={"text": f"chunk-{seq}"})
            if eng.handle_event(e).applied:
                applied += 1
        assert applied == 250
        assert eng.sessions[conv].status == "completed"
        assert eng.sessions[conv].last_sequence == 250
        # 250 事件：1 send + 249 update（全部落到同一张卡）
        ops = [c["op"] for c in eng.transport.calls]
        assert ops.count("send_card") == 1
        assert ops.count("update_card") == 249

    def test_burst_same_conversation_serialized(self):
        """同会话连续快速事件：严格按 seq 应用，乱序被拒、计数准确。"""
        eng = _engine()
        conv = "serial"
        seqs = [1, 2, 3, 5, 4, 6, 7]  # 故意乱序：5 先于 4
        results = [eng.handle_event(
            _ev(EventType.ANSWER_DELTA if s > 1 else EventType.MESSAGE_STARTED, s, conv,
                data={"text": f"t{s}"})).applied for s in seqs]
        # 1,2,3,5 应用；4 乱序（<=5）拒；6,7 应用
        assert results == [True, True, True, True, False, True, True]
        assert eng.sessions[conv].last_sequence == 7
        assert eng.sessions[conv].answer == "t2t3t5t6t7"


class TestHttpPayloadShape:
    """http 传输预留：MockTransport 断言载荷形状（不真发网络）。"""

    def test_send_payload_shape(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/open-apis/im/v1/messages"
            assert "receive_id_type=chat_id" in str(request.url)
            body = json.loads(request.read().decode("utf-8"))
            seen["body"] = body
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_payload"}},
                                  request=request)

        t = HttpFeishuTransport(client=httpx.Client(transport=httpx.MockTransport(handler)))
        r = t.send_card("oc_payload", {"config": {"wide_screen_mode": True},
                                       "elements": [{"tag": "markdown", "content": "hi"}]})
        assert r.outcome == "delivered" and r.message_id == "om_payload"
        body = seen["body"]
        assert body["receive_id"] == "oc_payload"
        assert body["msg_type"] == "interactive"
        content = json.loads(body["content"])  # content 是合法 JSON 字符串
        assert isinstance(content, dict) and "elements" in content
        t.close()

    def test_update_payload_shape(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "PATCH"
            assert request.url.path == "/open-apis/im/v1/messages/om_update"
            seen["body"] = json.loads(request.read().decode("utf-8"))
            return httpx.Response(200, json={"code": 0}, request=request)

        t = HttpFeishuTransport(client=httpx.Client(transport=httpx.MockTransport(handler)))
        u = t.update_card("om_update", {"config": {"wide_screen_mode": True},
                                        "elements": [{"tag": "markdown", "content": "更新"}]})
        assert u.outcome == "updated"
        body = seen["body"]
        assert body["msg_type"] == "interactive"
        content = json.loads(body["content"])
        assert content["config"]["wide_screen_mode"] is True
        assert "更新" in json.dumps(content, ensure_ascii=False)
        t.close()

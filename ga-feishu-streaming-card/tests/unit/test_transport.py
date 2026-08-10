"""transport 模块测试（禁真实网络，全部走 MockTransport/Fake）。"""

from __future__ import annotations

import httpx
import pytest

from ga_feishu_streaming_card.transport import (
    CallableTransport,
    FakeTransport,
    HttpFeishuTransport,
    SendResult,
    UpdateResult,
)

CARD = {"config": {"wide_screen_mode": True}, "elements": [{"tag": "markdown", "content": "hi"}]}


class TestFakeTransport:
    def test_send_default_delivered_with_id(self):
        t = FakeTransport()
        r = t.send_card("oc_1", CARD)
        assert r.outcome == "delivered"
        assert r.ok is True
        assert r.message_id == "fake_msg_1"

    def test_calls_recorded(self):
        t = FakeTransport()
        t.send_card("oc_1", CARD)
        t.update_card("m_1", CARD)
        assert len(t.calls) == 2
        assert t.calls[0] == {"op": "send_card", "chat_id": "oc_1", "card": CARD}
        assert t.calls[1]["op"] == "update_card"
        assert t.calls[1]["message_id"] == "m_1"

    def test_inject_send_outcomes(self):
        t = FakeTransport(send_plan=["not_sent", "unknown", SendResult("delivered", "m_x")])
        r1 = t.send_card("oc_1", CARD)
        r2 = t.send_card("oc_1", CARD)
        r3 = t.send_card("oc_1", CARD)
        assert r1.outcome == "not_sent" and r1.ok is False
        assert r2.outcome == "unknown" and r2.message_id is None
        assert r3.outcome == "delivered" and r3.message_id == "m_x"
        # 计划用尽回退默认成功
        r4 = t.send_card("oc_1", CARD)
        assert r4.outcome == "delivered"

    def test_inject_exception_raises(self):
        t = FakeTransport(send_plan=[RuntimeError("boom")])
        with pytest.raises(RuntimeError):
            t.send_card("oc_1", CARD)

    def test_inject_update_outcomes(self):
        t = FakeTransport(update_plan=["not_found", "unknown", "updated"])
        assert t.update_card("m_1", CARD).outcome == "not_found"
        assert t.update_card("m_1", CARD).outcome == "unknown"
        u = t.update_card("m_1", CARD)
        assert u.outcome == "updated" and u.ok is True
        assert t.update_card("m_1", CARD).outcome == "updated"  # 默认回退

    def test_invalid_plan_value_raises(self):
        t = FakeTransport(send_plan=["bogus"])
        with pytest.raises(ValueError):
            t.send_card("oc_1", CARD)


class TestCallableTransport:
    def test_send_and_update_success(self):
        seen = []
        t = CallableTransport(
            lambda chat_id, card: seen.append(("send", chat_id, card)) or "om_real",
            lambda message_id, card: seen.append(("update", message_id, card)) or True,
        )
        assert t.send_card("oc_1", CARD) == SendResult("delivered", "om_real")
        assert t.update_card("om_real", CARD) == UpdateResult.updated()
        assert seen == [("send", "oc_1", CARD), ("update", "om_real", CARD)]

    def test_falsey_and_exception_fail_open(self):
        t = CallableTransport(lambda _chat, _card: None, lambda _mid, _card: False)
        assert t.send_card("oc_1", CARD).outcome == "unknown"
        assert t.update_card("om_1", CARD).outcome == "unknown"

        def boom(*_args):
            raise RuntimeError("boom")

        t = CallableTransport(boom, boom)
        assert t.send_card("oc_1", CARD).outcome == "unknown"
        assert t.update_card("om_1", CARD).outcome == "unknown"


class TestHttpFeishuTransport:
    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_send_ok_returns_message_id(self):
        def handler(request):
            assert request.url.path == "/open-apis/im/v1/messages"
            assert "receive_id_type=chat_id" in str(request.url)
            body = request.read()
            assert b'"msg_type":"interactive"' in body
            return httpx.Response(
                200,
                json={"code": 0, "data": {"message_id": "om_abc123"}},
                request=request,
            )

        t = HttpFeishuTransport(client=self._client(handler))
        r = t.send_card("oc_1", CARD)
        assert r.outcome == "delivered" and r.message_id == "om_abc123"

    def test_send_client_error_not_sent(self):
        def handler(request):
            return httpx.Response(400, json={"code": 99999}, request=request)

        t = HttpFeishuTransport(client=self._client(handler))
        r = t.send_card("oc_1", CARD)
        assert r.outcome == "not_sent" and r.ok is False

    def test_send_server_error_unknown(self):
        def handler(request):
            return httpx.Response(503, request=request)

        t = HttpFeishuTransport(client=self._client(handler))
        assert t.send_card("oc_1", CARD).outcome == "unknown"

    def test_send_network_error_unknown(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        t = HttpFeishuTransport(client=self._client(handler))
        assert t.send_card("oc_1", CARD).outcome == "unknown"

    def test_update_ok(self):
        def handler(request):
            assert request.method == "PATCH"
            assert request.url.path == "/open-apis/im/v1/messages/om_abc"
            return httpx.Response(200, json={"code": 0}, request=request)

        t = HttpFeishuTransport(client=self._client(handler))
        assert t.update_card("om_abc", CARD).outcome == "updated"

    def test_update_not_found(self):
        def handler(request):
            return httpx.Response(404, json={"code": 230002}, request=request)

        t = HttpFeishuTransport(client=self._client(handler))
        u = t.update_card("om_abc", CARD)
        assert u.outcome == "not_found" and u.ok is False

    def test_update_other_error_unknown(self):
        def handler(request):
            return httpx.Response(500, request=request)

        t = HttpFeishuTransport(client=self._client(handler))
        assert t.update_card("om_abc", CARD).outcome == "unknown"

    def test_base_url_and_timeout_config(self):
        t = HttpFeishuTransport(base_url="https://open.feishu.cn/", timeout_ms=1500)
        assert t.base_url == "https://open.feishu.cn"
        assert t.timeout_ms == 1500
        t.close()

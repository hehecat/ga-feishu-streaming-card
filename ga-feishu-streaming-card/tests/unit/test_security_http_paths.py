"""安全审查测试：HTTP 路径注入与伪协议。"""
import httpx
import pytest

from ga_feishu_streaming_card.config import DEFAULT_BASE_URL
from ga_feishu_streaming_card.transport import HttpFeishuTransport


class MockTransport(httpx.MockTransport):
    def __init__(self):
        self.requests = []
        super().__init__(self._handler)

    def _handler(self, request):
        self.requests.append(request)
        if "/messages/" in request.url.path and request.method == "PATCH":
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_mock"}})


@pytest.fixture
def mock_transport():
    return MockTransport()


def _transport(mock_transport):
    return HttpFeishuTransport(
        client=httpx.Client(transport=mock_transport), base_url="https://open.feishu.cn"
    )


def test_update_card_message_id_quoted(mock_transport):
    t = _transport(mock_transport)
    evil = "om_abc/../../etc/passwd?x=1"
    t.update_card(evil, {"config": {"wide_screen_mode": True}})
    req = mock_transport.requests[0]
    # url 原始串保留百分号编码（.path 是解码视图，不能用于断言）
    assert "/messages/om_abc%2F..%2F..%2Fetc%2Fpasswd%3Fx%3D1" in str(req.url)
    assert "/messages/om_abc/../../etc/passwd" not in str(req.url)


def test_send_card_chat_id_never_in_url(mock_transport):
    t = _transport(mock_transport)
    t.send_card("oc_evil/../x", {"msg_type": "interactive", "content": "{}"})
    req = mock_transport.requests[0]
    assert "oc_evil" not in str(req.url.path)  # chat_id 只在 JSON body
    body = req.read().decode("utf-8")
    assert "oc_evil/../x" in body


def test_base_url_file_scheme_falls_back(mock_transport):
    t = HttpFeishuTransport(
        client=httpx.Client(transport=mock_transport), base_url="file:///etc/passwd"
    )
    assert t.base_url == DEFAULT_BASE_URL


def test_base_url_javascript_scheme_falls_back(mock_transport):
    t = HttpFeishuTransport(
        client=httpx.Client(transport=mock_transport), base_url="javascript:alert(1)"
    )
    assert t.base_url == DEFAULT_BASE_URL


def test_update_card_non_str_message_id(mock_transport):
    t = _transport(mock_transport)
    t.update_card(12345, {"config": {"wide_screen_mode": True}})
    assert "/messages/12345" in str(mock_transport.requests[0].url.path)

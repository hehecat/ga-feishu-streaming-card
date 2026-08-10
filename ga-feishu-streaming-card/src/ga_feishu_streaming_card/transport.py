"""卡片投递传输层（独立实现）。

设计约定：
- CardTransport.send_card(chat_id, card) -> SendResult
- CardTransport.update_card(message_id, card) -> UpdateResult
- SendResult(outcome: 'delivered'|'not_sent'|'unknown', message_id: str|None)
- UpdateResult(ok: bool, outcome: 'updated'|'not_found'|'unknown')
- FakeTransport：内存记录所有调用，可注入失败/unknown 以测 fail-open。
- HttpFeishuTransport：httpx 实现（测试禁用真实网络，用 httpx.MockTransport）。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional
from urllib.parse import quote

import httpx

from .config import DEFAULT_BASE_URL, valid_base_url

SendOutcome = Literal["delivered", "not_sent", "unknown"]
UpdateOutcome = Literal["updated", "not_found", "unknown"]


@dataclass
class SendResult:
    """发送结果三态：delivered(有 message_id 或 None)、not_sent(明确未发)、unknown(结果不明)。"""

    outcome: SendOutcome
    message_id: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.outcome == "delivered"


@dataclass
class UpdateResult:
    """更新结果：ok + outcome 三态。"""

    ok: bool
    outcome: UpdateOutcome

    @classmethod
    def updated(cls) -> "UpdateResult":
        return cls(True, "updated")

    @classmethod
    def not_found(cls) -> "UpdateResult":
        return cls(False, "not_found")

    @classmethod
    def unknown(cls) -> "UpdateResult":
        return cls(False, "unknown")


class CardTransport(ABC):
    """传输抽象。实现必须自身消化异常并返回结果对象（fail-open 原则）。"""

    @abstractmethod
    def send_card(self, chat_id: str, card: Dict[str, Any]) -> SendResult:
        ...

    @abstractmethod
    def update_card(self, message_id: str, card: Dict[str, Any]) -> UpdateResult:
        ...

    def close(self) -> None:
        """释放底层资源（默认无操作）。"""
        return None


class FakeTransport(CardTransport):
    """内存传输：记录全部调用；支持注入计划（send_plan/update_plan）。

    注入项可为：
    - SendResult/UpdateResult 实例（原样返回）
    - 字符串快捷值：'delivered'|'not_sent'|'unknown' / 'updated'|'not_found'|'unknown'
    - Exception 实例（调用时抛出，模拟底层异常）
    计划用尽后回退到默认成功行为。
    """

    def __init__(
        self,
        send_plan: Optional[List[Any]] = None,
        update_plan: Optional[List[Any]] = None,
    ) -> None:
        self.send_plan: List[Any] = list(send_plan or [])
        self.update_plan: List[Any] = list(update_plan or [])
        self.calls: List[Dict[str, Any]] = []  # 记录所有调用：{op, chat_id|message_id, card}

    def _next(self, plan: List[Any], default: Any) -> Any:
        if plan:
            return plan.pop(0)
        return default

    def _coerce_send(self, item: Any) -> SendResult:
        if isinstance(item, SendResult):
            return item
        if isinstance(item, str):
            if item in ("delivered", "not_sent", "unknown"):
                return SendResult(item, f"fake_msg_{len(self.calls)}" if item == "delivered" else None)
            raise ValueError(f"invalid send outcome: {item!r}")
        return item  # 可能是 Exception，由调用方处理

    def _coerce_update(self, item: Any) -> UpdateResult:
        if isinstance(item, UpdateResult):
            return item
        if isinstance(item, str):
            if item in ("updated", "not_found", "unknown"):
                return UpdateResult(item == "updated", item)
            raise ValueError(f"invalid update outcome: {item!r}")
        return item

    def send_card(self, chat_id: str, card: Dict[str, Any]) -> SendResult:
        self.calls.append({"op": "send_card", "chat_id": chat_id, "card": card})
        item = self._coerce_send(self._next(self.send_plan, None))
        if item is None:
            n = sum(1 for c in self.calls if c["op"] == "send_card")
            return SendResult("delivered", f"fake_msg_{n}")
        if isinstance(item, Exception):
            raise item
        return item

    def update_card(self, message_id: str, card: Dict[str, Any]) -> UpdateResult:
        self.calls.append({"op": "update_card", "message_id": message_id, "card": card})
        item = self._coerce_update(self._next(self.update_plan, None))
        if item is None:
            return UpdateResult.updated()
        if isinstance(item, Exception):
            raise item
        return item


class CallableTransport(CardTransport):
    """适配宿主进程已有的飞书 SDK 调用，避免复制凭据或重复鉴权。"""

    def __init__(
        self,
        send_fn: Callable[[str, Dict[str, Any]], Optional[str]],
        update_fn: Callable[[str, Dict[str, Any]], bool],
    ) -> None:
        self._send_fn = send_fn
        self._update_fn = update_fn

    def send_card(self, chat_id: str, card: Dict[str, Any]) -> SendResult:
        try:
            message_id = self._send_fn(chat_id, card)
            if message_id:
                return SendResult("delivered", str(message_id))
            return SendResult("unknown")
        except Exception:
            return SendResult("unknown")

    def update_card(self, message_id: str, card: Dict[str, Any]) -> UpdateResult:
        try:
            return UpdateResult.updated() if self._update_fn(message_id, card) else UpdateResult.unknown()
        except Exception:
            return UpdateResult.unknown()


class HttpFeishuTransport(CardTransport):
    """飞书开放平台 HTTP 传输（httpx）。

    - app_id/app_secret 仅保存引用（字符串），本模块不读取、不参与请求签名；
      token 由外部（调用方/上层）注入 headers，保持"仅存引用不读取"约定。
    - 测试禁止真实网络：注入 httpx.Client(transport=httpx.MockTransport(...))。
    - 错误映射（fail-open）：
      send: 200→delivered；其余 4xx→not_sent；5xx/网络/超时→unknown。
      update: 200→updated；404→not_found；其余→unknown。
    """

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        base_url: str = "https://open.feishu.cn",
        timeout_ms: int = 800,
        app_id: str = "",
        app_secret: str = "",
    ) -> None:
        self._client = client
        self._owns_client = client is None
        # 防御：非法 scheme（file:// 等）/无 host → 回退默认（fail-open）
        self.base_url = base_url.rstrip("/") if valid_base_url(base_url) else DEFAULT_BASE_URL
        self.timeout_ms = timeout_ms
        self.app_id = app_id  # 仅存引用
        self.app_secret = app_secret  # 仅存引用

    def _client_impl(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_ms / 1000.0)
            self._owns_client = True
        return self._client

    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json"}

    def _card_content(self, card: Dict[str, Any]) -> str:
        return json.dumps(card, ensure_ascii=False)

    def send_card(self, chat_id: str, card: Dict[str, Any]) -> SendResult:
        client = self._client_impl()
        url = f"{self.base_url}/open-apis/im/v1/messages"
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": self._card_content(card),
        }
        try:
            resp = client.post(
                url,
                params={"receive_id_type": "chat_id"},
                json=payload,
                headers=self._headers(),
            )
        except httpx.HTTPStatusError as exc:
            # 客户端显式 raise_for_status 时的映射
            return self._map_send_status(exc.response.status_code)
        except (httpx.RequestError, httpx.TimeoutException):
            return SendResult("unknown")
        except Exception:
            return SendResult("unknown")

        return self._map_send_status(resp.status_code, body=resp.text)

    def _map_send_status(self, status: int, body: str = "") -> SendResult:
        if status == 200:
            msg_id: Optional[str] = None
            try:
                data = json.loads(body) if body else {}
                msg_id = ((data.get("data") or {}).get("message_id")) or None
            except Exception:
                msg_id = None
            return SendResult("delivered", msg_id)
        if 400 <= status < 500:
            return SendResult("not_sent")
        return SendResult("unknown")

    def update_card(self, message_id: str, card: Dict[str, Any]) -> UpdateResult:
        client = self._client_impl()
        # message_id 百分号编码（防路径注入/畸形 id 改 URL 结构）
        url = f"{self.base_url}/open-apis/im/v1/messages/{quote(str(message_id), safe='')}"
        payload = {
            "msg_type": "interactive",
            "content": self._card_content(card),
        }
        try:
            resp = client.patch(url, json=payload, headers=self._headers())
        except httpx.HTTPStatusError as exc:
            return self._map_update_status(exc.response.status_code)
        except (httpx.RequestError, httpx.TimeoutException):
            return UpdateResult.unknown()
        except Exception:
            return UpdateResult.unknown()
        return self._map_update_status(resp.status_code)

    def _map_update_status(self, status: int) -> UpdateResult:
        if status == 200:
            return UpdateResult.updated()
        if status == 404:
            return UpdateResult.not_found()
        return UpdateResult.unknown()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

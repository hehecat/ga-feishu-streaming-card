"""交互令牌与传输证明（独立实现）。

设计约定：OperationToken / transport proof 校验。
- OperationToken：交互操作的一次性令牌（token/chat_id/message_id/expires_at/scope）。
- verify_token：常数时间比较 + 过期校验。
- verify_transport_proof：对传输回执证明做常数时间校验；proof 缺失/不匹配 → False。
- transition_interaction_status：InteractionState(status∈pending/completed/failed)
  的流转辅助（纯函数，供 engine 更新会话交互态；终态不可逆）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Optional

INTERACTION_SCOPE = "interaction"
DEFAULT_TTL_SECONDS = 300


@dataclass
class OperationToken:
    """一次交互操作的令牌载体。expires_at 为 epoch 秒。"""

    token: str
    chat_id: str
    message_id: Optional[str] = None
    expires_at: float = 0.0
    scope: str = INTERACTION_SCOPE

    @property
    def expires_at_ts(self) -> float:
        """到期时间戳（兼容访问）。"""
        return self.expires_at

    def expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) > self.expires_at


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_token(
    chat_id: str,
    message_id: Optional[str] = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    secret: str = "",
    scope: str = INTERACTION_SCOPE,
) -> OperationToken:
    """签发操作令牌：token = 随机值（防猜测）；签名 = HMAC(chat_id|message_id|expires|scope)。

    secret 为空时签名仍生成（弱校验），保证接口在无密钥环境下可测。
    """
    expires_at = time.time() + ttl_seconds
    token = secrets.token_hex(16)
    payload = f"{chat_id}|{message_id or ''}|{expires_at:.3f}|{scope}"
    signature = _sign(payload, secret)
    return OperationToken(
        token=f"{token}.{signature}",
        chat_id=chat_id,
        message_id=message_id,
        expires_at=expires_at,
        scope=scope,
    )


def _split(token: str) -> tuple[str, str]:
    if "." in token:
        head, _, tail = token.partition(".")
        return head, tail
    return token, ""


def verify_token(op: OperationToken, expected_token: str, now: Optional[float] = None) -> bool:
    """校验令牌：常数时间比较 token 随机主体 + 过期检查。任一失败 → False。"""
    if op is None or not expected_token:
        return False
    if op.expired(now):
        return False
    head, _ = _split(op.token)
    pres_head, _ = _split(expected_token)
    return hmac.compare_digest(head, pres_head)


def issue_transport_proof(
    chat_id: str,
    message_id: Optional[str],
    op: str,
    secret: str = "",
) -> str:
    """签发传输证明：HMAC(chat_id|message_id|op|secret)，供 transport 层回执使用。"""
    payload = f"{chat_id}|{message_id or ''}|{op}"
    return _sign(payload, secret)


def verify_transport_proof(proof: Optional[str], expected: Optional[str]) -> bool:
    """校验传输证明：proof 或 expected 任一缺失 → False；常数时间比较。"""
    if not proof or not expected:
        return False
    return hmac.compare_digest(proof, expected)


def transition_interaction_status(status: str, outcome: str) -> str:
    """InteractionState 流转：pending + completed/failed → 终态；终态不可逆。"""
    if status not in ("pending", "completed", "failed"):
        return "failed"  # 未知初态视为失败（状态机防呆）
    if status != "pending":
        return status  # 终态不可逆
    if outcome in ("completed", "failed"):
        return outcome
    return status  # 未知 outcome 保持 pending

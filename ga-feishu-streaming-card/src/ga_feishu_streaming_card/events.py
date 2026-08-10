"""CardEvent 与事件解析（引擎核心，独立实现）。

接口约定：
- CardEvent 字段：schema_version="1", type, conversation_id, message_id, chat_id,
  thread_id, platform="feishu", sequence, created_at, turn_id, data
- parse_event(raw: dict) -> CardEvent：结构校验失败抛 ValueError。
  校验规则：type 必须合法；platform 必须 == "feishu"；sequence 必须为 int >= 0；
  REQUIRES_CHAT_ID 中的事件缺 chat_id 直接拒绝（会话级事件必须有投递对象）。
  EventResult 语义（applied/reason）由 engine 层处理，本层只验结构。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class EventType(str, Enum):
    """协议事件类型（10 种，字符串值即 wire 表示）。"""

    MESSAGE_STARTED = "message.started"
    THINKING_DELTA = "thinking.delta"
    TOOL_UPDATED = "tool.updated"
    ANSWER_DELTA = "answer.delta"
    MESSAGE_COMPLETED = "message.completed"
    MESSAGE_FAILED = "message.failed"
    SYSTEM_NOTICE = "system.notice"
    INTERACTION_REQUESTED = "interaction.requested"
    INTERACTION_COMPLETED = "interaction.completed"
    INTERACTION_FAILED = "interaction.failed"


#: 需要 chat_id 的会话级事件（system.notice 允许全局通知，不强制 chat_id）。
REQUIRES_CHAT_ID = frozenset(
    {
        EventType.MESSAGE_STARTED,
        EventType.THINKING_DELTA,
        EventType.TOOL_UPDATED,
        EventType.ANSWER_DELTA,
        EventType.MESSAGE_COMPLETED,
        EventType.MESSAGE_FAILED,
        EventType.INTERACTION_REQUESTED,
        EventType.INTERACTION_COMPLETED,
        EventType.INTERACTION_FAILED,
    }
)


@dataclass
class CardEvent:
    """协议事件载体。sequence 单调递增用于防乱序；data 携带事件负载。"""

    type: EventType
    sequence: int
    created_at: float
    data: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1"
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    platform: str = "feishu"
    turn_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 wire dict（用于测试与调试）。"""
        return {
            "schema_version": self.schema_version,
            "type": self.type.value,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "platform": self.platform,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "turn_id": self.turn_id,
            "data": dict(self.data),
        }


def parse_event(raw: Dict[str, Any]) -> CardEvent:
    """从 dict 解析 CardEvent；结构非法抛 ValueError（不吞异常）。"""
    if not isinstance(raw, dict):
        raise ValueError(f"event must be a dict, got {type(raw).__name__}")

    # type 合法性
    type_raw = raw.get("type")
    try:
        ev_type = EventType(type_raw)
    except (TypeError, ValueError):
        raise ValueError(f"invalid event type: {type_raw!r}") from None

    # platform 校验
    platform = raw.get("platform", "feishu")
    if platform != "feishu":
        raise ValueError(f"unsupported platform: {platform!r}")

    # sequence 校验
    seq = raw.get("sequence")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError(f"sequence must be a non-negative int, got {seq!r}")

    # 需要 chat_id 的事件缺 chat_id 拒绝
    chat_id = raw.get("chat_id")
    if ev_type in REQUIRES_CHAT_ID and (chat_id is None or chat_id == ""):
        raise ValueError(f"event {ev_type.value} requires chat_id")

    created_at = raw.get("created_at", 0.0)
    if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
        raise ValueError(f"created_at must be numeric, got {created_at!r}")

    data = raw.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"data must be a dict, got {type(data).__name__}")

    return CardEvent(
        type=ev_type,
        sequence=seq,
        created_at=float(created_at),
        data=data,
        schema_version=str(raw.get("schema_version", "1")),
        conversation_id=raw.get("conversation_id"),
        message_id=raw.get("message_id"),
        chat_id=chat_id,
        thread_id=raw.get("thread_id"),
        platform=platform,
        turn_id=raw.get("turn_id"),
    )

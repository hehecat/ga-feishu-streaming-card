"""ga-feishu-streaming-card：GA(GenericAgent) → 飞书流式卡片桥（引擎核心）。

公开 API 摘要（均可从包顶层导入；模块级路径保持可用，如
``ga_feishu_streaming_card.session.CardSession``）：

- 事件协议：``EventType`` / ``CardEvent`` / ``parse_event``
- 会话与生命周期：``CardSession`` / ``ToolState`` / ``InteractionState``
  / ``CleanupPolicy`` / ``cleanup_expired``
- 展示态与渲染：``DisplayStatus`` / ``resolve_display_status``
  / ``CardRenderResult`` / ``render_card`` / ``render_card_result`` / ``html_escape_card_text``
- 引擎、配置与传输：``CardEngine`` / ``EngineConfig`` / ``load_config``
  / ``FakeTransport`` / ``HttpFeishuTransport``
"""

from ga_feishu_streaming_card.config import EngineConfig, load_config
from ga_feishu_streaming_card.engine import CardEngine
from ga_feishu_streaming_card.events import CardEvent, EventType, parse_event
from ga_feishu_streaming_card.lifecycle import CleanupPolicy, cleanup_expired
from ga_feishu_streaming_card.render import (
    CardRenderResult,
    html_escape_card_text,
    render_card,
    render_card_result,
)
from ga_feishu_streaming_card.session import CardSession, InteractionState, ToolState
from ga_feishu_streaming_card.status import DisplayStatus, resolve_display_status
from ga_feishu_streaming_card.transport import CallableTransport, FakeTransport, HttpFeishuTransport

__version__ = "0.1.0"

__all__ = [
    "CardEvent",
    "CardEngine",
    "CardRenderResult",
    "CardSession",
    "CallableTransport",
    "CleanupPolicy",
    "DisplayStatus",
    "EngineConfig",
    "EventType",
    "FakeTransport",
    "HttpFeishuTransport",
    "InteractionState",
    "ToolState",
    "cleanup_expired",
    "html_escape_card_text",
    "load_config",
    "parse_event",
    "render_card",
    "render_card_result",
    "resolve_display_status",
]

"""T14-D 新增：包顶层 re-export 面（公开 API）与旧模块路径兼容性。"""

import ga_feishu_streaming_card as pkg


def test_top_level_reexports_core_objects():
    for name in (
        "CardSession",
        "CardRenderResult",
        "render_card",
        "render_card_result",
        "EventType",
        "CardEvent",
        "parse_event",
        "DisplayStatus",
        "resolve_display_status",
        "CleanupPolicy",
        "cleanup_expired",
        "CardEngine",
        "EngineConfig",
        "load_config",
        "FakeTransport",
        "HttpFeishuTransport",
    ):
        assert hasattr(pkg, name), f"顶层缺少 re-export: {name}"


def test_old_module_paths_still_importable():
    from ga_feishu_streaming_card.session import CardSession  # noqa: F401
    from ga_feishu_streaming_card.render import (  # noqa: F401
        CardRenderResult,
        render_card,
        render_card_result,
    )
    from ga_feishu_streaming_card.events import CardEvent, EventType, parse_event  # noqa: F401
    from ga_feishu_streaming_card.status import DisplayStatus, resolve_display_status  # noqa: F401
    from ga_feishu_streaming_card.lifecycle import CleanupPolicy, cleanup_expired  # noqa: F401
    from ga_feishu_streaming_card.engine import CardEngine  # noqa: F401
    from ga_feishu_streaming_card.config import EngineConfig, load_config  # noqa: F401
    from ga_feishu_streaming_card.transport import FakeTransport, HttpFeishuTransport  # noqa: F401


def test_all_matches_top_level_attributes():
    # __all__ 每个名字都真实存在（子模块绑定属性如 config/engine 属正常 import 语义，不强制入 __all__）
    for name in pkg.__all__:
        assert name in dir(pkg), f"__all__ 声明但顶层不可见: {name}"


def test_parse_event_roundtrip_via_top_level():
    ev = pkg.parse_event({
        "type": pkg.EventType.MESSAGE_STARTED.value,
        "conversation_id": "c1",
        "chat_id": "ch1",
        "sequence": 1,
        "created_at": 1.0,
        "data": {},
    })
    assert ev.type is pkg.EventType.MESSAGE_STARTED
    d = ev.to_dict()
    assert d["type"] == "message.started"
    assert pkg.parse_event(d).type is pkg.EventType.MESSAGE_STARTED

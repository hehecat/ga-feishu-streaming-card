"""安全审查测试：超大 payload 资源上限 + 渲染降级。"""
from ga_feishu_streaming_card.config import EngineConfig
from ga_feishu_streaming_card.delivery_policy import DeliveryPolicy
from ga_feishu_streaming_card.engine import CardEngine
from ga_feishu_streaming_card.events import CardEvent, EventType
from ga_feishu_streaming_card.session import CardSession, MAX_TEXT_CHARS, MAX_TOOLS
from ga_feishu_streaming_card.transport import FakeTransport


def _ev(type_, seq, data=None, **kw):
    return CardEvent(
        type=type_,
        sequence=seq,
        created_at=100.0 + seq,
        conversation_id="c1",
        chat_id="oc_1",
        data=data or {},
        **kw,
    )


def test_answer_text_capped():
    s = CardSession("c1", "oc_1")
    for i in range(3000):
        s.apply_event(_ev(EventType.ANSWER_DELTA, i + 1, data={"text": "x" * 200}))
    assert len(s.answer) <= MAX_TEXT_CHARS + 32
    assert s.answer.endswith("[truncated]")


def test_thinking_text_capped():
    s = CardSession("c1", "oc_1")
    for i in range(2000):
        s.apply_event(_ev(EventType.THINKING_DELTA, i + 1, data={"text": "y" * 300}))
    assert len(s.thinking) <= MAX_TEXT_CHARS + 32


def test_tool_count_capped():
    s = CardSession("c1", "oc_1")
    for i in range(500):
        s.apply_event(
            _ev(EventType.TOOL_UPDATED, i + 1, data={"id": f"tool_{i}", "status": "running"})
        )
    assert len(s.tools) <= MAX_TOOLS


def test_failed_reason_non_str():
    s = CardSession("c1", "oc_1")
    s.apply_event(_ev(EventType.MESSAGE_FAILED, 1, data={"reason": 123}))
    assert s.status == "failed"
    assert s.error_text == "123"


def test_delta_text_wrong_type_does_not_raise():
    s = CardSession("c1", "oc_1")
    s.apply_event(_ev(EventType.ANSWER_DELTA, 1, data={"text": ["list"]}))
    assert s.answer == "['list']"


def test_render_degrade_when_limits_broken():
    # 程序化构造坏 limits（字符串 max_elements）→ render 抛错 → 引擎降级卡仍投递
    from types import SimpleNamespace

    cfg = EngineConfig()
    cfg.card_limits = SimpleNamespace(max_elements="abc", max_tables="x", safe_card_json_bytes=1)
    t = FakeTransport()
    eng = CardEngine(cfg=cfg, transport=t)
    r = eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1))
    assert r.applied is True
    assert len(t.calls) == 1  # 发送未被渲染异常阻塞
    card = t.calls[0]["card"]
    # 降级卡只含固定错误码，不拼接原始异常（T7 脱敏）
    assert "render_error: code=RC01" in str(card)
    assert "渲染失败" in str(card)


def test_render_degrade_no_exception_leak(monkeypatch):
    # 渲染抛带敏感详情的异常 → 卡片只含固定错误码，异常文本/类型不进卡片
    import ga_feishu_streaming_card.engine as engine_mod

    def _boom(session, limits):
        raise RuntimeError("secret-internal-detail-xyz")

    monkeypatch.setattr(engine_mod, "render_card", _boom)
    t = FakeTransport()
    eng = CardEngine(transport=t)
    r = eng.handle_event(_ev(EventType.MESSAGE_STARTED, 1))
    assert r.applied is True
    card_str = str(t.calls[0]["card"])
    assert "render_error: code=RC01" in card_str
    assert "secret-internal-detail-xyz" not in card_str
    assert "RuntimeError" not in card_str


def test_engine_survives_bad_apply_data():
    t = FakeTransport()
    eng = CardEngine(transport=t)
    # text 为非 str 字典：apply 不炸，事件照常投递
    r = eng.handle_event(_ev(EventType.ANSWER_DELTA, 1, data={"text": {"a": 1}}))
    assert hasattr(r, "applied")  # 不抛异常即通过
    assert len(t.calls) == 1

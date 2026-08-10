"""T4 命令结果卡测试：映射表 / 安全渲染 / 超限与异常降级 / bridge fail-open。

覆盖（对应 T4 切片 1/2/5）：
- 命令 → 标题/模板映射全覆盖（含未知命令回退、/llm 有参/无参差异）；
- 渲染结构、HTML 转义（哨兵不泄露）、多块拆分；
- 超限降级（CardLimitExceeded）与异常降级（永不抛）；
- bridge.send_command_result_card：HFC-off（enabled=False）不发送、无配置
  fail-open、发送异常 fail-open（宿主回退文本）、成功路径参数透传。
"""
import json
from types import SimpleNamespace

import pytest

from ga_feishu_streaming_card import bridge, command
from ga_feishu_streaming_card.config import EngineConfig
from ga_feishu_streaming_card.transport import SendResult


class TestMapping:
    """T4: 命令 → 标题/模板映射全覆盖。"""

    def test_known_commands_full_mapping(self):
        # TITLE_BY_COMMAND / TEMPLATE_BY_COMMAND 全部键必须命中
        # （/llm 需带参才是「切换」语义）
        for name in command.TITLE_BY_COMMAND:
            kw = {"has_arg": True} if name == "/llm" else {}
            assert command.title_for(name, **kw) == command.TITLE_BY_COMMAND[name], name
        for name in command.TEMPLATE_BY_COMMAND:
            kw = {"has_arg": True} if name == "/llm" else {}
            assert command.template_for(name, **kw) == command.TEMPLATE_BY_COMMAND[name], name

    def test_unknown_command_falls_back_to_raw_and_green(self):
        assert command.title_for("/foo") == "/foo"
        assert command.template_for("/foo") == command.DEFAULT_TEMPLATE == "green"

    def test_empty_and_plain_text(self):
        assert command.title_for("") == "/"
        assert command.template_for("") == "green"
        assert command.title_for("随便一句话") == "随便一句话"

    def test_llm_without_arg_stays_default(self):
        # 无参列表态：不映射「模型已切换」
        assert command.title_for("/llm") == "/llm"
        assert command.template_for("/llm") == "green"

    def test_llm_with_arg_maps_to_switched(self):
        assert command.title_for("/llm 1", has_arg=True) == "模型已切换"
        assert command.template_for("/llm 1", has_arg=True) == "green"

    def test_multi_token_command_uses_first_token(self):
        assert command.title_for("/clear all please") == "上下文已清理"
        assert command.template_for("/undo now") == "orange"


class TestRender:
    """T4: 渲染结构与安全。"""

    def test_render_structure(self):
        card = command.render_command_result_card(
            "✅ 已切换到 [1] deepseek", command.title_for("/llm 1", has_arg=True), command.template_for("/llm 1", has_arg=True)
        )
        assert card["header"]["template"] == "green"
        assert card["header"]["title"]["content"] == "模型已切换"
        assert card["config"]["wide_screen_mode"] is True
        assert any(e.get("tag") == "markdown" for e in card["body"]["elements"])
        assert any("deepseek" in e.get("content", "") for e in card["body"]["elements"])

    def test_html_escaping_guard_rail(self):
        # 哨兵：HTML/脚本不得以原始形态出现在卡片 JSON 中
        sentinel = '<script>alert(1)</script><b>&</b>'
        card = command.render_command_result_card(sentinel, sentinel, "green")
        dumped = json.dumps(card, ensure_ascii=False)
        assert "<script>" not in dumped
        assert "<b>" not in dumped
        # 转义后内容仍可读
        text = json.dumps(card, ensure_ascii=False)
        assert "&lt;script&gt;" in text or "&amp;" in text

    def test_multiple_blocks_split(self):
        content = "\n\n".join(f"第{i}段" + "x" * 1200 for i in range(8))
        card = command.render_command_result_card(content, "测试", "green")
        md_elems = [e for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        assert len(md_elems) >= 2

    def test_over_limit_degrades_not_raises(self):
        # 28KB 限额：超大内容 → 固定安全卡（渲染失败），绝不抛异常
        big = "y" * 60000
        card = command.render_command_result_card(big, "超限", "green")
        assert card["header"]["title"]["content"] == "渲染失败"

    def test_limits_override_reduces_budget(self, monkeypatch):
        calls = {}
        orig = command.split_markdown_blocks

        def spy(text, **kw):
            calls.update(kw)
            return orig(text, **kw)

        monkeypatch.setattr(command, "split_markdown_blocks", spy)
        from ga_feishu_streaming_card.config import CardLimitsConfig

        command.render_command_result_card("hi", "t", "green", limits=CardLimitsConfig(max_elements=50, max_tables=3, safe_bytes=10000))
        assert calls["max_blocks"] == 44  # max_elements - 6（T11 命令按钮行预留）
        assert calls["max_bytes"] == 10000

    def test_unexpected_exception_degrades(self, monkeypatch):
        monkeypatch.setattr(command, "split_markdown_blocks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        card = command.render_command_result_card("x", "y", "green")
        assert card["header"]["title"]["content"] == "渲染失败"


def _stub_engine(send_impl):
    return SimpleNamespace(transport=SimpleNamespace(send_card=send_impl))


class TestBridgeSendCommandResultCard:
    """T4: bridge 直发 + fail-open（失败时宿主回退纯文本）。"""

    @pytest.fixture(autouse=True)
    def _clean_bridge(self):
        bridge.reset_for_test()
        yield
        bridge.reset_for_test()

    def test_off_mode_returns_false_without_sending(self):
        # HFC-off（enabled=False）：不发送、返回 False → GA 保持纯文本路径
        cfg = EngineConfig(enabled=False, transport="fake")
        assert bridge.send_command_result_card("oc_1", "/new", "hi", cfg=cfg) is False

    def test_no_cfg_and_no_bridge_returns_false(self):
        assert bridge.send_command_result_card("oc_1", "/new", "hi") is False

    def test_engine_present_but_transport_fails_returns_false(self):
        def boom(cid, card):
            raise RuntimeError("send failed")

        bridge._BRIDGE = SimpleNamespace(engine=_stub_engine(boom))
        assert bridge.send_command_result_card("oc_1", "/new", "hi") is False

    def test_happy_path_sends_mapped_card(self):
        seen = {}

        def record(cid, card):
            seen["chat_id"] = cid
            seen["card"] = card
            return SendResult("delivered")

        bridge._BRIDGE = SimpleNamespace(engine=_stub_engine(record))
        ok = bridge.send_command_result_card("oc_1", "/new", "🆕 已开启新对话", reply_to="ou_x")
        assert ok is True
        assert seen["chat_id"] == "oc_1"
        assert seen["card"]["header"]["title"]["content"] == "会话已重置"
        assert any("已开启新对话" in e.get("content", "") for e in seen["card"]["body"]["elements"])

    def test_unknown_command_content_reaches_card(self):
        seen = {}

        def record(cid, card):
            seen["card"] = card
            return SendResult("delivered")

        bridge._BRIDGE = SimpleNamespace(engine=_stub_engine(record))
        assert bridge.send_command_result_card("oc_2", "/weird", "未知命令") is True
        assert seen["card"]["header"]["title"]["content"] == "/weird"

    def test_llms_maps_to_model_list(self):
        # T11a：/llms 显式映射（P2-B 修复）
        assert command.title_for("/llms") == "模型列表"
        assert command.template_for("/llms") == "green"

    def test_command_card_has_button_row(self):
        # T27-G：命令结果卡尾部 2 行×2 列（两个顶层 column_set，每行两列各 1 个
        # button 块；2.0 schema 实测 2×2 横排，1.0 action 容器必竖排）
        card = command.render_command_result_card("ok", "/new")
        containers = [e for e in card["body"]["elements"] if e.get("tag") == "column_set"]
        assert len(containers) == 2
        assert all(len(c["columns"]) == 2 for c in containers)
        buttons = [b for c in containers for col in c["columns"] for b in col["elements"]]
        assert len(buttons) == 4
        # 行1: 新会话 / 切换模型；行2: 设置 / 状态
        assert [b["text"]["content"] for b in buttons] == [
            "新会话",
            "切换模型",
            "设置",
            "状态",
        ]
        assert [b["value"]["action"] for b in buttons] == [
            "/new",
            "/model",
            "/settings",
            "/status",
        ]
        assert all(b["value"]["hfc"] == 1 for b in buttons)
        # T27-E（#132 用户实测修正）：current_model 不再注入按钮文案（防长名换行竖排），
        # 固定短文案“切换模型”；当前模型名由 footer meta 承载
        card2 = command.render_command_result_card("ok", "/new", current_model="gpt-x")
        containers2 = [e for e in card2["body"]["elements"] if e.get("tag") == "column_set"]
        model_btn = [b for c in containers2 for col in c["columns"] for b in col["elements"]
                     if (b.get("value") or {}).get("action") == "/model"][0]
        assert model_btn["text"]["content"] == "切换模型"
        assert model_btn["value"]["action"] == "/model"


class TestT27SendCard:
    """T27：bridge.send_card 通用发卡（/settings 菜单卡等）fail-open 语义。"""

    @pytest.fixture(autouse=True)
    def _clean_bridge(self):
        bridge.reset_for_test()
        yield
        bridge.reset_for_test()

    def test_off_mode_returns_false_without_sending(self):
        cfg = EngineConfig(enabled=False, transport="fake")
        assert bridge.send_card("oc_1", {"config": {}}, cfg=cfg) is False

    def test_no_cfg_and_no_bridge_returns_false(self):
        bridge._BRIDGE = None
        assert bridge.send_card("oc_1", {"config": {}}) is False

    def test_happy_path_sends_raw_card_unchanged(self):
        seen = {}

        def record(cid, card):
            seen["chat_id"] = cid
            seen["card"] = card
            return SendResult("delivered")

        bridge._BRIDGE = SimpleNamespace(engine=_stub_engine(record))
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": "设置"}},
        }
        assert bridge.send_card("oc_9", card) is True
        assert seen["chat_id"] == "oc_9"
        # 原样透传：send_card 不解析/不重写卡片内容
        assert seen["card"] is card

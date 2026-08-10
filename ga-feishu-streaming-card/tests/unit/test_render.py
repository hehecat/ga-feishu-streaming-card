"""render 模块测试。"""

from __future__ import annotations

import json

import pytest

from ga_feishu_streaming_card.config import CardLimitsConfig
from ga_feishu_streaming_card.limits import card_json_bytes, count_elements, count_tables
from ga_feishu_streaming_card.render import (
    html_escape_card_text,
    render_card,
    render_plain_info_card,
    render_settings_menu_card,
)
from ga_feishu_streaming_card.session import (
    CardSession,
    InteractionState,
    TimelineItem,
    ToolState,
)


def _session(**kw) -> CardSession:
    base = dict(conversation_id="c1", chat_id="oc_1")
    base.update(kw)
    return CardSession(**base)


class TestHtmlEscape:
    def test_escapes_special_chars(self):
        assert html_escape_card_text("a & b <c> \"d\" 'e'") == (
            "a &amp; b &lt;c&gt; &quot;d&quot; &#39;e&#39;"
        )

    def test_amp_first_no_double_escape(self):
        assert html_escape_card_text("&amp;") == "&amp;amp;"
        assert html_escape_card_text("") == ""


class TestRenderCard:
    def test_base_structure(self):
        card = render_card(_session(answer="hello"))
        assert card["config"]["wide_screen_mode"] is True
        assert card["header"]["title"]["content"] == "生成中…"  # answer 派生 in_progress
        assert isinstance(card["body"]["elements"], list)
        assert len(card["body"]["elements"]) >= 1

    def test_status_header(self):
        s = _session(answer="hi")
        s.status = "completed"
        card = render_card(s)
        assert card["header"]["template"] == "green"
        assert card["header"]["title"]["content"] == "已完成"
        s2 = _session(thinking="t")
        card2 = render_card(s2)
        assert card2["header"]["title"]["content"] == "思考中…"
        s3 = _session(answer="x")
        s3.status = "failed"
        s3.error_text = "boom"
        card3 = render_card(s3)
        assert card3["header"]["template"] == "red"
        joined = json.dumps(card3, ensure_ascii=False)
        assert "boom" in joined  # 失败原因上卡

    def test_think_tags_stripped(self):
        s = _session(thinking="pre ```think\nsecret\n```\nvisible")
        card = render_card(s)
        joined = json.dumps(card, ensure_ascii=False)
        assert "secret" not in joined
        assert "visible" in joined
        assert "pre" in joined

    def test_tools_are_not_exposed(self):
        s = _session(thinking="working")
        s.tools["t1"] = ToolState(id="t1", name="browser", status="running", detail="open page")
        card = render_card(s)
        joined = json.dumps(card, ensure_ascii=False)
        assert "working" in joined
        assert "browser" not in joined and "open page" not in joined

    def test_timeline_precedes_final_answer_and_hides_tool_detail(self):
        s = _session(answer="这是最终回答。")
        s.timeline.extend([
            TimelineItem(kind="reasoning", title="思考 1", content="先查天气"),
            TimelineItem(kind="tool", title="web_scan", status="completed", tool_id="t1"),
        ])
        s.tools["t1"] = ToolState(
            id="t1", name="web_scan", status="completed", detail="SECRET_ARGS_AND_RESULT"
        )
        card = render_card(s)
        joined = json.dumps(card, ensure_ascii=False)
        assert card["body"]["elements"][0]["tag"] == "collapsible_panel"  # 思考为独立折叠面板
        assert "先查天气" in joined and "工具调用" in joined and "已完成" in joined
        assert "web_scan" not in joined  # 工具名不进入卡片（生产验收标准）
        assert any(
            el.get("tag") == "markdown" and "这是最终回答。" in el.get("content", "")
            for el in card["body"]["elements"]
        )
        assert "SECRET_ARGS_AND_RESULT" not in joined
        assert "<summary>" not in joined
        assert "<font" not in joined  # 卡片 markdown 不得含 HTML 标签（飞书不解析）

    def test_pending_interaction_hint(self):
        s = _session(thinking="ask")
        s.interactions["i1"] = InteractionState(id="i1", status="pending")
        joined = json.dumps(render_card(s), ensure_ascii=False)
        assert "等待用户操作" in joined

    def test_huge_text_degraded_under_limit(self):
        s = _session(answer="x" * 200_000)
        card = render_card(s, CardLimitsConfig(safe_bytes=28000))
        assert card_json_bytes(card) <= 28000
        assert count_elements(card) <= 200
        assert count_tables(card) <= 5

    def test_notice_shown(self):
        s = _session(thinking="t")
        s.notices.append("系统通知")
        joined = json.dumps(render_card(s), ensure_ascii=False)
        assert "系统通知" in joined

    def test_html_in_text_escaped(self):
        s = _session(answer="<script>alert(1)</script>")
        joined = json.dumps(render_card(s), ensure_ascii=False)
        assert "<script>" not in joined
        assert "&lt;script&gt;" in joined


# ---------------------------------------------------------------- T5: header subtitle / footer meta


class TestT5Subtitle:
    def _active(self, **kw) -> CardSession:
        s = _session(thinking="t", answer="a")
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_running_with_action_word(self):
        s = self._active(runtime_header_text="正在搜索")
        card = render_card(s)
        assert card["header"]["subtitle"]["content"] == "正在搜索"
        # T10：生产实测 header.subtitle 仅接受 tag=lark_md
        assert card["header"]["subtitle"]["tag"] == "lark_md"

    def test_thinking_stage_fallback(self):
        # 纯思考（无正文）→ THINKING 态 → 阶段文案
        card = render_card(_session(thinking="t"))
        assert card["header"]["subtitle"]["content"] == "正在思考"

    def test_in_progress_stage_fallback(self):
        from ga_feishu_streaming_card.events import EventType, parse_event

        s = _session(answer="a")
        s.apply_event(parse_event({"type": "message.started", "sequence": 1,
                                   "chat_id": "oc_1", "data": {}}))
        card = render_card(s)
        assert card["header"]["subtitle"]["content"] == "正在生成"

    def test_terminal_no_subtitle(self):
        s = _session(answer="done")
        s.status = "completed"
        card = render_card(s)
        assert "subtitle" not in card["header"]

    def test_failed_no_subtitle(self):
        s = _session(answer="")
        s.status = "failed"
        card = render_card(s)
        assert "subtitle" not in card["header"]


class TestT5Footer:
    def test_footer_parts(self):
        import time

        s = _session(answer="a")
        s.created_at = time.time() - 90
        s.updated_at = s.created_at + 90
        s.model = "gpt-4o"
        s.tokens = {"input_tokens": 1200, "output_tokens": 3400}
        s.context = {"used_tokens": 12000, "max_tokens": 32000}
        card = render_card(s)
        footers = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        assert footers and footers[-1] == "⏱ 1m30s · gpt-4o · ↑1.2k/↓3.4k tokens · ctx 12k/32k 38%"

    def test_footer_omits_missing(self):
        import time

        s = _session(answer="a")
        s.created_at = time.time() - 5
        s.updated_at = s.created_at + 5
        card = render_card(s)
        footers = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        assert footers and footers[-1] == "⏱ 5s"

    def test_footer_no_tokens_no_model(self):
        import time

        s = _session(answer="a")
        s.created_at = time.time() - 75
        s.updated_at = s.created_at + 75
        s.model = "m"
        card = render_card(s)
        footers = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        assert footers and footers[-1] == "⏱ 1m15s · m"

    def test_footer_context_only_used(self):
        # max_tokens 缺失（如 usage 无上下文规格）→ 省略百分比，仍显示用量
        import time

        s = _session(answer="a")
        s.created_at = time.time() - 10
        s.updated_at = s.created_at + 10
        s.context = {"used_tokens": 1500}
        card = render_card(s)
        footers = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        assert footers and footers[-1] == "⏱ 10s · ctx 1.5k"

    def test_footer_no_context_omitted(self):
        import time

        s = _session(answer="a")
        s.created_at = time.time() - 10
        s.updated_at = s.created_at + 10
        card = render_card(s)
        footers = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        assert footers and footers[-1] == "⏱ 10s"
        assert "ctx" not in footers[-1]

    def test_abbr_count(self):
        from ga_feishu_streaming_card.render import _abbr_count

        assert _abbr_count(999) == "999"
        assert _abbr_count(1000) == "1k"
        assert _abbr_count(12345) == "12.3k"
        assert _abbr_count(1200000) == "1.2m"
        assert _abbr_count(2000000) == "2m"
        assert _abbr_count(-5) == "0"
        assert _abbr_count("x") == "0"


# ---------------------------------------------------------------- T11: 按钮行 / footer 步数


class TestT11Buttons:
    """T11a：命令按钮行 + pending 交互按钮行 + footer 工具步数。"""

    def test_command_buttons_present_with_protocol_value(self):
        card = render_card(_session(answer="a"))
        # T27-G：命令按钮 = 两个顶层 column_set（每行两列，每列 1 个 button 块；2.0 实测 2×2 横排）
        containers = [e for e in card["body"]["elements"] if e.get("tag") == "column_set"]
        assert containers, "命令按钮 column_set 必须存在"
        buttons = [b for cs in containers for col in cs["columns"] for b in col["elements"]]
        assert [b["value"]["action"] for b in buttons] == [
            "/new", "/model", "/settings", "/status",
        ]
        assert all(b["value"]["hfc"] == 1 for b in buttons)
        assert all(b["text"]["tag"] == "plain_text" for b in buttons)

    def test_pending_interaction_renders_button_row(self):
        s = _session(thinking="ask")
        s.interactions["i1"] = InteractionState(id="i1", status="pending")
        s.interactions["i2"] = InteractionState(id="i2", status="pending")
        card = render_card(s)
        joined = json.dumps(card, ensure_ascii=False)
        assert "等待用户操作" in joined
        sets = [e for e in card["body"]["elements"] if e.get("tag") == "column_set"]
        # 交互按钮行与命令按钮同层 column_set；按 value.action 区分（命令白名单 /new /model /settings /status）
        def _first_btn(cs):
            return cs["columns"][0]["elements"][0]
        inter = [e for e in sets
                 if (_first_btn(e).get("value") or {}).get("action") not in
                 {"/new", "/model", "/settings", "/status"}]
        assert {b["value"]["action"] for cs in inter for col in cs["columns"] for b in col["elements"]} == {"i1", "i2"}

    def test_completed_interaction_no_button_row(self):
        s = _session(thinking="ask")
        s.interactions["i1"] = InteractionState(id="i1", status="completed")
        card = render_card(s)
        joined = json.dumps(card, ensure_ascii=False)
        assert "等待用户操作" not in joined
        sets = [e for e in card["body"]["elements"] if e.get("tag") == "column_set"]
        # 交互 completed 不渲染交互按钮；顶层 column_set 只剩命令按钮（T27-G 两个）
        def _first_btn(cs):
            return cs["columns"][0]["elements"][0]
        inter = [e for e in sets
                 if (_first_btn(e).get("value") or {}).get("action") not in
                 {"/new", "/model", "/settings", "/status"}]
        assert inter == []
        containers = sets
        buttons = [b for cs in containers for col in cs["columns"] for b in col["elements"]]
        assert [b["value"]["action"] for b in buttons] == [
            "/new", "/model", "/settings", "/status",
        ]

    def test_footer_tool_steps(self):
        s = _session(answer="a")
        s.timeline.append(TimelineItem(kind="tool", title="工具", status="completed"))
        s.timeline.append(TimelineItem(kind="tool", title="工具", status="completed"))
        s.timeline.append(TimelineItem(kind="message", title="消息"))
        card = render_card(s)
        assert "工具调用 2 次" in json.dumps(card, ensure_ascii=False)

    def test_footer_no_tools_no_steps_part(self):
        card = render_card(_session(answer="a"))
        assert "工具调用" not in json.dumps(card, ensure_ascii=False)


class TestT27Cards:
    """T27：设置二级菜单卡 + 信息文本卡（本地构造，无命令执行面）。"""

    def test_settings_menu_card_structure(self):
        card = render_settings_menu_card()
        assert card["header"]["template"] == "blue"
        assert card["header"]["title"]["content"] == "设置"
        sets = [e for e in card["body"]["elements"] if e.get("tag") == "column_set"]
        # T27-G7: 3 按钮一行太挤 → 每按钮一个 column_set 独占整行（竖排一列）
        assert len(sets) == 3
        buttons = [b for s in sets for col in s["columns"] for b in col["elements"]]
        assert [b["value"]["action"] for b in buttons] == [
            "/goal_hive",
            "/help",
            "/about",
        ]
        assert all(b["value"]["hfc"] == 1 for b in buttons)
        assert all(b["text"]["tag"] == "plain_text" for b in buttons)

    def test_plain_info_card_structure(self):
        card = render_plain_info_card("关于", "这是说明文本 <b>&</b>")
        assert card["header"]["template"] == "blue"
        assert card["header"]["title"]["content"] == "关于"
        joined = json.dumps(card, ensure_ascii=False)
        assert "这是说明文本" in joined
        # 信息卡无命令按钮行（避免无意义回环），正文不落任何工具/命令信息
        assert all(e.get("tag") != "column_set" for e in card["body"]["elements"])

"""status.py 单元测试：展示态派生。"""

import pytest

from ga_feishu_streaming_card.session import CardSession, ToolState
from ga_feishu_streaming_card.status import DisplayStatus, resolve_display_status


def _session(**over):
    kw = {"conversation_id": "c-1", "chat_id": "oc_1"}
    kw.update(over)
    return CardSession(**kw)


class TestDisplayStatus:
    def test_thinking_without_tools(self):
        assert resolve_display_status(_session()) is DisplayStatus.THINKING

    def test_completed_maps_directly(self):
        s = _session(status="completed")
        assert resolve_display_status(s) is DisplayStatus.COMPLETED

    def test_failed_maps_directly(self):
        s = _session(status="failed")
        assert resolve_display_status(s) is DisplayStatus.FAILED

    def test_running_when_tool_active(self):
        s = _session()
        s.tools["t1"] = ToolState(id="t1", status="running")
        assert resolve_display_status(s) is DisplayStatus.RUNNING

    def test_running_when_tool_pending(self):
        s = _session()
        s.tools["t1"] = ToolState(id="t1", status="pending")
        assert resolve_display_status(s) is DisplayStatus.RUNNING

    def test_thinking_when_all_tools_terminal(self):
        """工具全部终态不代表等待用户：agent 会继续思考/产出，展示思考中。"""
        s = _session()
        s.tools["t1"] = ToolState(id="t1", status="completed")
        assert resolve_display_status(s) is DisplayStatus.THINKING

    def test_thinking_with_mixed_terminal_tools(self):
        s = _session()
        s.tools["t1"] = ToolState(id="t1", status="completed")
        s.tools["t2"] = ToolState(id="t2", status="failed")
        assert resolve_display_status(s) is DisplayStatus.THINKING

    def test_status_values_are_strings(self):
        assert DisplayStatus.THINKING.value == "thinking"
        assert DisplayStatus.RUNNING.value == "running"
        assert DisplayStatus.WAITING.value == "waiting"
        assert DisplayStatus.COMPLETED.value == "completed"
        assert DisplayStatus.FAILED.value == "failed"

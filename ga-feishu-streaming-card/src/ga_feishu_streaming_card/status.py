"""展示态派生（引擎核心，独立实现）。

关键语义：session.status 仅取 thinking/completed/failed 三值；
waiting/running 等均为**展示态**，由本模块从会话状态派生，供 render 层使用。
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import CardSession


class DisplayStatus(str, Enum):
    """卡片展示态（可投递给飞书 UI 的状态集合）。"""

    THINKING = "thinking"  # 纯思考中（无工具在跑）
    IN_PROGRESS = "in_progress"  # 已产出答案增量（正在生成回复）
    RUNNING = "running"  # 有进行中的工具
    WAITING = "waiting"  # 工具均已终态、会话未完成（等待下一步/用户交互）
    COMPLETED = "completed"  # 会话正常完成
    FAILED = "failed"  # 会话失败


def resolve_display_status(session: "CardSession") -> DisplayStatus:
    """从会话派生展示态。

    规则（自洽且可测）：
    0. 事件显式携带 display_status（合法值）时，显式值优先（src=explicit）；
    1. completed/failed 直接映射；
    2. thinking + answer 已有内容 -> in_progress（answer.delta 后不再停留 thinking）；
    3. thinking + 存在 running/pending 工具 -> running；
    4. thinking + 存在工具且全部终态（等待继续） -> waiting；
    5. thinking + 无任何工具/内容 -> thinking。
    """
    explicit = getattr(session, "display_status", "") or ""
    if explicit in DisplayStatus._value2member_map_:
        return DisplayStatus(explicit)
    if session.status == "completed":
        return DisplayStatus.COMPLETED
    if session.status == "failed":
        return DisplayStatus.FAILED
    # session.status == "thinking"
    if session.answer:
        return DisplayStatus.IN_PROGRESS
    has_active = False
    has_tool = False
    for tool in session.tools.values():
        has_tool = True
        if tool.status in ("running", "pending"):
            has_active = True
    if has_active:
        return DisplayStatus.RUNNING
    # 工具均已终态：不派发 waiting——agent 终态后必然继续思考/产出答案，
    # “等待用户输入”无法从会话状态可靠判定（上游也不显式发送 waiting），
    # 展示“思考中…”最贴近真实。
    return DisplayStatus.THINKING

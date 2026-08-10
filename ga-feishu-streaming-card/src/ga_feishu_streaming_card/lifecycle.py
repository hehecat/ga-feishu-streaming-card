"""会话生命周期清理（引擎核心，独立实现）。

设计约定：CleanupPolicy(retention_seconds=3600, zombie_grace_seconds=120,
history_limit=50) + cleanup_expired(engine)。

规则（自洽且可测）：
- completed/failed 会话：age(created_at) > retention_seconds -> 回收；
- thinking 会话（zombie）：最后活动（updated_at）距今 > retention_seconds +
  zombie_grace_seconds -> 回收（活动会话因持续 touch 而豁免）；
- history_limit：清理后仍超出时，回收顺序为 终态 > 久未活动的 thinking >
  最近活动的 thinking（活动会话豁免），同优先级按 created_at 旧到新，
  直至不超限或无可回收对象。返回被回收的会话键列表（按删除顺序）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class CleanupPolicy:
    retention_seconds: float = 3600
    zombie_grace_seconds: float = 120
    history_limit: int = 50


def _session_age(session: Any, now: float) -> float:
    return now - getattr(session, "created_at", now)


def _session_idle(session: Any, now: float) -> float:
    """最后活动距今（updated_at 优先，回退 created_at）。"""
    last_active = getattr(session, "updated_at", None)
    if last_active is None:
        last_active = getattr(session, "created_at", now)
    return now - last_active


def _is_active(session: Any, now: float, grace: float) -> bool:
    """thinking 会话在宽限期内有活动（updated_at 新）→ 视为仍在用。"""
    return session.status == "thinking" and _session_idle(session, now) <= grace


def cleanup_expired(
    engine: Any,
    policy: Optional[CleanupPolicy] = None,
    now: Optional[float] = None,
) -> List[str]:
    """清理过期会话；engine 需暴露 .sessions（dict[str, CardSession]）与
    .remove_session(key)（或支持 del engine.sessions[key]）。返回回收键列表。

    豁免规则（活动会话保护）：
    - 终态（completed/failed）按 created_at 年龄 > retention_seconds 回收；
    - thinking 会话仅当 最后活动(updated_at)距今 > retention+zombie_grace
      才回收（长运行但持续 touch 的会话不会被误杀）；
    - history_limit 收缩：终态优先，其次久未活动的 thinking，最近活动的
      thinking 最后才考虑（全部活动时不再强制收缩）。
    """
    if policy is None:
        policy = CleanupPolicy()
    now = now if now is not None else time.time()
    sessions = engine.sessions

    removed: List[str] = []
    zombie_limit = policy.retention_seconds + policy.zombie_grace_seconds
    for key, session in list(sessions.items()):
        if session.status in ("completed", "failed") and _session_age(session, now) > policy.retention_seconds:
            removed.append(key)
        elif session.status == "thinking" and _session_idle(session, now) > zombie_limit:
            removed.append(key)
    for key in removed:
        _remove(engine, key)

    # history_limit 收缩（保护活动会话：活动 thinking 最后才回收）
    if policy.history_limit > 0 and len(sessions) > policy.history_limit:
        # 排序 key：终态优先回收；同优先级内 idle 大（久未活动）先回收；
        # 活动会话（宽限期内有活动）排最后，全部活动时收缩跳过。
        def _key(key: str) -> tuple:
            s = sessions[key]
            terminal = 0 if s.status in ("completed", "failed") else 1
            active = 1 if _is_active(s, now, policy.zombie_grace_seconds) else 0
            return (terminal, active, -_session_idle(s, now), getattr(s, "created_at", 0.0), key)

        over = len(sessions) - policy.history_limit
        # 仅回收可回收对象（活动 thinking 排最后；若前 over 个全部是活动会话则跳过）
        candidates = sorted(sessions, key=_key)
        for key in candidates[:over]:
            if key not in sessions:
                continue
            s = sessions[key]
            if _is_active(s, now, policy.zombie_grace_seconds):
                continue  # 活动会话豁免
            removed.append(key)
            _remove(engine, key)
    return removed


def _remove(engine: Any, key: str) -> None:
    if hasattr(engine, "remove_session"):
        engine.remove_session(key)
    else:
        del engine.sessions[key]

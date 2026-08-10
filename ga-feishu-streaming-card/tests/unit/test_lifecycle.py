"""lifecycle.py 单元测试：过期回收与 history_limit 收缩。"""

import pytest

from ga_feishu_streaming_card.lifecycle import CleanupPolicy, cleanup_expired
from ga_feishu_streaming_card.session import CardSession


class FakeEngine:
    def __init__(self, sessions):
        self.sessions = sessions
        self.removed = []

    def remove_session(self, key):
        self.removed.append(key)
        del self.sessions[key]


def _mk(conversation_id, status, created_at, updated_at=None):
    s = CardSession(conversation_id=conversation_id, chat_id="oc_1")
    s.status = status
    s.created_at = created_at
    s.updated_at = created_at if updated_at is None else updated_at
    return s


class TestExpiry:
    def test_completed_expired_removed(self):
        eng = FakeEngine({"a": _mk("a", "completed", 100.0)})
        r = cleanup_expired(eng, CleanupPolicy(retention_seconds=60), now=200.0)
        assert r == ["a"]
        assert "a" not in eng.sessions

    def test_failed_expired_removed(self):
        eng = FakeEngine({"a": _mk("a", "failed", 100.0)})
        r = cleanup_expired(eng, CleanupPolicy(retention_seconds=60), now=200.0)
        assert r == ["a"]

    def test_terminal_not_expired_kept(self):
        eng = FakeEngine({"a": _mk("a", "completed", 150.0)})
        r = cleanup_expired(eng, CleanupPolicy(retention_seconds=60), now=200.0)
        assert r == []
        assert "a" in eng.sessions

    def test_thinking_zombie_removed(self):
        eng = FakeEngine({"a": _mk("a", "thinking", 0.0)})
        policy = CleanupPolicy(retention_seconds=60, zombie_grace_seconds=120)
        r = cleanup_expired(eng, policy, now=200.0)
        assert r == ["a"]

    def test_thinking_young_kept(self):
        eng = FakeEngine({"a": _mk("a", "thinking", 100.0)})
        policy = CleanupPolicy(retention_seconds=60, zombie_grace_seconds=120)
        r = cleanup_expired(eng, policy, now=200.0)
        assert r == []
        assert "a" in eng.sessions

    def test_thinking_within_grace_kept(self):
        # age=170 > retention(60) 但 < retention+grace(180)
        eng = FakeEngine({"a": _mk("a", "thinking", 30.0)})
        policy = CleanupPolicy(retention_seconds=60, zombie_grace_seconds=120)
        r = cleanup_expired(eng, policy, now=200.0)
        assert r == []

    def test_thinking_past_grace_removed(self):
        eng = FakeEngine({"a": _mk("a", "thinking", 10.0)})
        policy = CleanupPolicy(retention_seconds=60, zombie_grace_seconds=120)
        r = cleanup_expired(eng, policy, now=200.0)
        assert r == ["a"]

    def test_mixed_expiry(self):
        eng = FakeEngine({
            "old_done": _mk("old_done", "completed", 0.0),
            "fresh": _mk("fresh", "completed", 190.0),
            "zombie": _mk("zombie", "thinking", 0.0),
        })
        policy = CleanupPolicy(retention_seconds=60, zombie_grace_seconds=120)
        r = cleanup_expired(eng, policy, now=200.0)
        assert sorted(r) == ["old_done", "zombie"]
        assert set(eng.sessions) == {"fresh"}


class TestHistoryLimit:
    def test_shrink_keeps_latest(self):
        eng = FakeEngine({f"k{i}": _mk(f"k{i}", "completed", float(i)) for i in range(5)})
        r = cleanup_expired(eng, CleanupPolicy(history_limit=2), now=1000.0)
        assert set(eng.sessions) == {"k3", "k4"}

    def test_shrink_prefers_terminal(self):
        eng = FakeEngine({
            "t1": _mk("t1", "thinking", 100.0),
            "d1": _mk("d1", "completed", 101.0),
        })
        r = cleanup_expired(eng, CleanupPolicy(history_limit=1), now=1000.0)
        assert "d1" in r  # 终态优先回收
        assert set(eng.sessions) == {"t1"}

    def test_within_limit_untouched(self):
        eng = FakeEngine({"a": _mk("a", "completed", 0.0)})
        r = cleanup_expired(eng, CleanupPolicy(history_limit=5), now=1000.0)
        assert r == []
        assert "a" in eng.sessions

    def test_default_policy_uses_now(self):
        eng = FakeEngine({"a": _mk("a", "completed", 0.0)})
        r = cleanup_expired(eng)  # retention 3600 默认，created=0 → 已过期
        assert r == ["a"]


class TestActiveSessionExemption:
    """核心修复：zombie 判定基于 updated_at（最后活动），活动长会话不得被误杀。"""

    def test_long_running_active_thinking_kept(self):
        # 创建于 10000s 前，但 5s 前仍在活动（updated_at 新）→ 必须豁免
        eng = FakeEngine({"a": _mk("a", "thinking", 0.0, updated_at=195.0)})
        policy = CleanupPolicy(retention_seconds=60, zombie_grace_seconds=120)
        r = cleanup_expired(eng, policy, now=200.0)
        assert r == []
        assert "a" in eng.sessions

    def test_idle_thinking_removed_despite_young_created(self):
        # 创建不久但最后活动在很久前（zombie）→ 回收
        eng = FakeEngine({"a": _mk("a", "thinking", 100.0, updated_at=0.0)})
        policy = CleanupPolicy(retention_seconds=60, zombie_grace_seconds=120)
        r = cleanup_expired(eng, policy, now=200.0)
        assert r == ["a"]

    def test_active_thinking_exempt_from_history_shrink(self):
        # 超限但非终态会话均在宽限期内活动 → 收缩跳过，活动会话存活
        eng = FakeEngine({
            "old_done": _mk("old_done", "completed", 0.0, updated_at=0.0),
            "active_a": _mk("active_a", "thinking", 0.0, updated_at=199.0),
            "active_b": _mk("active_b", "thinking", 0.0, updated_at=198.0),
        })
        policy = CleanupPolicy(retention_seconds=60, zombie_grace_seconds=120, history_limit=2)
        r = cleanup_expired(eng, policy, now=200.0)
        assert "old_done" in r          # 终态优先回收
        assert set(eng.sessions) == {"active_a", "active_b"}  # 活动会话全部豁免

    def test_idle_thinking_reclaimed_before_active(self):
        # 收缩时：终态 > 久未活动 thinking > 活动 thinking
        eng = FakeEngine({
            "idle_a": _mk("idle_a", "thinking", 0.0, updated_at=0.0),      # idle=200
            "idle_b": _mk("idle_b", "thinking", 0.0, updated_at=10.0),     # idle=190
            "active": _mk("active", "thinking", 0.0, updated_at=199.0),    # idle=1
        })
        policy = CleanupPolicy(retention_seconds=60, zombie_grace_seconds=120, history_limit=1)
        r = cleanup_expired(eng, policy, now=200.0)
        assert r == ["idle_a", "idle_b"]
        assert set(eng.sessions) == {"active"}

"""interaction 模块测试（OperationToken / transport proof / 状态流转）。"""

from __future__ import annotations

import time

import pytest

from ga_feishu_streaming_card.interaction import (
    DEFAULT_TTL_SECONDS,
    OperationToken,
    issue_token,
    issue_transport_proof,
    transition_interaction_status,
    verify_token,
    verify_transport_proof,
)


class TestOperationToken:
    def test_issue_and_verify_ok(self):
        op = issue_token(chat_id="oc_1", message_id="om_1", secret="s3cret")
        assert op.chat_id == "oc_1"
        assert op.message_id == "om_1"
        assert op.scope == "interaction"
        assert op.expires_at > time.time()
        assert verify_token(op, op.token) is True

    def test_wrong_token_rejected(self):
        op = issue_token("oc_1")
        assert verify_token(op, "deadbeef") is False

    def test_expired_token_rejected(self):
        op = issue_token("oc_1", ttl_seconds=-1)
        assert op.expired() is True
        assert verify_token(op, op.token) is False

    def test_missing_parts_rejected(self):
        op = issue_token("oc_1")
        assert verify_token(op, "") is False
        assert verify_token(None, "x") is False

    def test_token_has_ttl_field(self):
        op = issue_token("oc_1")
        assert abs((op.expires_at - time.time()) - DEFAULT_TTL_SECONDS) < 1.0


class TestTransportProof:
    def test_ok_proof(self):
        proof = issue_transport_proof("oc_1", "om_1", "interaction.completed", "s3cret")
        expected = issue_transport_proof("oc_1", "om_1", "interaction.completed", "s3cret")
        assert verify_transport_proof(proof, expected) is True

    def test_tampered_proof_rejected(self):
        proof = issue_transport_proof("oc_1", "om_1", "interaction.completed", "s3cret")
        bad = issue_transport_proof("oc_1", "om_1", "interaction.completed", "WRONG")
        assert verify_transport_proof(proof, bad) is False

    def test_missing_proof_rejected(self):
        assert verify_transport_proof(None, "x") is False
        assert verify_transport_proof("x", None) is False
        assert verify_transport_proof("", "x") is False


class TestStatusTransition:
    def test_pending_to_completed(self):
        assert transition_interaction_status("pending", "completed") == "completed"

    def test_pending_to_failed(self):
        assert transition_interaction_status("pending", "failed") == "failed"

    def test_terminal_is_irreversible(self):
        assert transition_interaction_status("completed", "failed") == "completed"
        assert transition_interaction_status("failed", "completed") == "failed"

    def test_unknown_outcome_keeps_pending(self):
        assert transition_interaction_status("pending", "bogus") == "pending"

    def test_unknown_initial_state_fails(self):
        assert transition_interaction_status("whatever", "completed") == "failed"

"""Contract tests.

Each test names the failure mode it prevents. If you delete one, say in the
commit message which failure you are choosing to accept.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vassal.models import (
    Action,
    Budget,
    Escalation,
    ExecutorInfo,
    OnFailure,
    Order,
    Report,
    ReportStatus,
    Rollback,
    RollbackStrategy,
    Tier,
    ToolName,
    Verification,
    VerificationResult,
)
from vassal.schemas import emit

ULID_A = "01J9XKQ2H8F3M4N5P6Q7R8S9T0"
ULID_B = "01J9XKQ2H8F3M4N5P6Q7R8S9T1"


def order(**overrides):
    base = dict(
        order_id=ULID_A,
        plan_id=ULID_B,
        seq=1,
        tier=Tier.READ_ONLY,
        intent="run the auth tests",
        rationale="confirm the failing test is the one we think it is",
        actions=[Action(tool=ToolName.GREP, args={"pattern": "def test_"})],
        verification=Verification(command="pytest tests/test_auth.py -q"),
        on_failure=OnFailure.ESCALATE,
        budget=Budget(),
    )
    return Order(**{**base, **overrides})


def report(**overrides):
    base = dict(
        report_id=ULID_A,
        order_id=ULID_B,
        status=ReportStatus.FAILURE,
        verification=VerificationResult(ran=True, passed=False, exit_code=1),
        executor=ExecutorInfo(model="hermes-4-70b", grammar_enforced=True),
    )
    return Report(**{**base, **overrides})


class TestTiering:
    def test_valid_read_only_order(self):
        assert order().tier is Tier.READ_ONLY

    def test_under_tiering_a_write_is_rejected(self):
        """Prevents: an unreviewed destructive action reaching production."""
        with pytest.raises(ValidationError, match="under-tiers"):
            order(
                tier=Tier.READ_ONLY,
                actions=[Action(tool=ToolName.EDIT_FILE, args={})],
                rollback=Rollback(strategy=RollbackStrategy.GIT_CHECKOUT),
            )

    def test_destructive_order_may_not_retry(self):
        """Prevents: compounding damage from a partially-applied action."""
        with pytest.raises(ValidationError, match="retry"):
            order(
                tier=Tier.DESTRUCTIVE,
                actions=[Action(tool=ToolName.RUN_COMMAND, args={"cmd": "rm -rf x"})],
                on_failure=OnFailure.RETRY,
                rollback=Rollback(strategy=RollbackStrategy.NONE),
            )

    def test_mutating_order_must_declare_rollback(self):
        """Prevents: a halt that leaves a tree nobody can recover from a phone."""
        with pytest.raises(ValidationError, match="rollback"):
            order(tier=Tier.SCOPED_WRITE, actions=[Action(tool=ToolName.EDIT_FILE, args={})])


class TestVerificationHonesty:
    def test_unknown_stays_unknown(self):
        """Prevents: 'could not run' silently becoming 'passed' or 'failed'.

        The most dangerous single bug available to this system.
        """
        with pytest.raises(ValidationError):
            VerificationResult(ran=False, passed=True)
        with pytest.raises(ValidationError):
            VerificationResult(ran=False, passed=False)

    def test_ran_requires_a_verdict(self):
        with pytest.raises(ValidationError):
            VerificationResult(ran=True)

    def test_success_requires_passing_verification(self):
        """Prevents: the executor self-reporting success. Silent drift starts here."""
        with pytest.raises(ValidationError, match="does not get a vote"):
            report(
                status=ReportStatus.SUCCESS,
                verification=VerificationResult(ran=False),
            )

    def test_success_with_real_pass_is_accepted(self):
        r = report(
            status=ReportStatus.SUCCESS,
            verification=VerificationResult(ran=True, passed=True, exit_code=0),
        )
        assert r.status is ReportStatus.SUCCESS


class TestEscalation:
    def test_escalation_requires_reason_code(self):
        """Prevents: unattributable escalations, which make EP/ER uncomputable."""
        with pytest.raises(ValidationError, match="reason_code"):
            Escalation(needed=True)

    def test_default_is_no_escalation(self):
        assert report().escalation.needed is False


class TestIdentifiers:
    def test_malformed_ulid_rejected(self):
        with pytest.raises(ValidationError):
            report(report_id="not-a-ulid")


class TestSchemaArtifacts:
    def test_committed_schemas_match_models(self):
        """Prevents: planner and executor disagreeing on the contract.

        A drifted schema looks exactly like model failure and wastes days.
        """
        assert emit(check=True) == 0, "run: vassal schema emit"

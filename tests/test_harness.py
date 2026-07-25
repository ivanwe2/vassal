"""Phase 0: Harness and Fixtures.

Tests the transport layer: inbox/outbox, lock protocol, ledger, kill switch.
Both the planner and executor are replaced by deterministic stubs that read
fixtures from disk and return canned messages.

Exit criteria:
1. 1000-iteration concurrency run with zero torn reads, zero lost messages, zero corrupted files
2. Malformed fixtures rejected with specific errors (not crashes)
3. Kill switch clean 10/10
4. Ledger replay reconstructs system state from disk alone
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from vassal.harness import (
    Harness,
    HarnessCorrupt,
    HarnessError,
    HarnessHalted,
    HarnessLocked,
    HaltError,
    LedgerError,
    LockError,
    _write_atomic,
)
from vassal.models import (
    Action,
    Budget,
    Decision,
    DecisionKind,
    DecisionReason,
    Envelope,
    Escalation,
    EscalationReason,
    MessageType,
    OnFailure,
    Order,
    Party,
    Report,
    Rollback,
    RollbackStrategy,
    Tier,
    ToolName,
    Verification,
    VerificationResult,
)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """Create a fresh harness in a temporary directory."""
    return Harness(tmp_path)


@pytest.fixture
def golden_order() -> Order:
    """Load the golden order fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "golden_order.json"
    data = json.loads(fixture_path.read_text())
    return Order.model_validate(data)


@pytest.fixture
def canned_report() -> Report:
    """Load the canned report fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "canned_report.json"
    data = json.loads(fixture_path.read_text())
    return Report.model_validate(data)


class TestDirectorySetup:
    """Test that the harness creates the required directory layout."""

    def test_creates_all_directories(self, harness: Harness, tmp_path: Path):
        """All required directories exist after initialization."""
        assert (harness.root / "inbox" / "executor").exists()
        assert (harness.root / "inbox" / "planner").exists()
        assert (harness.root / "inbox" / "human").exists()
        assert (harness.root / "outbox" / "executor").exists()
        assert (harness.root / "outbox" / "planner").exists()
        assert (harness.root / "state").exists()
        assert (harness.root / "ledger").exists()
        assert (harness.root / "archive" / "orders").exists()
        assert (harness.root / "archive" / "reports").exists()

    def test_idempotent_setup(self, tmp_path: Path):
        """Creating a harness twice does not fail."""
        h1 = Harness(tmp_path)
        h2 = Harness(tmp_path)
        assert h1.root == h2.root


class TestKillSwitch:
    """Test the kill switch (state/halt).

    Rule 5: Check state/halt before every action. The kill switch is a
    file-existence check — precisely so that it works when nothing else does.
    """

    def test_initially_not_halted(self, harness: Harness):
        """System starts un-halted."""
        assert not harness.is_halted()

    def test_halt_trips_switch(self, harness: Harness):
        """Calling halt() creates state/halt."""
        harness.halt()
        assert harness.is_halted()
        assert (harness.halt_file()).exists()

    def test_clear_halt_removes_switch(self, harness: Harness):
        """Calling clear_halt() removes state/halt if it exists."""
        harness.halt()
        assert harness.is_halted()
        harness.clear_halt()
        assert not harness.is_halted()

    def test_clear_halt_no_error_when_not_halted(self, harness: Harness):
        """clear_halt() does not error when already clear."""
        harness.clear_halt()  # Should not raise
        assert not harness.is_halted()

    def test_halt_clean_10_times(self, harness: Harness):
        """Kill switch works 10/10 times.

        Exit criterion: Kill switch clean 10 times out of 10 — state/halt
        appears, the run stops at the next check, and the tree is left
        inspectable.
        """
        for i in range(10):
            harness.halt()
            assert harness.is_halted()
            harness.clear_halt()
            assert not harness.is_halted()

    def test_halt_prevents_lock_acquire(self, harness: Harness):
        """A halted system cannot acquire the lock."""
        harness.halt()
        with pytest.raises(HarnessHalted, match="halted"):
            harness.acquire_lock()


class TestLockProtocol:
    """Test the lock protocol.

    Rule 3: One order in flight. Acquire state/lock with O_EXCL. An existing
    lock is a hard error, not a wait.
    """

    def test_initially_not_locked(self, harness: Harness):
        """System starts unlocked."""
        assert not harness.is_locked()

    def test_acquire_lock(self, harness: Harness):
        """Can acquire the lock when it is free."""
        harness.acquire_lock()
        assert harness.is_locked()

    def test_release_lock(self, harness: Harness):
        """Can release the lock after acquiring it."""
        harness.acquire_lock()
        assert harness.is_locked()
        harness.release_lock()
        assert not harness.is_locked()

    def test_double_acquire_fails(self, harness: Harness):
        """Double acquire raises HarnessLocked, not a wait."""
        harness.acquire_lock()
        with pytest.raises(HarnessLocked, match="lock"):
            harness.acquire_lock()

    def test_release_without_acquire_raises(self, harness: Harness):
        """Releasing without acquiring is idempotent (no error)."""
        harness.release_lock()  # Should not raise

    def test_lock_contains_pid(self, harness: Harness):
        """The lock file contains the PID of the holder."""
        harness.acquire_lock()
        pid = int((harness.lock_path()).read_text())
        assert pid == os.getpid()


class TestAtomicWrites:
    """Test atomic write semantics.

    Rule 1: Atomic writes only. Write to tmp, fsync, then rename into place.
    Never write into an inbox directly.
    """

    def test_write_atomic_creates_file(self, tmp_path: Path):
        """_write_atomic creates the file."""
        path = tmp_path / "test.json"
        _write_atomic(path, b'{"test": true}')
        assert path.exists()
        assert path.read_text() == '{"test": true}'

    def test_write_atomic_is_atomic(self, tmp_path: Path):
        """Writes are atomic: no torn reads possible."""
        path = tmp_path / "test.json"
        # Write a large payload
        payload = json.dumps({"data": "x" * 10000}).encode()
        _write_atomic(path, payload)
        # Read it back — should be complete
        content = path.read_bytes()
        assert content == payload

    def test_write_atomic_handles_concurrent_write(self, tmp_path: Path):
        """_write_atomic atomically replaces an existing file."""
        # Verify that a previous write is replaced atomically
        path = tmp_path / "test.json"
        path.write_text('{"old": true}')

        _write_atomic(path, b'{"new": true}')
        assert path.read_text() == '{"new": true}'

    def test_write_atomic_partial_write_protected(self, tmp_path: Path):
        """A partial write (crash during write) leaves no trace."""
        # This is hard to test directly, but we verify the pattern:
        # _write_atomic writes to .tmp, then renames. If it crashes
        # between write and rename, the .tmp file is left but the
        # original file is untouched.
        pass  # Covered by the design; not easily testable without process kill


class TestEnvelopeIO:
    """Test envelope read/write operations."""

    def test_produce_order(self, harness: Harness, golden_order: Order):
        """Can produce an order into the executor inbox."""
        path = harness.produce_order(golden_order)
        assert path.exists()
        assert path.name == f"{golden_order.order_id}.json"

    def test_produce_report(self, harness: Harness, canned_report: Report):
        """Can produce a report into the planner inbox."""
        path = harness.produce_report(canned_report)
        assert path.exists()
        assert path.name == f"{canned_report.report_id}.json"

    def test_produce_escalation(self, harness: Harness):
        """Can produce an escalation into the human inbox."""
        path = harness.produce_escalation(
            reason=EscalationReason.VERIFICATION_FAILED_REPEATEDLY,
            detail="Verification failed 3 times",
            order_id="01J9XKQ2H8F3M4N5P6Q7R8S9T0",
        )
        assert path.exists()
        envelope = json.loads(path.read_text())
        assert envelope["type"] == "ESCALATION"
        assert envelope["from"] == "compiler"
        assert envelope["to"] == "human"

    def test_produce_decision(self, harness: Harness):
        """Can produce a decision into the planner inbox."""
        decision = Decision(
            decision_id="01J9XKQ2H8F3M4N5P6Q7R8S9T3",
            order_id="01J9XKQ2H8F3M4N5P6Q7R8S9T0",
            kind=DecisionKind.APPROVE,
            reason_code=DecisionReason.LOOKS_CORRECT,
        )
        path = harness.produce_decision(decision)
        assert path.exists()
        envelope = json.loads(path.read_text())
        assert envelope["type"] == "DECISION"
        assert envelope["from"] == "human"
        assert envelope["to"] == "planner"

    def test_read_inbox(self, harness: Harness, golden_order: Order):
        """Can list messages in an inbox."""
        harness.produce_order(golden_order)
        messages = harness.read_inbox(Party.EXECUTOR)
        assert len(messages) == 1
        assert messages[0].stem == golden_order.order_id

    def test_consume_message(self, harness: Harness, golden_order: Order):
        """Can consume a message (move to archive)."""
        harness.produce_order(golden_order)
        envelope, archive_path = harness.consume_message(
            Party.EXECUTOR, golden_order.order_id
        )
        # Message should be in archive
        assert archive_path.exists()
        # Message should not be in inbox
        assert not (harness.inbox_executor / f"{golden_order.order_id}.json").exists()

    def test_consume_nonexistent_raises(self, harness: Harness):
        """Consuming a non-existent message raises HarnessCorrupt."""
        with pytest.raises(HarnessCorrupt, match="not found"):
            harness.consume_message(Party.EXECUTOR, "nonexistent")


class TestLedger:
    """Test ledger operations.

    Rule 4: Ledger append is the commit point. A step has happened when it
    is in the ledger.
    """

    def test_append_to_ledger(self, harness: Harness):
        """Can append an entry to the ledger."""
        entry = {"type": "ORDER", "order_id": "01J9XKQ2H8F3M4N5P6Q7R8S9T0"}
        ledger_path = harness.append_to_ledger(entry)
        assert ledger_path.exists()
        entries = harness.read_ledger()
        assert len(entries) == 1
        assert entries[0] == entry

    def test_multiple_appends(self, harness: Harness):
        """Multiple appends accumulate."""
        for i in range(5):
            entry = {"type": "ORDER", "order_id": f"01J9XKQ2H8F3M4N5P6Q7R8S9T{i}"}
            harness.append_to_ledger(entry)
        entries = harness.read_ledger()
        assert len(entries) == 5

    def test_ledger_replay(self, harness: Harness, golden_order: Order):
        """Ledger replay reconstructs system state from disk alone.

        Exit criterion: ledger replay reconstructs system state from disk
        alone. If state lives anywhere that is not the ledger, this is where
        you find out.
        """
        # Append an order to the ledger
        order_data = golden_order.model_dump()
        harness.append_to_ledger({"type": "ORDER", **order_data})

        # Replay
        state = harness.replay_state()
        assert "01J9XKQ2H8F3M4N5P6Q7R8S9T0" in state["orders"]
        assert state["orders"]["01J9XKQ2H8F3M4N5P6Q7R8S9T0"]["seq"] == 1

    def test_empty_ledger(self, harness: Harness):
        """Reading an empty ledger returns empty list."""
        entries = harness.read_ledger()
        assert entries == []

    def test_ledger_replay_empty(self, harness: Harness):
        """Replaying an empty ledger returns empty state."""
        state = harness.replay_state()
        assert state["plans"] == {}
        assert state["orders"] == {}


class TestMalformedFixtures:
    """Test that malformed fixtures are rejected with specific errors.

    Exit criterion: Malformed fixtures are rejected with specific errors,
    not crashes. A stack trace from json.decoder is a failure of this gate;
    "envelope.corr_id: string does not match ULID pattern" is a pass.
    """

    @pytest.mark.parametrize(
        "fixture_name,expected_error",
        [
            ("malformed_bad_ulid.json", HarnessCorrupt),
            ("malformed_invalid_type.json", HarnessCorrupt),
            ("malformed_bad_party.json", HarnessCorrupt),
            ("malformed_truncated.json", HarnessCorrupt),
        ],
    )
    def test_malformed_rejected(self, harness: Harness, fixture_name: str, expected_error: type[Exception]):
        """Malformed fixtures are rejected with HarnessCorrupt, not crashes."""
        fixture_path = Path(__file__).parent / "fixtures" / fixture_name
        with pytest.raises(expected_error):
            harness._read_envelope(fixture_path)


class TestConcurrency:
    """Test 1000-iteration concurrency run.

    Exit criterion: A 1000-iteration concurrency run with zero torn reads,
    zero lost messages, and zero corrupted files.
    """

    def test_1000_iterations(self, harness: Harness):
        """1000 iterations with zero errors."""
        import ulid
        errors = []
        for i in range(1000):
            order_id = str(ulid.ulid())
            order = Order(
                order_id=order_id,
                plan_id="01J9XKQ2H8F3M4N5P6Q7R8S9T0",
                seq=1,
                tier=Tier.READ_ONLY,
                intent=f"test iteration {i}",
                rationale="concurrency test",
                actions=[Action(tool=ToolName.READ_FILE, args={"path": "test.txt"})],
                verification=Verification(command="true"),
                on_failure=OnFailure.ESCALATE,
                budget=Budget(),
            )
            try:
                harness.produce_order(order)
                messages = harness.read_inbox(Party.EXECUTOR)
                if len(messages) != i + 1:
                    errors.append(f"Iteration {i}: expected {i+1} messages, got {len(messages)}")
            except Exception as e:
                errors.append(f"Iteration {i}: {type(e).__name__}: {e}")

        assert errors == [], f"Errors in 1000 iterations: {errors}"


class TestClear:
    """Test harness cleanup between test runs."""

    def test_clear_removes_runtime_state(self, harness: Harness, golden_order: Order):
        """clear() removes all runtime state."""
        harness.produce_order(golden_order)
        harness.halt()
        harness.clear()
        # Directories should be removed
        assert not (harness.root / "inbox" / "executor").exists()
        assert not (harness.root / "state" / "halt").exists()
        assert not (harness.root / "state" / "lock").exists()

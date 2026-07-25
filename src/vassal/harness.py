"""Harness — the transport layer for Vassal.

Files on disk are the message bus. This module implements the five rules from
docs/PROTOCOL.md:

1. Atomic writes only (tmp → fsync → rename)
2. Move, never delete (archive on consume)
3. One order in flight (O_EXCL lock)
4. Ledger append is the commit point
5. Check state/halt before every action

The harness is deliberately synchronous and single-threaded: one order in
flight at a time makes asyncio pointless, and readable tracebacks are the
primary debugging tool for a system supervised remotely.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import ulid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vassal.models import (
    Decision,
    DecisionKind,
    DecisionReason,
    Envelope,
    Escalation,
    EscalationReason,
    MessageType,
    Order,
    Party,
    Report,
    ULID,
)


class HarnessError(Exception):
    """Base error for harness operations."""


class HarnessLocked(HarnessError):
    """Another process already holds the lock. One order in flight."""


class HarnessHalted(HarnessError):
    """state/halt exists. The kill switch has been tripped."""


class HarnessCorrupt(HarnessError):
    """A file is malformed or inconsistent. The transport is compromised."""


class DirectorySetupError(HarnessError):
    """Failed to create required directories."""


class AtomicWriteError(HarnessError):
    """Atomic write failed mid-operation."""


class LedgerError(HarnessError):
    """Ledger operation failed."""


class LockError(HarnessError):
    """Lock acquisition or release failed."""


class HaltError(HarnessError):
    """Halt file could not be created or removed."""


def _now_rfc3339() -> str:
    """Current UTC time in RFC 3339 with millisecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ulid() -> ULID:
    """Generate a ULID for a new message using the ulid library."""
    return str(ulid.ulid())


def _write_atomic(path: Path, content: bytes) -> None:
    """Write content atomically: tmp → fsync → rename.

    Rule 1: Never write into an inbox directly. Write to tmp, fsync, then
    rename into place. A partial write is visible to readers; rename within
    a filesystem is atomic.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd = -1
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.write(fd, content)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(str(tmp_path), str(path))
    except FileExistsError:
        # Another process wrote between our O_EXCL and our rename. This is
        # a race — the message already exists, which means a concurrent
        # writer is violating Rule 3 (one order in flight). Hard error.
        if fd >= 0:
            os.close(fd)
        raise HarnessError(
            f"Concurrent write to {path}; another process should not be "
            "writing to an inbox while we are. This indicates a lock protocol "
            "violation."
        )
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _move_atomic(src: Path, dst: Path) -> None:
    """Move a file atomically. Rule 2: Move, never delete.

    Consuming a message means renaming it into archive/. Nothing is ever
    unlinked. Losing a message makes 'delivered' indistinguishable from
    'mishandled'.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(str(src), str(dst))
    except FileNotFoundError:
        raise HarnessCorrupt(
            f"Source {src} does not exist; cannot move. Possible torn read "
            "or premature deletion."
        )


class Harness:
    """The Vassal transport harness.

    Manages the directory layout, message bus, lock protocol, ledger, and
    kill switch. All operations are synchronous and thread-safe via the
    filesystem lock.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self._setup_dirs()

    def _setup_dirs(self) -> None:
        """Create the directory layout. Idempotent."""
        dirs = [
            self.root / "inbox" / "executor",
            self.root / "inbox" / "planner",
            self.root / "inbox" / "human",
            self.root / "outbox" / "executor",
            self.root / "outbox" / "planner",
            self.root / "state",
            self.root / "ledger",
            self.root / "archive" / "orders",
            self.root / "archive" / "reports",
        ]
        for d in dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise DirectorySetupError(
                    f"Failed to create directory {d}: {e}"
                ) from e

    # --- Directory accessors ---

    @property
    def inbox_executor(self) -> Path:
        return self.root / "inbox" / "executor"

    @property
    def inbox_planner(self) -> Path:
        return self.root / "inbox" / "planner"

    @property
    def inbox_human(self) -> Path:
        return self.root / "inbox" / "human"

    @property
    def outbox_executor(self) -> Path:
        return self.root / "outbox" / "executor"

    @property
    def outbox_planner(self) -> Path:
        return self.root / "outbox" / "planner"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def ledger_dir(self) -> Path:
        return self.root / "ledger"

    @property
    def archive_orders(self) -> Path:
        return self.root / "archive" / "orders"

    @property
    def archive_reports(self) -> Path:
        return self.root / "archive" / "reports"

    # --- Kill switch ---

    def halt_file(self) -> Path:
        return self.state_dir / "halt"

    def is_halted(self) -> bool:
        """Check if the kill switch is tripped.

        Rule 5: Check state/halt before every action. The kill switch is a
        file-existence check — precisely so that it works when nothing else
        does. No IPC, no signal handler, no API call.
        """
        return self.halt_file().exists()

    def halt(self) -> None:
        """Trip the kill switch. Creates state/halt."""
        try:
            self.halt_file().touch()
        except OSError as e:
            raise HaltError(f"Failed to create halt file: {e}") from e

    def clear_halt(self) -> None:
        """Clear the kill switch. Removes state/halt if it exists."""
        try:
            if self.halt_file().exists():
                self.halt_file().unlink()
        except OSError as e:
            raise HaltError(f"Failed to remove halt file: {e}") from e

    # --- Lock protocol ---

    def lock_path(self) -> Path:
        return self.state_dir / "lock"

    def acquire_lock(self) -> None:
        """Acquire the harness lock with O_EXCL.

        Rule 3: One order in flight. An existing lock is a hard error, not
        a wait. Waiting hides the concurrency bug that produced the
        contention.
        """
        if self.is_halted():
            raise HarnessHalted("Cannot acquire lock: system is halted")

        lock = self.lock_path()
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, str(os.getpid()).encode())
            os.fsync(fd)
            os.close(fd)
        except FileExistsError:
            # Another process holds the lock. One order in flight.
            raise HarnessLocked(
                f"Lock {lock} exists; another process has the order in flight. "
                "Do not wait — fail loudly."
            )
        except OSError as e:
            raise LockError(f"Failed to acquire lock: {e}") from e

    def release_lock(self) -> None:
        """Release the harness lock. Removes state/lock."""
        lock = self.lock_path()
        try:
            if lock.exists():
                lock.unlink()
        except OSError as e:
            raise LockError(f"Failed to release lock: {e}") from e

    def is_locked(self) -> bool:
        """Check if the harness is currently locked."""
        return self.lock_path().exists()

    # --- Envelope I/O ---

    def _read_envelope(self, path: Path) -> Envelope:
        """Read and parse an envelope from disk.

        Validates JSON structure but NOT payload schema — that's the
        compiler's job. A JSON parse error here is a transport corruption,
        not a schema violation.
        """
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise HarnessCorrupt(f"Envelope not found: {path}")
        except OSError as e:
            raise HarnessCorrupt(
                f"Failed to read envelope {path}: {e}"
            ) from e

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise HarnessCorrupt(
                f"Envelope {path} contains invalid JSON: {e.msg} at line "
                f"{e.lineno}, column {e.colno}"
            ) from e

        try:
            envelope = Envelope.model_validate(data)
        except ValidationError as e:
            # Envelope-level validation failed. The structure is broken.
            # Report the specific error so the operator can inspect.
            first_error = e.errors()[0]
            raise HarnessCorrupt(
                f"Envelope {path} failed validation: field '{first_error['loc']}' "
                f"{first_error['msg']}"
            ) from e

        return envelope

    def _write_envelope(self, envelope: Envelope, directory: Path) -> Path:
        """Write an envelope to a directory atomically.

        Returns the path where the envelope was written.
        """
        path = directory / f"{envelope.id}.json"
        content = envelope.model_dump_json(by_alias=True, indent=2).encode("utf-8")
        _write_atomic(path, content)
        return path

    def read_inbox(self, party: Party) -> list[Path]:
        """List messages in an inbox, sorted by filename (ULID sorts lexicographically).

        Returns paths sorted by creation order (ULID timestamp prefix).
        """
        inbox = self._inbox_for(party)
        if not inbox.exists():
            return []
        files = sorted(inbox.glob("*.json"))
        return [f for f in files if f.is_file()]

    def _inbox_for(self, party: Party) -> Path:
        """Get the inbox directory for a party."""
        if party == Party.EXECUTOR:
            return self.inbox_executor
        elif party == Party.PLANNER:
            return self.inbox_planner
        elif party == Party.HUMAN:
            return self.inbox_human
        else:
            raise ValueError(f"Parties do not have inboxes: {party}")

    def consume_message(
        self, party: Party, message_id: str
    ) -> tuple[Envelope, Path]:
        """Read and archive a message from an inbox.

        Rule 2: Move, never delete. The message is renamed to archive/.
        Returns the parsed envelope and the new archive path.
        """
        inbox = self._inbox_for(party)
        src = inbox / f"{message_id}.json"

        if not src.exists():
            raise HarnessCorrupt(
                f"Message {message_id} not found in {party.value} inbox"
            )

        envelope = self._read_envelope(src)
        archive_dir = (
            self.archive_orders
            if party == Party.EXECUTOR
            else self.archive_reports
        )
        dst = archive_dir / f"{message_id}.json"
        _move_atomic(src, dst)
        return envelope, dst

    def produce_order(self, order: Order) -> Path:
        """Produce an order into the executor inbox.

        The planner writes orders here for the executor to pick up.
        """
        envelope = Envelope(
            v=1,
            id=order.order_id,
            type=MessageType.ORDER,
            ts=_now_rfc3339(),
            from_=Party.PLANNER,
            to=Party.EXECUTOR,
            plan_id=order.plan_id,
            payload=order.model_dump(),
        )
        return self._write_envelope(envelope, self.inbox_executor)

    def produce_report(self, report: Report) -> Path:
        """Produce a report into the planner inbox.

        The executor writes reports here for the planner to pick up.
        """
        envelope = Envelope(
            v=1,
            id=report.report_id,
            type=MessageType.REPORT,
            ts=_now_rfc3339(),
            from_=Party.EXECUTOR,
            to=Party.PLANNER,
            plan_id=report.order_id,  # Uses order_id as plan context
            corr_id=report.order_id,
            payload=report.model_dump(),
        )
        return self._write_envelope(envelope, self.inbox_planner)

    def produce_escalation(
        self, reason: EscalationReason, detail: str, order_id: str
    ) -> Path:
        """Produce an escalation into the human inbox."""
        escalation = Escalation(
            needed=True,
            reason_code=reason,
            detail=detail[:1000],  # Enforce max length
        )
        envelope = Envelope(
            v=1,
            id=_ulid(),
            type=MessageType.ESCALATION,
            ts=_now_rfc3339(),
            from_=Party.COMPILER,
            to=Party.HUMAN,
            plan_id=None,
            corr_id=order_id,
            payload={
                "order_id": order_id,
                "escalation": escalation.model_dump(),
            },
        )
        return self._write_envelope(envelope, self.inbox_human)

    def produce_decision(
        self,
        decision: Decision,
    ) -> Path:
        """Produce a human decision into the planner inbox."""
        envelope = Envelope(
            v=1,
            id=decision.decision_id,
            type=MessageType.DECISION,
            ts=_now_rfc3339(),
            from_=Party.HUMAN,
            to=Party.PLANNER,
            plan_id=None,
            corr_id=decision.order_id,
            payload=decision.model_dump(),
        )
        return self._write_envelope(envelope, self.inbox_planner)

    # --- Ledger ---

    def _ledger_path(self, date: str | None = None) -> Path:
        """Get the ledger file path for a date. Defaults to today."""
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.ledger_dir / f"{date}.jsonl"

    def append_to_ledger(self, entry: dict[str, Any]) -> Path:
        """Append a single line to the daily ledger.

        Rule 4: Ledger append is the commit point. A step has happened when
        it is in the ledger. Not when the file was written, not when the
        model returned.
        """
        ledger = self._ledger_path()
        ledger.parent.mkdir(parents=True, exist_ok=True)

        line = json.dumps(entry, default=str) + "\n"
        content = line.encode("utf-8")

        # Use O_APPEND for safe concurrent appends (single line writes are
        # atomic on Linux for files < PIPE_BUF = 4096 bytes on most systems,
        # and our lines are well under that).
        fd = os.open(str(ledger), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)

        return ledger

    def read_ledger(self, date: str | None = None) -> list[dict[str, Any]]:
        """Read all entries from a daily ledger file."""
        ledger = self._ledger_path(date)
        if not ledger.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            for line in ledger.read_text(encoding="utf-8").strip().split("\n"):
                if line:
                    entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise LedgerError(
                f"Ledger {ledger} contains invalid JSON at line "
                f"{e.lineno}: {e.msg}"
            ) from e
        return entries

    def replay_state(self) -> dict[str, Any]:
        """Reconstruct system state from the ledger alone.

        Exit criterion: ledger replay reconstructs system state from disk
        alone. If state lives anywhere that is not the ledger, this is
        where you find out.
        """
        state: dict[str, Any] = {
            "plans": {},
            "orders": {},
            "reports": {},
            "escalations": [],
            "decisions": [],
        }

        for date_dir in sorted(self.ledger_dir.glob("*.jsonl")):
            for entry in self.read_ledger(date_dir.stem):
                msg_type = entry.get("type")
                if msg_type == "ORDER" and "order_id" in entry:
                    state["orders"][entry["order_id"]] = entry
                    plan_id = entry.get("plan_id")
                    if plan_id and plan_id not in state["plans"]:
                        state["plans"][plan_id] = {
                            "order_ids": [],
                            "status": "in_progress",
                        }
                    if plan_id:
                        state["plans"][plan_id]["order_ids"].append(
                            entry["order_id"]
                        )
                elif msg_type == "REPORT" and "order_id" in entry:
                    state["reports"][entry["order_id"]] = entry
                elif msg_type == "ESCALATION":
                    state["escalations"].append(entry)
                elif msg_type == "DECISION":
                    state["decisions"].append(entry)

        return state

    # --- Utility ---

    def list_inbox(self, party: Party) -> list[str]:
        """List message IDs in an inbox."""
        return [p.stem for p in self.read_inbox(party)]

    def list_archive(self, party: Party) -> list[str]:
        """List archived message IDs."""
        archive = (
            self.archive_orders if party == Party.EXECUTOR else self.archive_reports
        )
        if not archive.exists():
            return []
        return sorted([p.stem for p in archive.glob("*.json")])

    def clear(self) -> None:
        """Remove all runtime state. Used between test runs."""
        import shutil

        for d in [
            self.root / "inbox",
            self.root / "outbox",
            self.root / "state",
            self.root / "ledger",
            self.root / "archive",
        ]:
            if d.exists():
                shutil.rmtree(d)

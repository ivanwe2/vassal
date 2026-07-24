"""Contracts between planner, compiler, and executor.

This module is the single source of truth. The JSON Schema files in ``schema/``
and the GBNF grammars in ``grammar/`` are generated from these models --- never
hand-edited. See ``docs/STACK.md`` for why.

Two layers of enforcement, deliberately separated:

* **Schema layer** --- structural. Emitted to JSON Schema, passed to
  ``claude -p --json-schema``, and converted to GBNF to constrain the executor
  at the sampler. Catches malformed output before it exists.
* **Validator layer** --- semantic. The ``model_validator`` functions below.
  These do *not* appear in the emitted schema, because they encode rules a
  sampler cannot enforce. They run in the compiler gate, after generation.

A message that passes the schema layer but fails the validator layer is the
normal, expected case for a coherent-looking but wrong plan. That is the
compiler earning its keep.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ULID = Annotated[str, Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")]


class Strict(BaseModel):
    """Base with extra fields forbidden, so unknown keys fail loudly."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class Tier(IntEnum):
    """Blast radius. Assign the HIGHEST tier any action in the order reaches.

    Under-tiering is a correctness failure, not a style issue: it is the bug
    that reaches production as an unreviewed destructive action.
    """

    READ_ONLY = 0
    SCOPED_WRITE = 1
    DESTRUCTIVE = 2


class ToolName(StrEnum):
    READ_FILE = "read_file"
    GREP = "grep"
    GLOB = "glob"
    WRITE_FILE = "write_file"
    EDIT_FILE = "edit_file"
    CREATE_FILE = "create_file"
    RUN_COMMAND = "run_command"


#: Minimum tier implied by each tool. ``run_command`` is DESTRUCTIVE by default;
#: the executor's tool binding may lower it for allowlisted commands, and that
#: allowlist is code, not prompt.
TOOL_MIN_TIER: dict[ToolName, Tier] = {
    ToolName.READ_FILE: Tier.READ_ONLY,
    ToolName.GREP: Tier.READ_ONLY,
    ToolName.GLOB: Tier.READ_ONLY,
    ToolName.WRITE_FILE: Tier.SCOPED_WRITE,
    ToolName.EDIT_FILE: Tier.SCOPED_WRITE,
    ToolName.CREATE_FILE: Tier.SCOPED_WRITE,
    ToolName.RUN_COMMAND: Tier.DESTRUCTIVE,
}


class PreconditionType(StrEnum):
    FILE_EXISTS = "file_exists"
    FILE_ABSENT = "file_absent"
    COMMAND_SUCCEEDS = "command_succeeds"
    GIT_CLEAN = "git_clean"
    PATH_WITHIN_WORKTREE = "path_within_worktree"


class Precondition(Strict):
    type: PreconditionType
    path: str | None = None
    command: str | None = None


class Action(Strict):
    tool: ToolName
    args: dict[str, Any] = Field(
        description=(
            "Tool-specific arguments. Validated by the executor's tool binding, "
            "which is the security boundary --- not here."
        )
    )
    note: str | None = Field(
        default=None,
        max_length=200,
        description="Optional per-action context for the human reviewer.",
    )


class Verification(Strict):
    """Ground truth for whether an order succeeded.

    Written by the planner, run by the harness in a subprocess neither model
    controls, evaluated by neither model. A verification command that passes
    regardless of this order's outcome is worse than none at all, because it
    manufactures confidence.
    """

    command: str
    expect_exit_code: int = 0
    timeout_s: int = Field(default=300, ge=1, le=1800)
    detects: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "What specific failure this catches. Hand-reviewed in Phase 1; if it "
            "cannot be stated concretely, the verification is decorative."
        ),
    )


class OnFailure(StrEnum):
    RETRY = "retry"
    ESCALATE = "escalate"
    HALT = "halt"


class Budget(Strict):
    max_attempts: int = Field(default=1, ge=1, le=3)
    timeout_s: int = Field(default=600, ge=1, le=3600)
    max_diff_lines: int | None = Field(
        default=None,
        description=(
            "Exceeding this escalates rather than proceeding. Keeps orders "
            "reviewable on a phone."
        ),
    )


class RollbackStrategy(StrEnum):
    GIT_CHECKOUT = "git_checkout"
    GIT_STASH = "git_stash"
    COMMAND = "command"
    NONE = "none"


class Rollback(Strict):
    strategy: RollbackStrategy
    command: str | None = None


class Order(Strict):
    """One unit of work a local executor can perform without interpretation."""

    order_id: ULID
    plan_id: ULID
    seq: int = Field(ge=1, description="1-based. Used to plot FPER by step index.")
    tier: Tier
    intent: str = Field(max_length=120, description="One line, imperative.")
    rationale: str = Field(
        max_length=280,
        description=(
            "Why this step, for a distracted human reviewing on a phone. Plain "
            "language. Do not restate the intent."
        ),
    )
    preconditions: list[Precondition] = Field(default_factory=list)
    actions: list[Action] = Field(min_length=1, max_length=8)
    verification: Verification
    on_failure: OnFailure
    budget: Budget
    rollback: Rollback | None = None
    context_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Paths or ledger IDs to read for context. Orders must otherwise be "
            "self-contained: the executor has a small context window and no "
            "memory of the plan."
        ),
    )

    @model_validator(mode="after")
    def _tier_covers_actions(self) -> Order:
        required = max(TOOL_MIN_TIER[a.tool] for a in self.actions)
        if self.tier < required:
            raise ValueError(
                f"tier {int(self.tier)} under-tiers actions requiring tier "
                f"{int(required)}. Fail closed: raise the tier or split the order."
            )
        return self

    @model_validator(mode="after")
    def _destructive_never_retries(self) -> Order:
        if self.tier is Tier.DESTRUCTIVE and self.on_failure is OnFailure.RETRY:
            raise ValueError(
                "tier 2 orders must not use on_failure=retry; retrying a "
                "destructive action that partially applied compounds the damage"
            )
        return self

    @model_validator(mode="after")
    def _mutating_orders_declare_rollback(self) -> Order:
        if self.tier >= Tier.SCOPED_WRITE and self.rollback is None:
            raise ValueError(
                "orders at tier >= 1 must declare a rollback strategy "
                "(use strategy='none' explicitly if genuinely irreversible)"
            )
        return self


class ReportStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PRECONDITION_FAILED = "precondition_failed"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    HALTED = "halted"


class VerificationResult(Strict):
    """``ran`` and ``passed`` are separate on purpose.

    A verification command that could not execute --- missing binary, bad path,
    wrong cwd --- is NOT a failed verification. Collapsing those two states is
    the single most dangerous bug this system can have, because it turns "I
    don't know" into "it's fine" or "it's broken" at random.
    """

    ran: bool
    passed: bool | None = Field(
        default=None, description="null when ran is false. Never infer this."
    )
    exit_code: int | None = None
    stdout_tail: str = Field(default="", max_length=4000)
    stderr_tail: str = Field(default="", max_length=4000)
    duration_s: float | None = None

    @model_validator(mode="after")
    def _unknown_stays_unknown(self) -> VerificationResult:
        if not self.ran and self.passed is not None:
            raise ValueError("passed must be null when ran is false")
        if self.ran and self.passed is None:
            raise ValueError("passed must be set when ran is true")
        return self


class Changes(Strict):
    """What changed on disk, read from git --- not from the model's recollection."""

    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    paths: list[str] = Field(default_factory=list)
    diff_truncated: bool = False


class EscalationReason(StrEnum):
    VERIFICATION_FAILED_REPEATEDLY = "verification_failed_repeatedly"
    VERIFICATION_COULD_NOT_RUN = "verification_could_not_run"
    PRECONDITION_UNSATISFIABLE = "precondition_unsatisfiable"
    ORDER_INCOHERENT = "order_incoherent"
    TOOL_DENIED_BY_TIER = "tool_denied_by_tier"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DIFF_CEILING_EXCEEDED = "diff_ceiling_exceeded"
    UNEXPECTED_REPO_STATE = "unexpected_repo_state"
    EXTERNAL_ERROR = "external_error"


class Escalation(Strict):
    needed: bool
    reason_code: EscalationReason | None = None
    detail: str | None = Field(
        default=None,
        max_length=1000,
        description=(
            "Written for the PLANNER, not the human. Observed facts only --- no "
            "hypotheses about intent."
        ),
    )

    @model_validator(mode="after")
    def _reason_required(self) -> Escalation:
        if self.needed and self.reason_code is None:
            raise ValueError("escalation.needed requires a reason_code")
        return self


class ExecutorInfo(Strict):
    model: str
    runtime_s: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    grammar_enforced: bool = Field(
        default=False,
        description=(
            "Whether constrained decoding was active. False invalidates the run "
            "for Phase 2 measurement purposes."
        ),
    )


class Report(Strict):
    """The executor's structured result for exactly one order."""

    report_id: ULID
    order_id: ULID
    status: ReportStatus
    attempts: int = Field(default=1, ge=1)
    verification: VerificationResult
    changes: Changes | None = None
    actions_completed: int = 0
    escalation: Escalation = Field(default_factory=lambda: Escalation(needed=False))
    executor: ExecutorInfo
    notes: str | None = Field(
        default=None,
        max_length=1000,
        description="Free text. Never load-bearing. No control flow may read this.",
    )

    @model_validator(mode="after")
    def _success_is_derived_not_asserted(self) -> Report:
        if self.status is ReportStatus.SUCCESS and self.verification.passed is not True:
            raise ValueError(
                "status=success requires verification.passed=true. The executor "
                "does not get a vote on whether it succeeded."
            )
        return self


class MessageType(StrEnum):
    ORDER = "ORDER"
    REPORT = "REPORT"
    ESCALATION = "ESCALATION"
    DECISION = "DECISION"
    ACK = "ACK"
    HALT = "HALT"


class Party(StrEnum):
    PLANNER = "planner"
    COMPILER = "compiler"
    EXECUTOR = "executor"
    HUMAN = "human"


class DecisionKind(StrEnum):
    APPROVE = "approve"
    APPROVE_WITH_EDIT = "approve_with_edit"
    REJECT = "reject"


class DecisionReason(StrEnum):
    """Why the human decided as they did.

    Not optional, and not free text. These are the labels on the eval set ---
    an approval without a reason code is a lost data point, and labelled human
    judgement is the scarcest thing this project produces.
    """

    LOOKS_CORRECT = "looks_correct"
    WRONG_STEP = "wrong_step"
    WRONG_ORDER_OF_STEPS = "wrong_order_of_steps"
    VERIFICATION_TOO_WEAK = "verification_too_weak"
    VERIFICATION_WRONG_TARGET = "verification_wrong_target"
    TIER_TOO_LOW = "tier_too_low"
    SCOPE_TOO_LARGE = "scope_too_large"
    UNSAFE_ACTION = "unsafe_action"
    REDUNDANT = "redundant"
    UNCLEAR_RATIONALE = "unclear_rationale"


class Decision(Strict):
    decision_id: ULID
    order_id: ULID
    kind: DecisionKind
    reason_code: DecisionReason
    edited_order: Order | None = None
    comment: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _edits_carry_the_edit(self) -> Decision:
        if self.kind is DecisionKind.APPROVE_WITH_EDIT and self.edited_order is None:
            raise ValueError("approve_with_edit requires edited_order")
        return self


class Envelope(Strict):
    """Transport wrapper. One JSON object per file, named ``<ulid>.json``."""

    v: Literal[1] = 1
    id: ULID
    type: MessageType
    ts: str = Field(description="RFC 3339 UTC, millisecond precision.")
    from_: Party = Field(alias="from")
    to: Party
    plan_id: ULID | None = None
    corr_id: ULID | None = Field(
        default=None, description="The id this responds to. Reports must set it."
    )
    payload: dict[str, Any]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

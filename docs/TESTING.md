# Testing

This is a promotion ladder, not a checklist. Each phase is a gate with numeric
exit criteria, and a component is only admitted to the loop once the phase below
it has passed. Nothing here is about coverage percentages.

## Why it is this strict

When the planner, the compiler, and the executor are all in the loop at once, a
failure is not **attributable**. A wrong file on disk is consistent with all of:
the planner wrote an incoherent order; the compiler let a bad order through; the
executor misread a good order; the verification command was pointed at the wrong
target; the transport delivered a stale message. You cannot tell which from the
outcome, and you will spend days tuning prompts to fix what is actually a schema
bug.

Every phase below exists to make failures attributable. The method is always the
same: admit exactly one new source of nondeterminism at a time, and hold
everything else at deterministic stubs or fixtures. A phase you skip does not
save time — it converts a clear failure in that phase into a confusing failure
three phases later.

## Metrics

Defined once here. Every one of these is computed **from the ledger**, never
from a model's self-report, and never by hand-tallying. If a metric cannot be
computed from committed ledger files, it is not a metric — it is an impression.

| Metric | Full name | Definition | Read it as |
| --- | --- | --- | --- |
| **OVR** | Order validity rate | Orders passing schema **and** validators / orders emitted | Is the planner speaking the protocol? |
| **FPER** | First-pass execution rate | Orders whose verification passes on attempt 1 / orders executed | Is the loop actually working? |
| **VSA** | Verification signal accuracy | Verification verdicts that match hand-adjudicated ground truth / verdicts sampled | **Do the green ticks mean anything?** |
| **EP** | Escalation precision | Escalations that were genuinely unresolvable / escalations raised | False escalations burn human quota |
| **ER** | Escalation recall | Escalations raised / situations that warranted one | Missed escalations grind on unsalvageable plans |
| **HIR** | Human intervention rate | Orders requiring edit, rejection, or manual repair / orders reviewed | How much of the work is still yours? |
| **CCL** | Cost per closed loop | Sum of `total_cost_usd` per completed plan | Is this cheaper than doing it yourself? |
| — | Silent failure count | Runs where the system reported success or made no noise, and the work was wrong | Hard gate. Target is zero, always. |

### VSA is the most important metric in this document

Everything else measures whether the system does things. VSA measures whether
the system *knows* whether it did them.

High FPER with low VSA is the worst state this project can be in, and it looks
from the outside exactly like success: a stream of green ticks, a rising
completion count, and a codebase quietly filling with wrong work. A system with
low FPER and high VSA is merely slow, and slow is recoverable. A system with
high FPER and low VSA is confidently doing the wrong thing, and every hour it
runs increases the cost of finding out.

Measure VSA by sampling closed orders and adjudicating them by hand: did the
verification verdict match what actually happened? Never let a model adjudicate
its own verification — that is the loop the metric exists to break.

### On EP and ER

Both matter, but not equally, and the asymmetry should drive tuning. A false
escalation costs one interruption. A missed escalation lets the system grind
forward on a plan that cannot be salvaged, burning quota and producing work
that must be thrown away. **Recall matters more than precision.** Tune the
trigger toward escalating.

---

## Phase 0 — Harness and fixtures

**Neither model is present.** Both the planner and the executor are replaced by
deterministic stubs that read fixtures from disk and return canned messages.
This is the only phase where the entire system is reproducible, so it is the
only phase where transport bugs are cheap to find.

What is under test: the envelope round-trip, atomic writes, ledger append,
validator behaviour, the lock protocol, and the kill switch.

**Exit criteria**

- A 1000-iteration concurrency run with zero torn reads, zero lost messages, and
  zero corrupted files.
- Malformed fixtures are rejected with **specific errors**, not crashes. A
  stack trace from `json.decoder` is a failure of this gate; "envelope.corr_id:
  string does not match ULID pattern" is a pass.
- Kill switch clean 10 times out of 10 — `state/halt` appears, the run stops at
  the next check, and the tree is left inspectable.
- Ledger replay reconstructs system state from disk alone. If state lives
  anywhere that is not the ledger, this is where you find out.

Current status: **this is where the repo is.** Contracts and tests exist;
transport does not yet.

---

## Phase 1 — Planner conformance

**Claude alone. No executor.** Orders are generated and inspected; nothing is
executed. Run 30 prompts across three difficulty bands (roughly: single obvious
step; multi-step with an ordering constraint; underspecified enough that the
correct response may be to ask or halt).

Expect this early insight to keep recurring: **most Phase 1 failures are schema
design problems wearing a model-behaviour costume.** When the planner keeps
emitting something that fails validation, the first hypothesis should be that
the schema makes the right thing awkward to express — not that the model needs a
firmer prompt. Prompt-tuning a schema bug is the single most effective way to
waste a week on this project.

Hand-review **100%** of orders for *semantic* quality. Structural validity is
already machine-checked; that is not what review is for. The question is:

> Would this verification command actually detect failure of *this* step?

A verification that passes regardless of the order's outcome is worse than no
verification, because it manufactures confidence. The `detects` field exists to
force this question to be answerable; if it cannot be filled in concretely, the
verification is decorative.

**Exit criteria**

- OVR ≥ 95%.
- ≥ 90% of verifications judged sound on hand review.
- **Zero under-tiered orders.** Hard fail — not a percentage. One under-tiered
  order is one unreviewed destructive action, and the fact that it did not
  execute this phase is luck, not safety.
- `--resume` demonstrably carries context across 5+ calls.
- Baseline CCL recorded, so later phases have something to compare against.

---

## Phase 2 — Executor conformance

**Hermes alone**, fed golden fixture orders written by hand. The planner is not
running.

Report schema conformance is enforced **at the sampler** — GBNF via llama.cpp,
or xgrammar via vLLM — not by asking nicely in the system prompt. Constrained
decoding makes malformed output impossible rather than unlikely, and the
difference matters at the tail. `executor.grammar_enforced` records whether it
was active; a run with it false does not count toward this phase's measurements.

Run each golden order **10 times** — the interesting failures are the
intermittent ones.

Include, deliberately, **one golden order with a subtly wrong instruction that
passes its own verification.** Confirm the executor performs it faithfully
anyway. Faithfulness to a bad order is **correct behaviour** here. An executor
that silently improves on its orders destroys the signal the entire loop depends
on: if the executor quietly fixes bad plans, the planner never learns it wrote
one, and the verification never gets the chance to catch it. The executor is not
a check on the planner's work. The verification is.

**Exit criteria**

- 100% report schema conformance.
- ≥ 90% execution correctness at tiers 0–1.
- 100% correct refusal on unsatisfiable preconditions. A missing file is not an
  invitation to create it.
- **Zero instances of reporting success without running verification.**

---

## Phase 3 — Single-hop closed loop, supervised

Planner and executor both live, one order at a time, human approving every
order. Run 20 loops.

The critical test in this phase is not that loops close. It is whether the
planner **actually conditions on reports**:

> Inject a failure report for a step that actually succeeded. Check that the
> next order changes.

If the next order is identical, the planner is generating a plan from the
original prompt and treating reports as decoration. Everything downstream —
escalation, recovery, multi-step work — is built on the assumption that reports
change behaviour, and this is the cheapest place to verify that assumption is
true. Run the mirror case too if time allows: inject a success report for a step
that failed, and confirm the planner moves on rather than repeating.

**Exit criteria**

- 20/20 loops close without manual file surgery. Editing a message by hand to
  unstick the system is a failed loop, however small the edit.
- The injected-failure test passes.
- HIR baseline recorded.
- Every human decision carries a reason code — see below.
- VSA ≥ 95%.

---

## Phase 4 — Multi-step supervised

Plans of 5–15 steps, human still approving. **This is the phase that tells you
whether the project works.** Everything before it tests components; this tests
the premise.

Plot **FPER by step index**. The shape matters more than the average: a
downward slope from step 1 to step 10 means context degradation, and no amount
of prompt work fixes a plan that gets worse the longer it runs. `Order.seq`
exists precisely so this plot is a one-liner over the ledger.

Tune the escalation trigger here. Start at: two consecutive verification
failures, **or** an unparseable order, **or** any tier-2 order. Adjust from
measured EP/ER, remembering that recall is worth more than precision.

**Exit criteria**

- FPER ≥ 80%, **with no decline from step 1 to step 10.**
- EP ≥ 80%, ER ≥ 90%.
- HIR at or below the Phase 3 baseline. If supervision cost goes *up* as plans
  get longer, the system is not amortising.
- CCL documented.

One honest-reporting rule: **if CCL approaches the cost of just using Claude
Code directly, record that plainly rather than explaining it away.** That number
is the project's central empirical question, and a result that says "this is not
worth it" is a real finding delivered cheaply. The failure mode to avoid is
discovering it at Phase 7 having already argued yourself out of it at Phase 4.

---

## Phase 5 — Failure injection

Everything above tests the system working. This tests the system breaking. Each
fault is injected **at least 5 times**, because the failure that matters is
usually the intermittent one.

| Fault injected | What it must prove |
| --- | --- |
| Malformed order | Rejected with a specific error; no partial execution |
| Verification binary missing | **Distinguishes "verification failed" from "verification did not run"** |
| Verification passes but the work is wrong | Caught by VSA sampling; the gap is visible, not silent |
| Executor timeout mid-write | Well-formed report still produced; tree state legible |
| Invalid JSON despite the grammar | Halt, not a guess at what was meant |
| `--resume` session expired | **Halt.** Never silently start a fresh session |
| Network loss mid-call | Clean halt; no half-applied plan |
| Two orders in flight | Lock refuses the second; hard error, not a wait |
| Git conflict with human edits | Surfaces as a conflict, never an overwrite |
| Rate limit hit | Legible halt with the reason recorded in the ledger |
| Tier above threshold, human unreachable | Waits or halts. Never proceeds. |

Two of these deserve emphasis. **The missing-verification-binary case** is the
direct test of the `ran`/`passed` split: a verification that could not execute
must produce `ran: false, passed: null`, and any code path that turns that into
`false` has reintroduced the most dangerous bug in the design. **The expired
`--resume` session** must halt, because a planner without history will re-issue
work it already completed, and re-issuing a tier-1 order is how you get a
double-applied edit.

**Exit criteria**

- Every fault produces either a clean, legible halt or a correct escalation.
- **Silent failure count = 0.** Hard gate.
- Every halt leaves the tree recoverable from a phone.

---

## Phase 6 — Bounded autonomy

Approval becomes a policy toggle rather than a step. This is a configuration
change to a system that has already earned it — not a new capability.

**Preconditions, all required:**

- Phase 5 fully met.
- HIR < 15% over the last 50 supervised orders.
- Scratch repository only.
- Hard step, wall-clock, and spend budgets **enforced in code, not in prompts.**
  A budget expressed as an instruction is a suggestion.
- Kill switch reachable from wherever you actually are.

Sequence: tier 0 automatic first (20 tasks), then tier 1 (20 more).

**Tier 2 stays human-gated permanently.** This is a design decision, not a phase
that eventually arrives. There is no configuration flag for it and none should
be added.

Review **100% of ledgers** afterward. Autonomy moves review to after the fact;
it does not remove it. A phase-6 run whose ledger nobody read is not autonomy,
it is just an unobserved system.

---

## Phase 7 — Soak

A real repository, over weeks. New failure modes here are mostly about drift
rather than logic.

**Re-measure VSA monthly.** Verification conditions rot as a codebase changes: a
test gets renamed, a path moves, a command starts passing for an unrelated
reason. A stale verification is indistinguishable from no verification, and it
degrades silently — nothing fails, the ticks stay green, and the signal is gone.
This is the one metric that must be re-measured on a calendar rather than on an
event.

---

## Field mode — phone-only supervision

Treat this as a forcing function, not a limitation. Constraints that make the
system supervisable from a phone are the same constraints that make it
attributable at a desk.

- **Autonomy stays off.** You cannot investigate a subtle failure from a phone,
  and an uninvestigated failure teaches nothing — it just accumulates.
- **Orders must fit a phone screen.** An oversized order is a **compiler bug**,
  not a review inconvenience. Fix it with `budget.max_diff_lines` and by
  splitting the order, never by scrolling more.
- **Rationale is written for a distracted human.** Plain language, one line, why
  this step and why now — not a restatement of the intent.
- **Fail closed, loudly.** Intervention latency is hours. A system that stops
  and waits costs you an afternoon; a system that guesses and continues costs
  you the afternoon plus everything it did in the meantime.
- **Append-only.** Never mutate history you might need to read on a small screen
  at 2am.
- **One thing in flight.** Always.

| Phase | Phone-viable? |
| --- | --- |
| 0 — harness and fixtures | Yes |
| 1 — planner conformance | Yes |
| 2 — executor conformance | Yes |
| 3 — single-hop loop | The approval half, yes |
| 4+ — multi-step and beyond | Wants a keyboard |

---

## Ledger and fixture layout

```
.vassal/ledger/YYYY-MM-DD.jsonl     committed — audit trail and eval set
tests/fixtures/orders/*.json        golden orders, hand-written
tests/fixtures/reports/*.json       canned executor replies (Phase 0 stubs)
tests/fixtures/malformed/*.json     must be rejected with specific errors
```

The ledger is committed deliberately. It is simultaneously the audit trail, the
input to every metric above, and the evaluation set for later phases. Treat it
as data you are collecting, not as logs you are emitting.

## Reason codes are never optional

Every human decision carries a `DecisionReason`. Every escalation carries an
`EscalationReason`. Neither is free text and neither has a default.

This is not bureaucracy — it is the labelling step. An approval without a reason
code is a lost data point, and labelled human judgement is the scarcest thing
this project produces. Without reason codes, EP and ER are uncomputable and the
ledger degrades from an eval set into a log file. With them, "why did you reject
this?" is a `GROUP BY` rather than an archaeology exercise.

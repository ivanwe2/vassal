# Current plan

What to build next, and in what order. Rewritten as each phase opens. The phase
ladder and its exit criteria live in `docs/TESTING.md`; this file is the working
sequence, with the reasoning for the ordering attached.

Two parts, and the second is gated on the first:

- **Part A — close Phase 0.** PR #1 is not mergeable yet. Six blocking items.
- **Part B — Phase 1, planner conformance.** Do not start until Part A lands.

The gate between them is not ceremony. Phase 1 measures whether the planner
speaks the protocol, and every one of those measurements is computed from the
ledger. Part A item 3 is "nothing writes the ledger." Starting Phase 1 first
means measuring OVR against a file the system does not produce, and discovering
that at the point where you have already spent thirty planner calls.

---

## Part A — close Phase 0

PR #1 (`feat/phase-0-harness`) implements the transport well. A real concurrent
probe — four writer processes, 250 orders each, one reader parsing every file it
saw — delivered 1000/1000 with zero torn reads and zero stray tmp files. The
atomic write pattern is correct and the malformed-fixture error messages are
specific in exactly the way the gate asks for.

The problem is the distance between what the test suite asserts and what the
exit criteria ask. That gap is the failure mode Phase 0 exists to catch, so it
gets closed here rather than inherited at Phase 3.

### Blocking

1. **Make `uv run ruff check .` pass.** 32 errors. `--fix` clears 23; the rest
   are 4×`B904`, 2×`E501`, 1×`SIM105`, 1×`B007`, 1×`RUF059`.

2. **Make the concurrency run concurrent.** The current test is a single-threaded
   `for` loop asserting a counter. It cannot produce a torn read, a lost message,
   or a corrupted file, which are the three things the criterion names. Replace
   with multiple writer processes against one inbox and a reader parsing
   continuously; assert zero parse failures, zero losses, and zero stray `.tmp`
   files. The code passes this today — the test is what is missing.

3. **Implement Rule 4.** `append_to_ledger` has no call sites in `src/`. Every
   produce and consume must commit through it: order produced, report produced,
   decision produced, escalation produced, message consumed. Then delete
   `test_ledger_replay`'s hand-fed entry and reconstruct state from a ledger the
   system actually wrote — that is what the criterion means by *from disk alone*.

4. **Implement Rule 5.** `is_halted()` is called once, in `acquire_lock`. It
   belongs at the top of every produce, consume, and ledger append. "Before every
   action" is the whole mechanism; a kill switch checked in one code path stops
   one code path.

5. **Fix `produce_report`'s `plan_id`.** It is currently set to the report's
   `order_id`. It validates because both are ULIDs, which is why it will not
   surface on its own — it surfaces as every plan-grouped metric being wrong.
   `Report` carries no `plan_id`, so thread it through from the order rather than
   substituting a value that happens to typecheck.

6. **Stop `clear()` deleting the ledger.** It calls `shutil.rmtree` over
   `ledger/`. Rule 2 is move-never-delete, and the ledger is the audit trail, the
   metric source, and the eval set at once. Confine the helper to `inbox/`,
   `outbox/`, `state/`, and `archive/`, or move it into the test suite where a
   non-test caller cannot reach it.

### Also, before merge

7. **The dead `except FileExistsError` in `_write_atomic`.** Flags are
   `O_WRONLY | O_CREAT | O_TRUNC`; without `O_EXCL` the branch is unreachable,
   and its comment names a Rule 3 guard that does not exist. Add the flag or drop
   the branch — a comment describing a guard that is not there is worse than no
   comment, because the next reader budgets for protection they do not have.

8. **Drop the `ulid` dependency.** Version 1.1 was published in 2016, is
   sdist-only and unmaintained, and draws 512 bytes of `os.urandom` per ID. It is
   not in STACK.md's table. Generating a ULID against the Crockford alphabet
   already pinned in `models.ULID` is about twelve lines of stdlib.

9. **Two tests that do not test.** `test_write_atomic_partial_write_protected` is
   a bare `pass`. `test_release_without_acquire_raises` is named for a raise its
   own docstring says does not happen. Both count toward the reported total.

10. **Assert on the malformed-fixture messages, not just the type.** The gate is
    about the message — `"envelope.corr_id: string does not match ULID pattern"`
    passes, a stack trace fails. `pytest.raises(HarnessCorrupt)` with no `match=`
    would stay green if the messages were collapsed to `"bad file"`.

11. **Write the deterministic stubs.** Phase 0 is defined as both models replaced
    by stubs that read fixtures and return canned messages. The fixtures landed;
    the stubs did not, so nothing drives the harness end to end. This is also
    what blocks the kill-switch criterion: *"`state/halt` appears, **the run
    stops at the next check**"* needs a run. A stub loop that polls the inbox at
    500ms, consumes an order, returns the canned report, and checks `halt` each
    pass is enough — and it makes item 2's concurrency test easier to write, not
    harder.

12. **Move fixtures to the specified layout** — `tests/fixtures/orders/`,
    `reports/`, `malformed/`.

13. **Update the two "no transport yet" notes** in `CLAUDE.md` and `docs/TESTING.md`
    line 88, which go stale on merge.

**Part A is done when:** `uv run pytest`, `uv run ruff check .`, and
`uv run vassal schema emit --check` all exit 0, and each of the four Phase 0 exit
criteria is evidenced by a test that would fail if the behaviour regressed.

---

## Part B — Phase 1, planner conformance

**Claude alone. No executor, no execution, no verification.** Orders are
generated and inspected. Nothing runs. The exit criteria are OVR ≥ 95%, ≥ 90% of
verifications sound on hand review, **zero** under-tiered orders, `--resume`
carrying context across 5+ calls, and a baseline CCL.

Keep this in view throughout, from `docs/TESTING.md`: **most Phase 1 failures are
schema design problems wearing a model-behaviour costume.** When the planner
keeps emitting something that fails validation, the first hypothesis is that the
schema makes the right thing awkward to express — not that the prompt needs to be
firmer. Prompt-tuning a schema bug is the most effective way to lose a week here.

### B1. Answer the `$ref` question first

STACK.md open question 1: does `claude -p --json-schema` accept `$defs`/`$ref`?
`order.schema.json` currently carries ten of them.

This goes first because it is the highest-risk unknown and the cheapest to
resolve — one call with a trivial prompt tells you. If refs are rejected, the fix
is **a dereferencing step in `schemas.py`**, never a hand-edited schema. Doing
this before the adapter means a rejection costs one call; doing it after means
debugging it through a subprocess wrapper and a session file.

Record the answer in STACK.md under the open question either way.

### B2. The planner adapter — `src/vassal/planner.py`

One module, one interface: takes a prompt, returns a validated `Order`.
Everything CLI-specific stays on its far side, so the Agent SDK remains a
swap of one file later.

Non-negotiable details, each of which costs a day if missed:

- **Explicit timeout on the subprocess.** Every call, no exceptions. A hung
  `claude` is indistinguishable from a slow one.
- **Do not pass `--bare`** — it skips OAuth/keychain resolution and demands
  `ANTHROPIC_API_KEY`, defeating subscription auth.
- **Read `structured_output`, not `result`.** With a schema supplied, `result`
  holds prose or nothing.
- **Capture `total_cost_usd` into the ledger on every call.** It is only
  available at call time; CCL cannot be reconstructed afterwards.
- **If `--resume` fails, HALT.** Never silently open a fresh session. A planner
  without history re-issues completed work.
- **Log the exact command string** into the ledger. The reason the CLI was chosen
  over the SDK is that you can replay it by hand from a phone.
- `--allowedTools "Read,Grep,Glob"`. The planner plans; it does not execute.

### B3. The compiler gate

Thin: parse the structured output, `Order.model_validate` it, and ledger the
outcome with the rejection reason when it fails. Both enforcement layers are
already in `models.py` — this only records which one rejected and why, because
that record *is* OVR.

Expect rejections at a steady rate and do not engineer them away. A well-formed
order that under-tiers its own actions is what a capable model produces when it
is slightly wrong; catching it is the point.

### B4. Session continuity

`state/session` holds the `--resume` id per PROTOCOL.md. The exit criterion is
that context demonstrably survives 5+ calls, so test it directly: reference
something established in call 1 from call 5 and confirm the planner has it.
Verify the halt-on-resume-failure path here too, by corrupting the session id on
purpose — that path is worth more than the happy one.

### B5. The eval prompt set

Thirty prompts, ten per band, committed:

| Band | Shape | What it probes |
| --- | --- | --- |
| 1 | Single obvious step | Baseline protocol conformance |
| 2 | Multi-step with an ordering constraint | Does `seq` mean anything to it? |
| 3 | Underspecified | **Does it ask or halt rather than guess?** |

Band 3 is the interesting one and should not be softened. The correct response to
an underspecified prompt is often to escalate, and a planner that always produces
a confident order is failing in a way OVR alone will score as success.

### B6. Metrics, computed from the ledger

OVR and CCL, read from ledger files — never hand-tallied. `Order.seq` exists so
that later phases can plot FPER by step index from the same source; keep the
reader general enough that Phase 4 does not rewrite it.

If a number cannot be produced from committed ledger files, it is not a metric.

### B7. Run and hand-review 100%

Structural validity is already machine-checked, so review is not for that. The
question for every order is the one thing a machine cannot answer:

> Would this verification command actually detect failure of *this* step?

A verification that passes regardless of the order's outcome is worse than none,
because it manufactures confidence. If `detects` cannot be filled in concretely,
the verification is decorative — record it as unsound and move on.

### Out of scope for Phase 1

No executor, no tool binding, no GBNF or xgrammar, no verification subprocess, no
retry logic, no approval flow. Every one of those belongs to a later phase and
each would add a second source of nondeterminism to a phase whose entire purpose
is having exactly one.

The serving-stack decision (STACK.md open question 2) stays deferred to Phase 2
deliberately: Phase 1 needs no executor, so postponing costs nothing and the
decision benefits from Phase 1's findings about how large orders actually get.

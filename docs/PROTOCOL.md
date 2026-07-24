# Protocol

How the planner, compiler, executor, and human exchange messages.

## Files on disk are the message bus

This is a deliberate choice, and it is the choice most likely to look naive to
someone reading the repo for the first time. The reasoning:

- **Inspectable.** Every message is a file you can `cat`.
- **Git-versionable.** The ledger is committed, so history is diffable.
- **A free audit log.** The transport *is* the record; there is no separate
  logging layer that can disagree with what actually happened.
- **Debuggable over SSH from a phone.** This is not a joke constraint. It is the
  supervision model described in `docs/TESTING.md`.

A socket would be faster and would tell you nothing at 2am. **Speed is not the
bottleneck; attributability is.** You are waiting on a 70B model — the transport
is not what makes this slow.

The tradeoff is real and must be stated plainly: **a filesystem gives you no
transactions.** There is no atomic multi-file commit, no rollback, no isolation.
Everything below compensates for that, which is why the five rules are mandatory
rather than advisory.

## Directory layout

```
.vassal/
├── inbox/
│   ├── executor/          orders awaiting execution
│   ├── planner/           reports and decisions awaiting the planner
│   └── human/             escalations awaiting a decision
├── outbox/
│   ├── executor/          reports written, not yet consumed
│   └── planner/           orders written, not yet consumed
├── state/
│   ├── lock               O_EXCL; one order in flight
│   ├── halt               existence = stop. The kill switch.
│   └── session            claude --resume session id
├── ledger/
│   └── YYYY-MM-DD.jsonl   append-only. COMMITTED.
└── archive/
    ├── orders/            consumed orders
    └── reports/           consumed reports
```

`state/` and `inbox/` are gitignored — they are runtime scratch. **The ledger is
committed.** It is the audit trail, the metric source, and the eval set.

## The envelope

One JSON object per file, named `<ulid>.json`.

| Field | Meaning |
| --- | --- |
| `v` | Envelope version. Currently `1`. |
| `id` | ULID for this message. |
| `type` | One of the message types below. |
| `ts` | RFC 3339 UTC, millisecond precision. |
| `from` | `planner` / `compiler` / `executor` / `human`. |
| `to` | Same set. |
| `plan_id` | The plan this belongs to. |
| `corr_id` | The id this responds to. **Reports must set it.** |
| `payload` | The typed body — an `Order`, a `Report`, a `Decision`. |

ULIDs are load-bearing, not decorative. They **sort lexicographically by
creation time**, which means a plain directory listing is an ordered queue. No
index, no sequence file, no sorting logic that can disagree with reality. `ls`
is the queue.

| Type | From → To | Payload |
| --- | --- | --- |
| `ORDER` | planner → executor | `Order` |
| `REPORT` | executor → planner | `Report` |
| `ESCALATION` | executor/compiler → human | context for a decision |
| `DECISION` | human → planner | `Decision` |
| `ACK` | any | receipt confirmation |
| `HALT` | any → all | stop now |

## The five rules

Each rule exists because of a specific way a filesystem transport corrupts
itself. The consequence column is why the rule is not negotiable.

### 1. Atomic writes only

Write to `tmp/`, `fsync`, then `rename` into place. **Never write into an inbox
directly.**

*Consequence if broken:* a reader picks up a half-written file. Because it is
valid JSON right up until it isn't, this presents as an inexplicable schema
error on a message that looks fine by the time you go to inspect it. `rename`
within a filesystem is atomic; a partial `write` is visible to readers.

### 2. Move, never delete

Consuming a message means renaming it into `archive/`. Nothing is ever unlinked.

*Consequence if broken:* you lose the ability to reconstruct what happened, and
"the message was never delivered" becomes indistinguishable from "the message
was delivered and mishandled." Those two failures have opposite fixes.

### 3. One order in flight

Acquire `state/lock` with `O_EXCL`. **An existing lock is a hard error, not a
wait.**

*Consequence if broken:* two orders execute concurrently against one worktree
and the diff is a blend of both. Waiting on the lock is worse than failing on it
here, because a wait hides the concurrency bug that produced the contention —
you want it loud the first time, not intermittently at Phase 4.

### 4. Ledger append is the commit point

A step has happened when it is in the ledger. Not when the file was written, not
when the model returned.

*Consequence if broken:* replay reconstructs a different state than the one that
actually occurred, and every metric computed from the ledger is quietly wrong.

### 5. Check `state/halt` before every action

The kill switch is a file-existence check — **precisely so that it works when
nothing else does.** No IPC, no signal handler, no API call. `touch
.vassal/state/halt` from any SSH session stops the system at its next check.

*Consequence if broken:* there is no way to stop a running system from a phone,
which means autonomy can never be enabled at all.

## Concurrency: use a worktree

```sh
git worktree add ../vassal-exec exec/main
```

The executor works in `../vassal-exec`. Planning and review happen on `main`.
Both share one `.vassal/` directory.

The point is that this **converts silent corruption into ordinary merge
conflicts.** Two processes editing one tree produce a blended diff nobody can
attribute. Two branches editing separately produce a conflict git will refuse to
resolve for you. The second failure is annoying; the first is undebuggable.

**The human is a third writer.** This is the part that gets forgotten. If you
open an editor and change a file during a run, you are a concurrent writer with
no lock and no audit trail. Either acquire the lock or do not touch the tree.
Before hand-editing during a run, **send `HALT` first** — a stopped system that
you then edit is a clean state; a running system that you edit underneath is the
git-conflict fault from Phase 5, self-inflicted.

## Tiers

| Tier | Name | Examples | Policy |
| --- | --- | --- | --- |
| 0 | Read-only | `read_file`, `grep`, `glob` | Auto-approve once Phase 6 opens |
| 1 | Scoped write | `write_file`, `edit_file`, `create_file` | Auto only within budget, in scratch repos |
| 2 | Destructive | `run_command`, deletes, history rewrites, anything network | **Permanently human-gated** |

Two rules attach to this table:

- **Anything unclassifiable is tier 2 by default.** Fail closed. If you cannot
  confidently say what an action's blast radius is, you have already established
  that you cannot review it as anything less.
- **The compiler may only lower a planner's tier, never raise it past review.**
  Lowering happens against a code allowlist and is safe. Raising a tier *past*
  the review threshold would let the system quietly promote its own permissions,
  which is precisely the thing the tier system exists to prevent.

## Planner invocation

```sh
claude -p "$PROMPT" \
  --output-format json \
  --json-schema schema/order.schema.json \
  --append-system-prompt-file prompts/compiler.md \
  --resume "$SESSION_ID" \
  --allowedTools "Read,Grep,Glob"
```

`--allowedTools` is restricted to read-only tools on purpose: **the planner
plans, it does not execute.** Execution is the executor's job, under the tier
system, with a verification attached. A planner that can write is a planner that
can bypass every gate in this document.

Details that cost time if you get them wrong:

- **Do not pass `--bare`.** It skips OAuth/keychain resolution and requires
  `ANTHROPIC_API_KEY`, defeating subscription auth.
- **Structured output lands in `structured_output`, not `result`.** Reading
  `result` when a schema was supplied gets you prose, or nothing.
- **Capture `total_cost_usd` into the ledger on every call.** CCL cannot be
  reconstructed later — that field is only available at call time.
- **If `--resume` fails, HALT.** Do not start a fresh session. A planner without
  history re-issues completed work, and re-issuing a tier-1 order is how a
  double-applied edit happens.

## Executor invocation

- **Constrained decoding is mandatory** — GBNF via llama.cpp, or xgrammar via
  vLLM. Schema conformance is enforced at the sampler, not requested in a
  prompt. Record whether it was active in `executor.grammar_enforced`; a run
  without it does not count for Phase 2 measurement.
- **The tool binding is the security boundary.** Tier enforcement lives in code
  that binds tools, not in the system prompt. A prompt is not an access control
  mechanism, and `prompts/executor.md` should be read as documentation of
  intended behaviour rather than as an enforcement layer.
- **Verification runs in a subprocess the model does not control** — separate
  process, explicit timeout, exit code read directly. This is the mechanism
  behind "the executor does not get a vote."
- **Timeouts must still produce well-formed reports.** A timeout is a legitimate
  outcome with a status of its own; it is not permission to emit nothing.

## Migration

The envelope is transport-agnostic by design: `id`, `type`, `from`, `to`,
`corr_id`, and `payload` describe a message, not a file. Moving to a socket, a
queue, or a database is a change of writer and reader, not of contract.

**Swap the transport after Phase 5, not before.** Until the failure modes are
known and injected, debuggability beats throughput — and the filesystem's single
real advantage is that when something goes wrong at 2am, the entire state of the
system is a directory you can read.

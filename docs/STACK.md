# Stack

Decisions, with the reasoning attached. A decision without its reasoning cannot
be revisited honestly later — you end up either defending it out of habit or
reversing it without knowing what it was buying.

## Python 3.12, despite the obvious objection

The objection is fair: this is a **process supervisor**. It shells out, watches
files, enforces timeouts, and moves JSON around. Go or Rust are the natural
choices for that, and both would give a single static binary with no environment
to manage.

The reason it is Python anyway is that **the executor half is unavoidably
Python-adjacent.** Grammar tooling, tokenizers, schema→GBNF conversion, and
every serving stack worth using are Python-first. A Go core does not avoid that
work — it defers it. Within a week you have a Python sidecar, and now there is
an FFI or IPC boundary sitting in the middle of the thing you are trying to
debug from a phone. The boundary buys nothing and costs attributability, which
is the one currency this project is spending.

The usual counterargument — performance — does not apply. **Nothing here is
CPU-bound.** You are waiting on a 70B model. Every millisecond the supervisor
spends is invisible next to the seconds the executor takes.

## Layers

| Concern | Choice | Why this one |
| --- | --- | --- |
| Packaging / env | **uv** | Fast, lockfile-native, one tool for venv + deps + scripts |
| Contracts | **pydantic v2** | Models, JSON Schema emission, and runtime validation from one definition |
| CLI | **typer** | Type hints become the interface; no argparse boilerplate |
| HTTP | **httpx** | Timeouts that actually work; sync API available |
| Inbox watching | **500ms polling** | See below |
| Subprocesses | **stdlib `subprocess`**, explicit timeout | No dependency needed; timeout on *every* call |
| Ledger | **`json` + `O_APPEND`** | Append-only JSONL. No database to corrupt or migrate |
| Tests | **pytest, hypothesis, pytest-timeout** | Property tests for validators; hard timeout so no test can hang a phone-driven run |
| Lint / format | **ruff** | One tool, fast; line length 96 |
| Planner | **`claude` CLI via subprocess** | See "CLI over Agent SDK" |
| Executor | **llama.cpp + GBNF**, or **vLLM + xgrammar** | Constrained decoding is mandatory; pick at Phase 2 |

## Pydantic as the single source of truth

```
                    ┌──────────────────────────────────────────┐
                    │        src/vassal/models.py              │
                    │        (the only definition)             │
                    └────────────────────┬─────────────────────┘
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              ▼                          ▼                          ▼
    schema/order.schema.json   schema/report.schema.json   runtime validation
              │                          │                  (compiler gate)
              ▼                          ▼
   claude -p --json-schema        grammar/*.gbnf
      (planner output)                   │
                                         ▼
                              executor sampler constraint
```

One definition, three consumers: the planner's output schema, the executor's
sampler grammar, and runtime validation in the compiler gate.

The reason this matters more than it looks: **a schema mismatch between planner
and executor is a bug that presents as model failure.** The planner emits a
field the executor's grammar does not accept; the executor produces garbage; you
conclude the local model is too small for the task and spend days on prompts and
quantisation settings. The actual fix was regenerating a file. Generating both
artifacts from one definition makes that class of bug impossible rather than
merely unlikely, and `vassal schema emit --check` in CI keeps it that way.

### Two enforcement layers

They are separated deliberately, and the separation is the interesting part of
the design.

**The schema layer is structural.** It is emitted to JSON Schema and compiled to
GBNF. It catches malformed output *before it exists* — a sampler constrained by
a grammar cannot emit an invalid field name.

**The validator layer is semantic.** These are the `model_validator` functions
in `models.py`, and they deliberately **do not appear in the emitted schema**,
because they encode rules a sampler cannot enforce: that a tier covers the
actions beneath it, that a destructive order does not retry, that `passed` is
null exactly when `ran` is false, that success requires a passing verification.

The consequence to internalise:

> **A message that passes the schema layer and fails the validator layer is the
> normal, expected case for a coherent-looking but wrong plan.**

This is not an error condition to be engineered away. A well-formed order that
under-tiers its own actions is exactly what a capable model produces when it is
slightly wrong, and catching it is the compiler earning its keep. Expect these
rejections at a steady rate forever; their disappearance would be more worrying
than their presence.

## Rejected: hand-written JSON Schema

Considered, because a hand-written schema can be tuned to whatever the consumer
accepts. Rejected because it drifts. Two definitions of one contract diverge the
first week someone is in a hurry, and the divergence surfaces as model failure
(above), not as a schema error.

**One open item follows from this choice.** The generated schemas use `$defs`
and `$ref` — `order.schema.json` currently contains ten `$ref`s. **Verify in
Phase 1 that `claude -p --json-schema` accepts refs.** If it does not, the fix
is to **add a dereferencing/inlining step to the emitter** in `schemas.py` — not
to revert to hand-editing. Hand-editing solves a day's problem and reintroduces
the drift permanently.

## CLI over Agent SDK, for now

The `claude` CLI is invoked as a subprocess rather than through the Agent SDK.
The reason is the subprocess boundary itself: **you can log the exact command
and replay it by hand.** From a phone, over SSH, at 2am, with no Python
involved. When a plan goes wrong, "here is the exact string that produced this"
is worth more than any amount of ergonomics.

The SDK is the upgrade path once the loop is stable and the failure modes are
known. To keep that cheap, **the planner adapter stays behind one interface** —
one module that takes a prompt and returns a validated `Order`, with everything
CLI-specific on its far side.

## Synchronous and single-threaded

One order is in flight at a time, by protocol rule. That makes asyncio pointless
— there is nothing to overlap — and it costs readable stack traces, which are
the primary debugging tool for a system supervised remotely. A traceback that
reads top to bottom is worth more here than concurrency the design forbids
anyway.

## Polling over inotify

500ms polling. inotify is the technically correct answer and is rejected on
operational grounds: **it drops events silently on network filesystems.** A
missed event presents as a hung system with no error anywhere, which is
miserable to diagnose remotely and indistinguishable from a slow model. Polling
cannot silently miss anything; it can only be slightly late, and half a second
is nothing next to a 70B inference.

## No agent framework

**The control flow is the product.** The ordering of plan → compile → approve →
execute → verify → ledger, and the decision of what happens at each failure
point, is the entire contribution of this project. Delegating it to a framework
means the interesting logic lives in someone else's abstraction, and when a step
misfires the question "why did it do that?" has to be answered by reading their
source.

Framework indirection is the enemy of attributability, and attributability is
what `docs/TESTING.md` spends seven phases protecting.

## No Docker initially

A container is a layer you cannot peel back from a phone. Adding one before the
failure modes are understood means every mystery gets a second hypothesis — "is
it the container?" — that has to be eliminated before the real debugging starts.
Revisit at Phase 6, where isolation starts paying for itself.

## Repo layout

```
src/vassal/models.py     contracts — the single source of truth
src/vassal/schemas.py    emits schema/*.json from the models
src/vassal/cli.py        the vassal command; Phase 0 surface only
schema/                  GENERATED, committed, never hand-edited
grammar/                 GENERATED, gitignored (regenerable, large)
tests/                   contract tests
docs/                    this file, PROTOCOL.md, TESTING.md
prompts/                 runtime system prompts for planner and executor
```

`schema/` is committed and `grammar/` is not: the schema is small and its diff
is the contract change, which is exactly what a reviewer needs to see. Grammars
are large, mechanical, and reviewable only via the schema they came from.

## Commands

```sh
uv sync --extra dev
uv run vassal schema emit
uv run vassal schema emit --check
uv run pytest
uv run ruff check .
```

## Open questions

1. **Does `claude -p --json-schema` accept `$defs`/`$ref`?** Answer in Phase 1.
   If not, add dereferencing to the emitter — never hand-edit the schema.
2. **Which serving stack — llama.cpp + GBNF, or vLLM + xgrammar?** Decide at
   Phase 2. Deliberately deferred: Phase 1 needs no executor at all, so this
   decision costs nothing to postpone and benefits from Phase 1's findings about
   how large orders actually get.
3. **Does subscription-backed programmatic use stay viable at Phase 7 volumes?**
   `claude -p` draws on the same limits as interactive use, and a soak test is a
   different volume profile than a person typing. The API key is the fallback;
   the CCL metric is what tells you when to switch.
4. **Ledger rotation.** Daily files are fine for now. At Phase 7 volumes, decide
   whether to compact, and note that the ledger is an eval set — compaction that
   loses labelled decisions is destroying training data, not saving disk.

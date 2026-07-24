# Vassal

A plan compiler that lets a frontier model command a local agent.

A frontier model (Claude, via `claude -p`) acts as the **planner**: it issues
structured, one-at-a-time *orders*. A small local model (Hermes) acts as the
**executor**: it performs exactly one order and reports on it. Between them sits
the **compiler** — a validation gate that rejects orders which are structurally
valid but semantically wrong.

Human approval is a policy toggle, not an architectural requirement. The system
is designed so that removing the human changes *when* review happens, not
*whether* the loop is sound.

## The rule everything else follows from

> **The executor does not get a vote on whether it succeeded.**

Every order carries a machine-checkable verification command, written by the
planner and run by the harness in a subprocess that neither model controls.
Status is *derived* from an exit code. It is never asserted by a model.

This is enforced in the type system, not in a prompt. `Report` will not
construct with `status=success` unless `verification.passed is True`:

```python
Report(status=ReportStatus.SUCCESS, verification=VerificationResult(ran=False), ...)
# ValidationError: status=success requires verification.passed=true.
# The executor does not get a vote on whether it succeeded.
```

A closely related rule has its own validator: `ran` and `passed` are separate
fields, and `passed` is `null` whenever `ran` is false. A verification that
*could not execute* is not a verification that *failed*. Collapsing those two
states turns "I don't know" into "it's fine" at random, and is the single most
dangerous bug available to this design.

## Status: Phase 0

**Contracts and tests only. Nothing executes end to end yet.**

Phase 0 deliberately contains no planner, no executor, and no transport — only
the contracts they will have to satisfy and the tests that pin them down. The
phased plan and its promotion gates are in [`docs/TESTING.md`](docs/TESTING.md).
The short version of why it is this strict: once planner, compiler, and executor
are all in the loop at once, a failure is no longer *attributable*. Every phase
exists to keep failures attributable.

## Layout

| Path | What it is |
| --- | --- |
| `src/vassal/models.py` | The contracts. Single source of truth. |
| `src/vassal/schemas.py` | Emits `schema/*.json` from the models. |
| `src/vassal/cli.py` | The `vassal` command. Phase 0 surface only. |
| `schema/` | **Generated.** Committed for reviewable diffs; never hand-edited. |
| `tests/` | Contract tests. Each names the failure mode it prevents. |
| `docs/TESTING.md` | Phase ladder, metrics, promotion gates, field mode. |
| `docs/PROTOCOL.md` | Message bus, envelope, the five transport rules. |
| `docs/STACK.md` | Technology decisions, with the reasoning attached. |
| `prompts/compiler.md` | Runtime system prompt for the planner. |
| `prompts/executor.md` | Runtime system prompt for the executor. |
| `CLAUDE.md` | Instructions for agents working *on* this repo. |

## Commands

```sh
uv sync --extra dev              # install, including dev tooling
uv run vassal schema emit        # regenerate schema/*.json from the models
uv run vassal schema emit --check  # fail if the committed schemas have drifted
uv run pytest                    # contract tests
uv run ruff check .              # lint
```

## Design notes worth knowing before reading the code

**Two layers of enforcement, deliberately separated.** The *schema* layer is
structural: emitted to JSON Schema, handed to `claude -p --json-schema`, and
converted to GBNF to constrain the executor at the sampler. The *validator*
layer is semantic — the `model_validator` functions — and does not appear in the
emitted schema, because it encodes rules a sampler cannot enforce. A message
that passes the schema layer and fails the validator layer is the normal,
expected case for a coherent-looking but wrong plan. That is the compiler
earning its keep.

**Tiers describe blast radius**, and an order takes the highest tier any of its
actions reaches. Under-tiering is a correctness failure, not a style issue: it
is the bug that reaches production as an unreviewed destructive action. Tier 2
is permanently human-gated — a design decision, not a phase that gets retired.

**Files on disk are the message bus.** This is a deliberate choice in favour of
inspectability over throughput; the reasoning is in `docs/PROTOCOL.md`.

## License

TBD.

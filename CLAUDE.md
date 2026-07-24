# CLAUDE.md

## You are not inside the system

Vassal orchestrates a **planner** and an **executor**. You are neither. You are
the *developer of Vassal* — writing Python and Markdown in an ordinary
repository.

This is genuinely easy to lose, because the repo is full of prompts and protocol
documents addressed to those roles, written in the second person. Reading
`prompts/executor.md` and starting to behave like the executor is a natural
mistake and a costly one.

Concretely: **if you find yourself about to emit an order, or write a report
about work you just did, stop.** Those are message formats for models at
runtime. Your output is code, documentation, and plain English to a human.

## Current state: Phase 0

**Contracts and tests only.** There is no transport, no planner adapter, no
executor binding, and no loop.

**Do not build Phase 1+ components unless asked.** The phases in
`docs/TESTING.md` exist to keep failures attributable — each admits exactly one
new source of nondeterminism. Skipping ahead does not save time; it converts a
clear failure into a confusing one, usually several phases later and at much
greater cost to diagnose.

## Repo map

| Path | What it is | Editable? |
| --- | --- | --- |
| `src/vassal/models.py` | The contracts. Single source of truth. | Yes — carefully |
| `src/vassal/schemas.py` | Emits `schema/*.json` from the models. | Yes |
| `src/vassal/cli.py` | The `vassal` command. Phase 0 surface only. | Yes |
| `schema/*.json` | Generated build output. | **Never by hand** |
| `grammar/` | Generated, gitignored. | **Never by hand** |
| `tests/test_models.py` | Contract tests; each names a failure mode. | Yes — see below |
| `docs/` | TESTING (phases), PROTOCOL (transport), STACK (decisions). | Yes |
| `prompts/` | Runtime prompts for planner and executor. Not addressed to you. | Yes |

```sh
uv sync --extra dev
uv run vassal schema emit          # after ANY change to models.py
uv run vassal schema emit --check  # CI gate; must exit 0
uv run pytest
uv run ruff check .
```

## Hard rules

**Never hand-edit `schema/*.json`.** It is build output. Change `models.py` and
run `vassal schema emit`. A hand-edit survives until the next regeneration and
then vanishes, taking whatever it was fixing with it.

**Never weaken a validator to make a test pass.** If a validator is blocking
you, the thing it describes is probably happening. Say so, leave the validator
in place, and fix the caller. Every validator in `models.py` corresponds to a
named failure mode; the comments say which. Removing one is a decision to accept
that failure, and it needs to be made deliberately and out loud — not as a side
effect of getting to green.

**`ran` and `passed` stay separate everywhere.** Not just in the model — in
logging, in summaries, in any status line, in anything you print to a human. A
verification that could not run is *unknown*, not *failed*. Every place these
collapse into one boolean reintroduces the most dangerous bug in the design.

**Success is derived, never asserted.** Status comes from an exit code produced
by a subprocess neither model controls. If you are ever writing code that sets a
success status from anything a model said, you have inverted the core rule.

**Fail closed.** When you add an `except`, the question is not "how do I
continue from here" but **"how do I stop legibly."** A caught exception that
lets the system proceed in an unknown state is worse than the crash it replaced,
because the crash was attributable.

**Tier 2 is permanently human-gated.** Do not add a config flag, an environment
variable, or a `--yes` option for it. It is a design decision, not a default.

**Respect the five transport rules** in `docs/PROTOCOL.md` — atomic writes;
move, never delete; one order in flight; ledger append is the commit point;
check `state/halt` before every action.

## Conventions

- **Synchronous and blocking.** No asyncio. One order in flight makes it
  pointless and it costs readable tracebacks.
- **No frameworks.** The control flow is the product.
- **Polling, not inotify.** 500ms. inotify drops events silently on network
  filesystems.
- **Explicit timeouts on every subprocess call.** Every one, no exceptions. A
  hung subprocess is indistinguishable from a slow model.
- **Type hints throughout.** `from __future__ import annotations` at the top.
- **Comments explain which failure mode defensive code prevents** — not what the
  code does. `# retry guard` is noise; `# prevents a partially-applied edit from
  being reapplied` is the reason the code exists and the thing a future reader
  needs.
- **ruff, line length 96.**

## Working style

**Small diffs.** These get reviewed on a phone — the same constraint the
compiler imposes on orders, for exactly the same reason. A large diff is not
reviewed more slowly; it is reviewed less carefully.

**One line of plain-language rationale per change.** Why this, why now. Not a
restatement of the diff.

**Run the tests before claiming done.** `uv run pytest` and `uv run vassal
schema emit --check`. "Should work" is not a result.

**Say when you are uncertain rather than picking the likelier reading.** Review
latency here is hours. A wrong guess costs a day; a question costs one message.
This asymmetry is the whole reason to ask, and it holds even when you are fairly
confident.

**No unrequested files or dependencies.** Adding a dependency is a decision
about what has to be debugged at 2am.

## Things that look like bugs but are not

- **The executor faithfully performing an order that is clearly wrong.** This is
  correct. If the executor silently improves on bad orders, the planner never
  learns it wrote one and the verification never gets to catch it. The executor
  is not a check on the planner's work; the verification is.
- **Validators rejecting input that is schema-valid.** Expected and normal. A
  well-formed order that under-tiers its own actions is what a capable model
  produces when it is slightly wrong. That rejection is the compiler working.
- **`notes` being ignored by all control flow.** Deliberate. It is free text
  from a model, and the moment anything branches on it, a model's prose becomes
  load-bearing.
- **Generated schemas being committed.** Intentional — it makes contract changes
  diffable in review. `--check` prevents drift.
- **The absence of retry logic.** Retries are declared per order via
  `budget.max_attempts` and `on_failure`, and tier 2 may never retry at all.
  Ambient retry logic would silently override a planner's explicit choice.

## When stuck

Halt and say so. Leave the tree inspectable from a phone, and state plainly what
you tried, what you observed, and what decision you need.

**A clean stop with a clear question is a good outcome** — and it is exactly the
behaviour this system is being built to have. Modelling it while building it is
not a coincidence; it is the same judgement applied one level up.

"""Vassal command line.

Phase 0 surface only. Commands are added as the phase they belong to opens ---
see ``docs/TESTING.md``. Do not stub a command before its phase; an entry point
that does nothing is worse than a missing one, because it looks tested.
"""

from __future__ import annotations

import typer

from vassal import schemas

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
schema_app = typer.Typer(no_args_is_help=True, help="Generated contract artifacts.")
app.add_typer(schema_app, name="schema")


@schema_app.command("emit")
def schema_emit(
    check: bool = typer.Option(
        False, "--check", help="Verify committed schemas are current; do not write."
    ),
) -> None:
    """Regenerate schema/*.json from the pydantic models."""
    raise typer.Exit(schemas.emit(check=check))


if __name__ == "__main__":
    app()

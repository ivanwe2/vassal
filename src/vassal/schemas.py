"""Generate JSON Schema artifacts from the pydantic models.

``schema/*.json`` is build output. It is committed so the contract is diffable
in review, but it is never hand-edited --- run ``vassal schema emit`` instead.
CI checks that the committed files match what the models produce.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from vassal.models import Envelope, Order, Report

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"

TARGETS: dict[str, type] = {
    "order.schema.json": Order,
    "report.schema.json": Report,
    "envelope.schema.json": Envelope,
}

_BASE_ID = "https://github.com/OWNER/vassal/schema/"


def build(model: type, filename: str) -> dict[str, Any]:
    schema = model.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = _BASE_ID + filename
    return schema


def render(model: type, filename: str) -> str:
    return json.dumps(build(model, filename), indent=2, sort_keys=False) + "\n"


def emit(check: bool = False) -> int:
    """Write schemas to disk, or verify they are current.

    Returns a process exit code: 0 clean, 1 drifted.
    """
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    drifted: list[str] = []

    for filename, model in TARGETS.items():
        path = SCHEMA_DIR / filename
        current = render(model, filename)
        if check:
            existing = path.read_text() if path.exists() else ""
            if existing != current:
                drifted.append(filename)
        else:
            path.write_text(current)

    if check and drifted:
        print(
            "schema drift: " + ", ".join(drifted) + "\nrun: vassal schema emit",
            file=sys.stderr,
        )
        return 1
    if not check:
        print(f"wrote {len(TARGETS)} schemas to {SCHEMA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(emit(check="--check" in sys.argv))

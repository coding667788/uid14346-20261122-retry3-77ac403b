"""Command line entry point: ``python -m solution <command> CASE.json``.

All results are deterministic: keys are sorted, no wall-clock data appears in
the output and every collection is produced in a fixed order.
"""

from __future__ import annotations

import io
import json
import sys

from .checker import check as run_check
from .errors import CaseError
from .evaluator import evaluate as run_evaluate
from .model import load_case
from .planner import plan as run_plan

COMMANDS = ("plan", "check", "evaluate")


def _emit(payload: dict, exit_code: int) -> int:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in COMMANDS:
        return _emit(
            {
                "error_code": "BAD_USAGE",
                "field": "",
                "message": (
                    "usage: python -m solution {plan|check|evaluate} CASE.json"
                ),
            },
            2,
        )
    command, path = argv
    try:
        # utf-8-sig also accepts a BOM from Windows editors.
        with io.open(path, "r", encoding="utf-8-sig") as handle:
            doc = json.load(handle)
    except FileNotFoundError:
        return _emit(
            {
                "error_code": "FILE_NOT_FOUND",
                "field": "",
                "message": f"case file not found: {path}",
            },
            1,
        )
    except json.JSONDecodeError as exc:
        field = f"line {exc.lineno} column {exc.colno}"
        return _emit(
            {
                "error_code": "INVALID_JSON",
                "field": field,
                "message": exc.msg,
            },
            1,
        )

    try:
        case = load_case(doc)
        if command == "plan":
            payload = run_plan(case)
        elif command == "check":
            if not isinstance(doc, dict):
                raise CaseError("TYPE_MISMATCH", "case document must be an object")
            payload = run_check(case, doc)
        else:
            payload = run_evaluate(case)
    except CaseError as exc:
        return _emit(exc.to_jsonable(), 1)

    return _emit(payload, 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

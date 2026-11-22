"""Structured input errors with JSON-pointer-like field locations.

Every structural or semantic error in a CASE document is reported as a
``CaseError`` carrying a stable machine-readable code and the dotted field
path of the offending value.  The CLI serialises these as ordered UTF-8
JSON instead of a Python traceback.
"""

from __future__ import annotations


class CaseError(Exception):
    """Raised for malformed case documents or illegal runtime arguments."""

    def __init__(self, code: str, message: str, field: str = ""):
        self.code = code
        self.field = field
        super().__init__(message)

    def to_jsonable(self) -> dict:
        return {
            "error_code": self.code,
            "field": self.field,
            "message": str(self),
        }


def err_type(field: str, expected: str, got: object) -> CaseError:
    name = type(got).__name__
    return CaseError(
        "TYPE_MISMATCH",
        f"expected {expected} at {field or '<root>'}, got {name}",
        field,
    )


def err_missing(field: str) -> CaseError:
    return CaseError("MISSING_FIELD", f"missing required field '{field}'", field)


def err_dup(field: str, value: object) -> CaseError:
    return CaseError(
        "DUPLICATE_ID", f"duplicate value {value!r} at {field}", field
    )


def err_ref(field: str, value: object) -> CaseError:
    return CaseError(
        "UNKNOWN_REFERENCE",
        f"reference {value!r} at {field} does not resolve",
        field,
    )


def err_value(field: str, code: str, message: str) -> CaseError:
    return CaseError(code, message, field)

"""The svckit JSON contract between a controller and participant tools (see ``docs/contract.md``)."""

from __future__ import annotations

from cli_svckit.contract.envelope import (
    CONTRACT_VERSION,
    ERROR_CODES,
    EXIT_BUSY,
    EXIT_FAIL,
    EXIT_OK,
    EXIT_TEMPFAIL,
    EXIT_USAGE,
    EXIT_WARN,
    METHODS,
    Error,
    Op,
    Request,
    Response,
    SvcError,
    load_schema,
    matches,
    new_op_id,
    parse_stdout,
    redact,
)

__all__ = [
    "CONTRACT_VERSION",
    "ERROR_CODES",
    "EXIT_BUSY",
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_TEMPFAIL",
    "EXIT_USAGE",
    "EXIT_WARN",
    "METHODS",
    "Error",
    "Op",
    "Request",
    "Response",
    "SvcError",
    "load_schema",
    "matches",
    "new_op_id",
    "parse_stdout",
    "redact",
]

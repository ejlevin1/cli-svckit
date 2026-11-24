"""cli-svckit: toolkit for CLIs that ship local services."""

from __future__ import annotations

from cli_svckit.contract.client import CallResult, Participant, negotiate
from cli_svckit.contract.discovery import participants_from_registry
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
from cli_svckit.contract.server import dispatch, run
from cli_svckit.coord.controller import Operation
from cli_svckit.coord.lease import PHASES, Lease, LeaseDir
from cli_svckit.coord.participant import Interest, Quiescer, active_ops
from cli_svckit.coord.watch import LeaseWatcher
from cli_svckit.doctor import run_checks
from cli_svckit.health import HealthResult

__version__ = "0.1.0"

__all__ = [
    "CONTRACT_VERSION",
    "CallResult",
    "ERROR_CODES",
    "EXIT_BUSY",
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_TEMPFAIL",
    "EXIT_USAGE",
    "EXIT_WARN",
    "Error",
    "HealthResult",
    "Interest",
    "Lease",
    "LeaseDir",
    "LeaseWatcher",
    "METHODS",
    "Op",
    "Operation",
    "PHASES",
    "Participant",
    "Quiescer",
    "Request",
    "Response",
    "SvcError",
    "__version__",
    "active_ops",
    "dispatch",
    "load_schema",
    "matches",
    "negotiate",
    "new_op_id",
    "parse_stdout",
    "participants_from_registry",
    "redact",
    "run",
    "run_checks",
]

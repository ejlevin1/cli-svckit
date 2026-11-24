"""The svckit JSON contract: request/response envelopes, errors, exit codes and the operation record.

A controller runs ``<cmd> svc <method> --json`` with a :class:`Request` on stdin; the participant prints log lines
as it likes and finishes with one JSON :class:`Response` line on stdout (the last JSON line wins, see
:func:`parse_stdout`). Readers ignore fields they do not know (must-ignore), so minor versions only add optional
fields. ``busy`` is a normal result (``prepare`` returning ``state: "busy"``), never an error.
"""

from __future__ import annotations

import copy
import fnmatch
import importlib.resources
import json
import re
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

CONTRACT_VERSION = "1.0"
METHODS = ("describe", "health", "doctor", "install", "uninstall", "prepare", "complete")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_BUSY = 3
EXIT_WARN = 4
EXIT_TEMPFAIL = 75

ERROR_CODES = ("unsupported", "invalid", "forbidden", "requires_root", "timeout", "internal", "incompatible")

_SECRET_KEY = re.compile(r"(?i)token|secret|password|passwd|key|auth|cookie")
_SCHEMA_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class SvcError(Exception):
    """An error a participant (or the library) reports through the contract.

    ``code`` is one of :data:`ERROR_CODES`; ``retry_after_s`` tells the caller when a retry may succeed.
    """

    def __init__(
        self,
        code: str,
        message: str,
        data: dict[str, Any] | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.retry_after_s = retry_after_s

    def to_error(self) -> Error:
        """Return the wire :class:`Error` for this exception."""
        return Error(code=self.code, message=self.message, retry_after_s=self.retry_after_s, data=self.data)

    def __repr__(self) -> str:
        return f"SvcError({self.code!r}, {self.message!r})"


def _need_dict(d: Any, what: str) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise SvcError("invalid", f"{what} must be a JSON object")
    return d


def _need_str(d: dict[str, Any], key: str, what: str) -> str:
    value = d.get(key)
    if not isinstance(value, str):
        raise SvcError("invalid", f"{what}.{key} must be a string")
    return value


def _opt_dict(d: dict[str, Any], key: str, what: str) -> dict[str, Any] | None:
    value = d.get(key)
    if value is not None and not isinstance(value, dict):
        raise SvcError("invalid", f"{what}.{key} must be an object or null")
    return value


@dataclass
class Request:
    """One call from a controller to a participant: ``method`` with ``params``.

    ``caller`` describes who is calling (free-form object); ``deadline_ms`` is how long the caller will wait.
    """

    method: str
    params: dict[str, Any]
    id: str
    caller: dict[str, Any] | None = None
    deadline_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form (keys ``svckit, id, method, caller, deadline_ms, params``)."""
        return {
            "svckit": CONTRACT_VERSION,
            "id": self.id,
            "method": self.method,
            "caller": self.caller,
            "deadline_ms": self.deadline_ms,
            "params": self.params,
        }

    def to_json(self) -> str:
        """Return the wire form as one line of JSON."""
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Request:
        """Build a request from its wire form; unknown keys are ignored. Raises ``SvcError("invalid")``."""
        d = _need_dict(d, "request")
        params = d.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise SvcError("invalid", "request.params must be an object")
        deadline = d.get("deadline_ms")
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, int | float)):
            raise SvcError("invalid", "request.deadline_ms must be a number or null")
        rid = d.get("id")
        return cls(
            method=_need_str(d, "method", "request"),
            params=params,
            id="" if rid is None else str(rid),
            caller=_opt_dict(d, "caller", "request"),
            deadline_ms=None if deadline is None else int(deadline),
        )


@dataclass
class Error:
    """The ``error`` object of a failed response."""

    code: str
    message: str
    retry_after_s: float | None = None
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form; optional fields are left out when unset."""
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after_s is not None:
            out["retry_after_s"] = self.retry_after_s
        if self.data is not None:
            out["data"] = self.data
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Error:
        """Build an error from its wire form. Raises ``SvcError("invalid")`` when it is malformed."""
        d = _need_dict(d, "error")
        retry = d.get("retry_after_s")
        if retry is not None and (isinstance(retry, bool) or not isinstance(retry, int | float)):
            raise SvcError("invalid", "error.retry_after_s must be a number or null")
        return cls(
            code=_need_str(d, "code", "error"),
            message=str(d.get("message", "")),
            retry_after_s=None if retry is None else float(retry),
            data=_opt_dict(d, "data", "error"),
        )


@dataclass
class Response:
    """A participant's answer: ``ok`` with a ``result`` object, or not ok with an :class:`Error`."""

    id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: Error | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form (``svckit, id, ok`` plus ``result`` and/or ``error`` when set)."""
        out: dict[str, Any] = {"svckit": CONTRACT_VERSION, "id": self.id, "ok": self.ok}
        if self.result is not None:
            out["result"] = self.result
        if self.error is not None:
            out["error"] = self.error.to_dict()
        return out

    def to_json(self) -> str:
        """Return the wire form as one line of JSON (what a participant prints last on stdout)."""
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Response:
        """Build a response from its wire form; unknown keys are ignored. Raises ``SvcError("invalid")``."""
        d = _need_dict(d, "response")
        ok = d.get("ok")
        if not isinstance(ok, bool):
            raise SvcError("invalid", "response.ok must be true or false")
        err = d.get("error")
        rid = d.get("id")
        return cls(
            id="" if rid is None else str(rid),
            ok=ok,
            result=_opt_dict(d, "result", "response"),
            error=None if err is None else Error.from_dict(err),
        )


def parse_stdout(text: str) -> Response:
    """Find the response in a participant's stdout.

    Participants may print log lines first; the last non-empty line that parses as a JSON object is the
    response. Raises ``SvcError("invalid")`` when there is no such line or it is not a valid response.
    """
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return Response.from_dict(obj)
    raise SvcError("invalid", "no JSON response line on stdout", data={"stdout_tail": text[-500:]})


def redact(obj: Any) -> Any:
    """Return a deep copy of ``obj`` in which values of secret-looking keys are replaced by ``"***"``.

    A key is secret-looking when it contains token, secret, password, passwd, key, auth or cookie (any case).
    Dicts, lists and tuples are walked recursively; the input is never modified.
    """
    if isinstance(obj, dict):
        return {k: "***" if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact(v) for v in obj)
    return copy.deepcopy(obj)


@dataclass
class Op:
    """An operation a controller announces (restart, update, ...) and asks participants to quiesce for.

    Times are UNIX timestamps: ``deadline`` is when the controller stops waiting for participants,
    ``expires_at`` when a lease for this op counts as stale. ``wait_for`` says what ready means
    (``"interrupted"``: in-flight work may be interrupted safely; other values are tool-defined); ``now``
    asks participants not to wait at all. ``lease`` is the lease file path, when there is one.
    On the wire ``affected_pids`` is nested as ``"affected": {"pids": [...] | null}``.
    """

    id: str
    kind: str
    targets: list[str]
    reason: str = ""
    by: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    deadline: float = 0.0
    expires_at: float = 0.0
    affected_pids: list[int] | None = None
    wait_for: str = "interrupted"
    now: bool = False
    lease: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form."""
        return {
            "id": self.id,
            "kind": self.kind,
            "targets": list(self.targets),
            "reason": self.reason,
            "by": dict(self.by),
            "started_at": self.started_at,
            "deadline": self.deadline,
            "expires_at": self.expires_at,
            "affected": {"pids": None if self.affected_pids is None else list(self.affected_pids)},
            "wait_for": self.wait_for,
            "now": self.now,
            "lease": self.lease,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Op:
        """Build an op from its wire form; missing optional fields get defaults. Raises ``SvcError("invalid")``."""
        d = _need_dict(d, "op")
        targets = d.get("targets", [])
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            raise SvcError("invalid", "op.targets must be a list of strings")
        affected = d.get("affected") or {}
        if not isinstance(affected, dict):
            raise SvcError("invalid", "op.affected must be an object")
        pids = affected.get("pids")
        if pids is not None:
            if not isinstance(pids, list) or not all(isinstance(p, int) and not isinstance(p, bool) for p in pids):
                raise SvcError("invalid", "op.affected.pids must be a list of integers or null")
            pids = list(pids)
        lease = d.get("lease")
        try:
            return cls(
                id=_need_str(d, "id", "op"),
                kind=_need_str(d, "kind", "op"),
                targets=list(targets),
                reason=str(d.get("reason") or ""),
                by=_opt_dict(d, "by", "op") or {},
                started_at=float(d.get("started_at") or 0.0),
                deadline=float(d.get("deadline") or 0.0),
                expires_at=float(d.get("expires_at") or 0.0),
                affected_pids=pids,
                wait_for=str(d.get("wait_for") or "interrupted"),
                now=bool(d.get("now", False)),
                lease=None if lease is None else str(lease),
            )
        except (TypeError, ValueError) as exc:
            raise SvcError("invalid", f"op has a malformed field: {exc}") from exc


def new_op_id(clock: Callable[[], float] = time.time) -> str:
    """Return a fresh op id ``op-YYYYMMDDTHHMMSS-<4 hex>`` (UTC time from ``clock``, random suffix)."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(clock()))
    return f"op-{stamp}-{secrets.token_hex(2)}"


def matches(value: str, globs: Iterable[str]) -> bool:
    """Return True when ``value`` matches any of the shell-style ``globs`` (case-sensitive fnmatch)."""
    return any(fnmatch.fnmatchcase(value, g) for g in globs)


def load_schema(name: str) -> dict[str, Any]:
    """Load the JSON Schema ``contract/schemas/<name>.json`` shipped with the package (``.json`` optional)."""
    if name.endswith(".json"):
        name = name[: -len(".json")]
    if not _SCHEMA_NAME.match(name):
        raise ValueError(f"bad schema name: {name!r}")
    res = importlib.resources.files("cli_svckit.contract").joinpath("schemas").joinpath(f"{name}.json")
    if not res.is_file():
        raise FileNotFoundError(f"no svckit schema named {name!r}")
    data: dict[str, Any] = json.loads(res.read_text(encoding="utf-8"))
    return data

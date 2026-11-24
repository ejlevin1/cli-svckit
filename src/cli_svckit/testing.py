"""Helpers for tools that adopt cli-svckit: a fake participant, temp lease dirs and a conformance check.

``FakeParticipant`` stands in for :class:`~cli_svckit.contract.client.Participant` in controller tests.
``assert_conforms`` runs a real participant command through every method it declares and lists every
contract violation it finds.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cli_svckit.contract.client import CallResult
from cli_svckit.contract.envelope import (
    CONTRACT_VERSION,
    ERROR_CODES,
    EXIT_BUSY,
    EXIT_FAIL,
    EXIT_OK,
    EXIT_TEMPFAIL,
    EXIT_WARN,
    METHODS,
    Error,
    Op,
    Request,
    SvcError,
    load_schema,
    new_op_id,
)
from cli_svckit.coord.lease import LeaseDir

__all__ = [
    "ConformanceReport",
    "FakeParticipant",
    "assert_conforms",
    "make_op",
    "temp_lease_dir",
]


# --------------------------------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------------------------------

Reply = dict[str, Any] | Error | SvcError


class FakeParticipant:
    """A scripted stand-in for ``Participant``: same ``call()``/``describe()`` surface, records every call.

    ``responses`` is either ``{method: [reply, ...]}`` or a callable ``(method, params) -> reply``. A reply is a
    result dict (an ok answer), or an ``Error``/``SvcError`` (a failed answer; code ``timeout`` also sets
    ``timed_out``). Replies are used in order and the last one repeats. A method with no reply answers
    ``unsupported``; ``describe`` falls back to a generated answer built from ``coordination``.
    A ``prepare`` result with ``state == "busy"`` reports rc 3, like a real participant.
    """

    def __init__(
        self,
        name: str,
        responses: dict[str, list[dict[str, Any]]] | Callable[[str, dict[str, Any]], Reply] | None = None,
        fail_policy: str = "open",
        wait_for: str = "interrupted",
        coordination: dict[str, Any] | None = None,
    ) -> None:
        if fail_policy not in ("open", "closed"):
            raise SvcError("invalid", "fail_policy must be 'open' or 'closed'", data={"participant": name})
        self.name = name
        self.responses = responses if responses is not None else {}
        self.fail_policy = fail_policy
        self.wait_for = wait_for
        self.coordination = coordination
        self.cmd: list[str] = ["fake", name]
        self.timeout_s = 120.0
        self.services: list[str] = list((coordination or {}).get("services", []))
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._counts: dict[str, int] = {}

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        """Return the params of every recorded call to ``method``, oldest first."""
        return [p for m, p in self.calls if m == method]

    def _next_reply(self, method: str, params: dict[str, Any]) -> Reply | None:
        if callable(self.responses):
            return self.responses(method, params)
        queue = self.responses.get(method)
        if not queue:
            return None
        index = self._counts.get(method, 0)
        self._counts[method] = index + 1
        return queue[min(index, len(queue) - 1)]

    def _describe_default(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "tool": {"name": self.name, "version": "0.0.0"},
            "contract": {"min": CONTRACT_VERSION, "max": CONTRACT_VERSION},
            "methods": sorted({m for m in self.responses if m in METHODS}) if isinstance(self.responses, dict) else [],
            "services": [],
        }
        if self.coordination is not None:
            result["coordination"] = dict(self.coordination)
        return result

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None) -> CallResult:
        """Record the call and return the scripted ``CallResult`` (never raises)."""
        params = dict(params or {})
        self.calls.append((method, params))
        reply = self._next_reply(method, params)
        if reply is None and method == "describe":
            reply = self._describe_default()
        if reply is None:
            reply = Error("unsupported", f"{method} is not supported by {self.name}")
        return self._result(method, reply)

    def _result(self, method: str, reply: Reply) -> CallResult:
        from cli_svckit.contract.envelope import Response

        if isinstance(reply, SvcError):
            reply = reply.to_error()
        if isinstance(reply, Error):
            timed_out = reply.code == "timeout"
            rc = EXIT_TEMPFAIL if timed_out else EXIT_FAIL
            response = None if timed_out else Response(id="", ok=False, error=reply)
            return CallResult(self.name, method, False, response, reply, rc, 0.0, timed_out, self.fail_policy)
        rc = EXIT_BUSY if method == "prepare" and reply.get("state") == "busy" else EXIT_OK
        response = Response(id="", ok=True, result=dict(reply))
        return CallResult(self.name, method, True, response, None, rc, 0.0, False, self.fail_policy)

    def describe(self, cache: Any = None) -> dict[str, Any] | None:
        """Return the scripted ``describe`` result, or None when it failed. ``cache`` is ignored."""
        res = self.call("describe")
        if res.ok and res.response is not None and isinstance(res.response.result, dict):
            return res.response.result
        return None


# --------------------------------------------------------------------------------------------------------------
# Small builders
# --------------------------------------------------------------------------------------------------------------


def temp_lease_dir(tmp_path: str | os.PathLike[str], *, clock: Callable[[], float] = time.time) -> LeaseDir:
    """Return a ready ``LeaseDir`` in a fresh ``leases`` folder under ``tmp_path`` (create dirs included)."""
    lease_dir = LeaseDir(Path(tmp_path) / "leases", clock=clock)
    lease_dir.ensure()
    return lease_dir


def make_op(kind: str = "service.restart", targets: Iterable[str] = ("svc",), **kw: Any) -> Op:
    """Build an ``Op`` with sensible test defaults (starts now, 5 min deadline/expiry); ``kw`` overrides any field."""
    clock = kw.pop("clock", time.time)
    now = clock()
    fields: dict[str, Any] = {
        "id": new_op_id(clock),
        "kind": kind,
        "targets": list(targets),
        "reason": "test",
        "by": {"tool": "example-tool"},
        "started_at": now,
        "deadline": now + 300.0,
        "expires_at": now + 300.0,
    }
    fields.update(kw)
    return Op(**fields)


# --------------------------------------------------------------------------------------------------------------
# Conformance
# --------------------------------------------------------------------------------------------------------------


@dataclass
class ConformanceReport:
    """What ``assert_conforms`` saw: the describe result, per-method results and any violations."""

    describe: dict[str, Any] | None = None
    methods: list[str] = field(default_factory=list)
    results: dict[str, CallResult] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no violation was found."""
        return not self.violations


_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
    "null": (type(None),),
}


def _type_ok(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES.get(name, (object,)))


def _check_schema(value: Any, schema: dict[str, Any], defs: dict[str, Any], path: str, out: list[str]) -> None:
    """Check ``value`` against the subset of JSON Schema the bundled schemas use; append problems to ``out``."""
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref.startswith("#/$defs/") and ref[8:] in defs:
            _check_schema(value, defs[ref[8:]], defs, path, out)
        return  # cross-file refs are not followed by the stdlib check
    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else list(types)
        if not any(_type_ok(value, n) for n in names):
            out.append(f"{path}: expected {'|'.join(names)}, got {type(value).__name__}")
            return
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} is not one of {schema['enum']}")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and isinstance(value, str) and not re.search(pattern, value):
        out.append(f"{path}: {value!r} does not match {pattern}")
    minimum = schema.get("minimum")
    if minimum is not None and _type_ok(value, "number") and value < minimum:
        out.append(f"{path}: {value} is below the minimum {minimum}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{path}: missing required key {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                _check_schema(value[key], sub, defs, f"{path}.{key}", out)
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            known = schema.get("properties", {})
            for key, item in value.items():
                if key not in known:
                    _check_schema(item, extra, defs, f"{path}.{key}", out)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            _check_schema(item, schema["items"], defs, f"{path}[{i}]", out)


def _validate(value: Any, def_name: str, schema_file: str, path: str, out: list[str]) -> None:
    """Validate against ``$defs/<def_name>`` of a bundled schema (stdlib check, plus jsonschema when importable)."""
    try:
        doc = load_schema(schema_file)
    except Exception:  # noqa: BLE001 - a missing schema file just means nothing to check against
        return
    defs = doc.get("$defs", {})
    if def_name not in defs:
        return
    _check_schema(value, defs[def_name], defs, path, out)
    if out:
        return  # the stdlib check already explained what is wrong
    try:
        import jsonschema  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError:
        return
    try:
        jsonschema.validate(value, {"$schema": doc.get("$schema"), "$defs": defs, "$ref": f"#/$defs/{def_name}"})
    except jsonschema.ValidationError as exc:
        out.append(f"{path}: {exc.message}")
    except Exception:  # noqa: BLE001 - unresolvable cross-file refs etc.: the stdlib check stands
        return


def _sample_op(contract_kinds: list[str], services: list[str], lease_dir: LeaseDir) -> Op:
    """Build a synthetic op that matches the participant's declared interest, with a lease in a temp dir."""
    kind = next((k.replace("*", "check") for k in contract_kinds), "service.restart")
    target = next((s.replace("*", "check") for s in services), "svc")
    op = make_op(kind=kind, targets=[target], reason="conformance check", by={"tool": "svckit-conformance"})
    return lease_dir.create(op).op


def _sample_params(method: str, op: Op) -> dict[str, Any]:
    safe_flags = {"dry_run": True, "confirm": False, "scope": "all"}
    if method == "doctor":
        return {"fix": False, "deep": False}
    if method in ("install", "uninstall"):
        return dict(safe_flags)
    if method == "prepare":
        return {"op": op.to_dict(), "poll": 0}
    if method == "complete":
        facts = {"targets": {}, "duration_s": 0.0, "waited_s": 0.0, "capped": False, "forced_now": False}
        return {"op": op.to_dict(), "outcome": "aborted", "facts": facts}
    return {}


def _expected_rc(method: str, response: dict[str, Any]) -> int:
    if not response.get("ok"):
        err = response.get("error")
        return EXIT_TEMPFAIL if isinstance(err, dict) and err.get("code") == "timeout" else EXIT_FAIL
    result = response.get("result") or {}
    if method == "prepare" and result.get("state") == "busy":
        return EXIT_BUSY
    if method in ("health", "doctor"):
        return {"warn": EXIT_WARN, "fail": EXIT_FAIL}.get(result.get("status"), EXIT_OK)
    return EXIT_OK


def _run_raw(cmd: list[str], method: str, params: dict[str, Any], timeout_s: float) -> tuple[str, int | None, str, str]:
    """Run ``cmd svc <method> --json`` with a request on stdin; return (request id, rc, stdout, problem)."""
    req = Request(
        method=method,
        params=params,
        id=uuid.uuid4().hex,
        caller={"tool": "svckit-conformance"},
        deadline_ms=int(timeout_s * 1000),
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
    try:
        done = subprocess.run(
            [*cmd, "svc", method, "--json"],
            input=req.to_json() + "\n",
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return req.id, None, "", f"timed out after {timeout_s:g}s"
    except (OSError, ValueError) as exc:
        return req.id, None, "", f"cannot run command: {type(exc).__name__}: {exc}"
    return req.id, done.returncode, done.stdout, ""


def _last_json_line(stdout: str) -> Any:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _check_envelope(
    method: str, rid: str, rc: int | None, stdout: str, problem: str, out: list[str]
) -> dict[str, Any] | None:
    label = f"{method}"
    if problem:
        out.append(f"{label}: {problem}")
        return None
    obj = _last_json_line(stdout)
    if obj is None:
        out.append(f"{label}: stdout has no JSON object line (rc={rc})")
        return None
    problems: list[str] = []
    _validate(obj, "response", "envelope", f"{label}.response", problems)
    out.extend(problems)
    if obj.get("id") != rid:
        out.append(f"{label}: response id {obj.get('id')!r} does not echo the request id")
    if isinstance(obj.get("svckit"), str) and obj["svckit"].split(".")[0] != CONTRACT_VERSION.split(".")[0]:
        out.append(f"{label}: response svckit {obj['svckit']!r} has a different major than {CONTRACT_VERSION}")
    if isinstance(obj.get("ok"), bool):
        want = _expected_rc(method, obj)
        if rc != want:
            out.append(f"{label}: exit code {rc}, expected {want} for this response")
    error = obj.get("error")
    if obj.get("ok") is False and isinstance(error, dict) and error.get("code") not in ERROR_CODES:
        out.append(f"{label}: error code {error.get('code')!r} is not one of {list(ERROR_CODES)}")
    return obj


def assert_conforms(
    cmd: list[str], *, methods: Iterable[str] | None = None, timeout_s: float = 30
) -> ConformanceReport:
    """Check that ``cmd`` speaks the svckit contract; return a report or raise ``AssertionError`` listing everything.

    Calls ``describe`` and checks the envelope, the describe shape and that the contract range includes ours. Then
    calls every declared method (or just ``methods``) with safe sample params (``dry_run`` true, ``confirm`` false,
    a synthetic op whose lease lives in a temp dir) and checks each response's envelope, result shape and exit code.
    A declared method answering ``unsupported`` or ``internal`` is a violation; other error codes are accepted.
    """
    report = ConformanceReport()
    out = report.violations
    cmd = list(cmd)
    tmp = tempfile.mkdtemp(prefix="svckit-conformance-")
    try:
        rid, rc, stdout, problem = _run_raw(cmd, "describe", {}, timeout_s)
        obj = _check_envelope("describe", rid, rc, stdout, problem, out)
        result = obj.get("result") if obj else None
        if obj is not None and obj.get("ok") is not True:
            out.append("describe: must succeed (ok: true)")
        if isinstance(result, dict):
            report.describe = result
            problems: list[str] = []
            _validate(result, "result", "describe", "describe.result", problems)
            out.extend(problems)
            contract = result.get("contract")
            if isinstance(contract, dict) and "min" in contract and "max" in contract:
                from cli_svckit.contract.client import negotiate

                try:
                    negotiate(contract)
                except SvcError as exc:
                    out.append(f"describe: {exc.message}")
        declared = [m for m in (report.describe or {}).get("methods", []) if isinstance(m, str)]
        for m in declared:
            if m not in METHODS:
                out.append(f"describe: declared method {m!r} is not a contract method")
        if "describe" not in declared and report.describe is not None:
            out.append("describe: methods must include 'describe'")
        wanted = [m for m in declared if m in METHODS and m != "describe"]
        if methods is not None:
            chosen = list(methods)
            wanted = [m for m in chosen if m != "describe"]
            for m in wanted:
                if m not in declared:
                    out.append(f"{m}: requested but not declared in describe.methods")
        report.methods = wanted

        coord = (report.describe or {}).get("coordination")
        coord = coord if isinstance(coord, dict) else {}
        lease_dir = LeaseDir(Path(tmp) / "leases")
        lease_dir.ensure()
        op = _sample_op(list(coord.get("kinds") or []), list(coord.get("services") or []), lease_dir)
        for method in wanted:
            params = _sample_params(method, op)
            rid, rc, stdout, problem = _run_raw(cmd, method, params, timeout_s)
            obj = _check_envelope(method, rid, rc, stdout, problem, out)
            if obj is None:
                continue
            error = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            if obj.get("ok") is True:
                problems = []
                _validate(obj.get("result"), "result", method, f"{method}.result", problems)
                out.extend(problems)
            elif error.get("code") in ("unsupported", "internal"):
                out.append(f"{method}: declared but answered error {error.get('code')!r}: {error.get('message')}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if out:
        raise AssertionError(f"{len(out)} contract violation(s) for {cmd}:\n" + "\n".join(f"  - {v}" for v in out))
    return report

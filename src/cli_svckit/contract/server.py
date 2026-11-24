"""Participant side of the svckit contract: turn a request into one JSON response line and an exit code.

A *provider* is any object with optional methods named after :data:`~cli_svckit.contract.envelope.METHODS`
(``describe``, ``health``, ...). Each takes ``params: dict`` and returns a result dict, raising
:class:`~cli_svckit.contract.envelope.SvcError` to report a contract error.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, TextIO

from cli_svckit.contract.envelope import (
    CONTRACT_VERSION,
    EXIT_BUSY,
    EXIT_FAIL,
    EXIT_OK,
    EXIT_TEMPFAIL,
    EXIT_USAGE,
    EXIT_WARN,
    METHODS,
    Error,
    Request,
    Response,
    SvcError,
    redact,
)


def implemented_methods(provider: Any) -> list[str]:
    """Return the contract methods the provider implements (callable attributes named as in METHODS)."""
    return [m for m in METHODS if callable(getattr(provider, m, None))]


def _major(version: Any) -> str:
    return str(version).split(".", 1)[0]


def _parse_request(method: str, request_text: str) -> Request:
    text = request_text.strip()
    if not text:
        return Request(method=method, params={}, id="")
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise SvcError("invalid", f"request is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise SvcError("invalid", "request must be a JSON object")
    version = obj.get("svckit")
    if version is not None and _major(version) != _major(CONTRACT_VERSION):
        raise SvcError("incompatible", f"unsupported svckit version {version!r}; this server speaks {CONTRACT_VERSION}")
    obj = dict(obj)
    obj.setdefault("method", method)
    req = Request.from_dict(obj)
    req.method = method  # the command line decides which method runs
    return req


def _rc_for_result(method: str, result: dict[str, Any]) -> int:
    if method == "prepare" and result.get("state") == "busy":
        return EXIT_BUSY
    if method in ("health", "doctor"):
        status = result.get("status")
        if status == "warn":
            return EXIT_WARN
        if status == "fail":
            return EXIT_FAIL
    return EXIT_OK


def _rc_for_error(error: Error) -> int:
    return EXIT_TEMPFAIL if error.code == "timeout" else EXIT_FAIL


def _fill_describe(provider: Any, result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    contract = out.get("contract")
    if not isinstance(contract, dict):
        contract = {}
    out["contract"] = {"min": "1.0", "max": "1.0", **contract}
    if "methods" not in out:
        out["methods"] = implemented_methods(provider)
    return out


def dispatch(provider: Any, method: str, request_text: str) -> tuple[str, int]:
    """Run ``method`` on ``provider`` for the request text; return ``(one JSON line, exit code)``.

    Empty text means a request with empty params. Bad JSON gives ``invalid``, a different svckit major gives
    ``incompatible``, a method the provider lacks gives ``unsupported`` and any other exception gives ``internal``
    (the traceback goes to stderr only). Results are redacted before output.
    """
    rid = ""
    try:
        req = _parse_request(method, request_text)
        rid = req.id
        if method not in METHODS or not callable(getattr(provider, method, None)):
            raise SvcError("unsupported", f"method {method!r} is not supported")
        result = getattr(provider, method)(req.params)
        if not isinstance(result, dict):
            raise SvcError("internal", f"{method} returned {type(result).__name__}, expected an object")
        if method == "describe":
            result = _fill_describe(provider, result)
        result = redact(result)
        return Response(id=rid, ok=True, result=result).to_json(), _rc_for_result(method, result)
    except SvcError as exc:
        err = exc.to_error()
    except Exception as exc:  # noqa: BLE001 - any provider failure becomes an `internal` error
        traceback.print_exc(file=sys.stderr)
        err = Error(code="internal", message=f"{type(exc).__name__}: {exc}")
    if err.data is not None:
        err.data = redact(err.data)
    return Response(id=rid, ok=False, error=err).to_json(), _rc_for_error(err)


def run(
    provider: Any,
    argv: list[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Handle ``<method> [--json] [--request-file PATH]`` (argv without the ``svc`` word); return the exit code.

    The request is read from the file, or from stdin when it is not a terminal. Exactly one JSON line is written
    to stdout (pretty-printed when ``--json`` is absent).
    """
    import argparse

    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    parser = argparse.ArgumentParser(prog="svc", description="svckit contract entry point")
    parser.add_argument("method", help="one of: " + ", ".join(METHODS))
    parser.add_argument("--json", action="store_true", dest="as_json", help="print one JSON line")
    parser.add_argument("--request-file", default=None, help="read the request from this file")
    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK

    text = ""
    if ns.request_file:
        try:
            with open(ns.request_file, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            line = Response(id="", ok=False, error=Error("invalid", f"cannot read request file: {exc}")).to_json()
            stdout.write((line if ns.as_json else _pretty(line)) + "\n")
            return EXIT_FAIL
    elif not _is_tty(stdin):
        text = stdin.read()

    line, rc = dispatch(provider, ns.method, text)
    stdout.write((line if ns.as_json else _pretty(line)) + "\n")
    return rc


def _is_tty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def _pretty(line: str) -> str:
    return json.dumps(json.loads(line), indent=2, sort_keys=False)

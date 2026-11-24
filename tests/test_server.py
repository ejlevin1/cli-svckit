"""Tests for the participant-side dispatcher and ``run``."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from cli_svckit.contract.envelope import (
    EXIT_BUSY,
    EXIT_FAIL,
    EXIT_OK,
    EXIT_TEMPFAIL,
    EXIT_USAGE,
    EXIT_WARN,
    SvcError,
)
from cli_svckit.contract.server import dispatch, run


class Provider:
    def __init__(self) -> None:
        self.result: dict[str, Any] = {}
        self.exc: BaseException | None = None
        self.seen: dict[str, Any] | None = None

    def _go(self, params: dict[str, Any]) -> dict[str, Any]:
        self.seen = params
        if self.exc:
            raise self.exc
        return self.result

    health = doctor = prepare = describe = _go


def call(p: Any, method: str, text: str = "") -> tuple[dict[str, Any], int]:
    line, rc = dispatch(p, method, text)
    assert "\n" not in line
    return json.loads(line), rc


def test_ok_and_id_echo() -> None:
    p = Provider()
    p.result = {"state": "ready"}
    out, rc = call(p, "prepare", json.dumps({"svckit": "1.0", "id": "r1", "method": "prepare", "params": {"a": 1}}))
    assert rc == EXIT_OK and out["ok"] and out["id"] == "r1" and out["result"] == {"state": "ready"}
    assert p.seen == {"a": 1}


def test_empty_request() -> None:
    p = Provider()
    out, rc = call(p, "health")
    assert rc == EXIT_OK and p.seen == {} and out["id"] == ""


@pytest.mark.parametrize(
    ("method", "result", "rc"),
    [
        ("prepare", {"state": "busy"}, EXIT_BUSY),
        ("prepare", {"state": "ready"}, EXIT_OK),
        ("health", {"status": "warn"}, EXIT_WARN),
        ("health", {"status": "fail"}, EXIT_FAIL),
        ("health", {"status": "pass"}, EXIT_OK),
        ("doctor", {"status": "warn"}, EXIT_WARN),
        ("doctor", {"status": "fail"}, EXIT_FAIL),
    ],
)
def test_result_rc(method: str, result: dict[str, Any], rc: int) -> None:
    p = Provider()
    p.result = result
    out, got = call(p, method)
    assert out["ok"] and got == rc


@pytest.mark.parametrize(
    ("code", "rc"),
    [("invalid", EXIT_FAIL), ("requires_root", EXIT_FAIL), ("timeout", EXIT_TEMPFAIL), ("forbidden", EXIT_FAIL)],
)
def test_error_rc(code: str, rc: int) -> None:
    p = Provider()
    p.exc = SvcError(code, "nope", retry_after_s=2.0)
    out, got = call(p, "prepare")
    assert not out["ok"] and out["error"]["code"] == code and out["error"]["retry_after_s"] == 2.0 and got == rc


def test_unsupported() -> None:
    out, rc = call(Provider(), "install")
    assert out["error"]["code"] == "unsupported" and rc == EXIT_FAIL
    out, rc = call(Provider(), "bogus")
    assert out["error"]["code"] == "unsupported" and rc == EXIT_FAIL


def test_invalid_and_incompatible() -> None:
    out, rc = call(Provider(), "health", "{nope")
    assert out["error"]["code"] == "invalid" and rc == EXIT_FAIL
    out, rc = call(Provider(), "health", json.dumps({"svckit": "2.0", "method": "health"}))
    assert out["error"]["code"] == "incompatible"
    out, rc = call(Provider(), "health", json.dumps({"svckit": "1.7", "method": "health"}))
    assert out["ok"]


def test_internal_error_no_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    p = Provider()
    p.exc = ValueError("boom")
    line, rc = dispatch(p, "health", "")
    out = json.loads(line)
    assert out["error"] == {"code": "internal", "message": "ValueError: boom"}
    assert rc == EXIT_FAIL and "Traceback" not in line
    assert "Traceback" in capsys.readouterr().err


def test_redaction() -> None:
    p = Provider()
    p.result = {"status": "pass", "api_token": "abc", "nested": [{"password": "x", "ok": 1}]}
    out, _ = call(p, "health")
    assert out["result"]["api_token"] == "***"
    assert out["result"]["nested"][0] == {"password": "***", "ok": 1}


def test_describe_defaults() -> None:
    p = Provider()
    p.result = {"tool": {"name": "example-tool", "version": "1"}}
    out, _ = call(p, "describe")
    assert out["result"]["contract"] == {"min": "1.0", "max": "1.0"}
    assert out["result"]["methods"] == ["describe", "health", "doctor", "prepare"]


def test_run_stdin_json() -> None:
    p = Provider()
    p.result = {"status": "warn"}
    out = io.StringIO()
    rc = run(p, ["health", "--json"], stdin=io.StringIO('{"id":"z"}'), stdout=out)
    assert rc == EXIT_WARN
    assert out.getvalue().count("\n") == 1 and json.loads(out.getvalue())["id"] == "z"


def test_run_request_file_and_pretty(tmp_path: Any) -> None:
    f = tmp_path / "r.json"
    f.write_text('{"id":"q"}')
    p = Provider()
    out = io.StringIO()
    assert run(p, ["health", "--request-file", str(f)], stdin=io.StringIO(""), stdout=out) == EXIT_OK
    assert "\n  " in out.getvalue() and json.loads(out.getvalue())["id"] == "q"
    out = io.StringIO()
    assert run(p, ["health", "--json", "--request-file", str(tmp_path / "missing")], stdout=out) == EXIT_FAIL
    assert json.loads(out.getvalue())["error"]["code"] == "invalid"


def test_run_tty_stdin_not_read() -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

        def read(self, *a: Any) -> str:
            raise AssertionError("must not read a TTY")

    out = io.StringIO()
    assert run(Provider(), ["health", "--json"], stdin=Tty(), stdout=out) == EXIT_OK


def test_run_usage_error() -> None:
    assert run(Provider(), [], stdin=io.StringIO(), stdout=io.StringIO()) == EXIT_USAGE

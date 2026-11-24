"""Envelope dataclasses, stdout parsing, redaction, op ids and glob matching."""

from __future__ import annotations

import json
import re

import pytest

from cli_svckit.contract import (
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
    matches,
    new_op_id,
    parse_stdout,
    redact,
)


def test_constants():
    assert CONTRACT_VERSION == "1.0"
    assert METHODS == ("describe", "health", "doctor", "install", "uninstall", "prepare", "complete")
    assert (EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_BUSY, EXIT_WARN, EXIT_TEMPFAIL) == (0, 1, 2, 3, 4, 75)
    assert ERROR_CODES == (
        "unsupported",
        "invalid",
        "forbidden",
        "requires_root",
        "timeout",
        "internal",
        "incompatible",
    )


def test_request_round_trip_and_wire_keys():
    req = Request("health", {"deep": True}, "r-1", caller={"tool": "example-tool"}, deadline_ms=5000)
    d = req.to_dict()
    assert list(d) == ["svckit", "id", "method", "caller", "deadline_ms", "params"]
    assert d["svckit"] == CONTRACT_VERSION
    assert Request.from_dict(d) == req
    assert Request.from_dict(json.loads(req.to_json())) == req
    minimal = Request("describe", {}, "r-2")
    assert Request.from_dict(minimal.to_dict()) == minimal


def test_request_from_dict_ignores_unknown_and_rejects_bad():
    req = Request.from_dict({"svckit": "1.3", "id": "x", "method": "describe", "future": 1})
    assert req.params == {} and req.caller is None
    for bad in ({"id": "x"}, {"method": "describe", "params": []}, [], {"method": "m", "deadline_ms": "soon"}):
        with pytest.raises(SvcError) as ei:
            Request.from_dict(bad)  # type: ignore[arg-type]
        assert ei.value.code == "invalid"


def test_error_round_trip():
    full = Error("timeout", "too slow", retry_after_s=2.5, data={"n": 1})
    assert Error.from_dict(full.to_dict()) == full
    bare = Error("internal", "boom")
    assert bare.to_dict() == {"code": "internal", "message": "boom"}
    assert Error.from_dict(bare.to_dict()) == bare


def test_response_round_trip():
    ok = Response("r-1", True, result={"state": "ready"})
    assert Response.from_dict(ok.to_dict()) == ok
    assert ok.to_dict()["svckit"] == CONTRACT_VERSION and "error" not in ok.to_dict()
    bad = Response("r-2", False, error=Error("forbidden", "no", data={"why": "uid"}))
    assert Response.from_dict(bad.to_dict()) == bad
    line = bad.to_json()
    assert "\n" not in line and Response.from_dict(json.loads(line)) == bad
    with pytest.raises(SvcError):
        Response.from_dict({"id": "x", "ok": "yes"})


def test_svcerror_to_error():
    exc = SvcError("requires_root", "need root", data={"argv": ["sudo"]})
    assert str(exc) == "need root"
    assert exc.to_error() == Error("requires_root", "need root", None, {"argv": ["sudo"]})
    assert SvcError("timeout", "t", retry_after_s=3).to_error().retry_after_s == 3


def test_parse_stdout_skips_logs_and_trailing_blanks():
    resp = Response("r", True, result={"a": 1})
    text = "starting\n{not json\n[1, 2]\n" + resp.to_json() + "\n\n   \n"
    assert parse_stdout(text) == resp


def test_parse_stdout_last_json_line_wins():
    first = Response("r", False, error=Error("internal", "x")).to_json()
    last = Response("r", True, result={}).to_json()
    assert parse_stdout(f"{first}\n{last}\ndone.\n").ok is True


@pytest.mark.parametrize("text", ["", "\n\n", "hello\nworld\n", '[1]\n"str"\n'])
def test_parse_stdout_without_json_is_invalid(text):
    with pytest.raises(SvcError) as ei:
        parse_stdout(text)
    assert ei.value.code == "invalid"


def test_redact_nested_lists_and_case():
    src = {
        "user": "alice",
        "API_Token": "t0",
        "nested": {"Password": "p", "keep": 1, "deeper": [{"cookie": "c", "x": 2}, ("authHeader", {"Secret": 3})]},
        "items": [{"private_key": {"a": 1}}, "plain"],
    }
    out = redact(src)
    assert out == {
        "user": "alice",
        "API_Token": "***",
        "nested": {
            "Password": "***",
            "keep": 1,
            "deeper": [{"cookie": "***", "x": 2}, ("authHeader", {"Secret": "***"})],
        },
        "items": [{"private_key": "***"}, "plain"],
    }
    assert src["API_Token"] == "t0" and src["nested"]["deeper"][0]["cookie"] == "c"  # input untouched
    assert out["nested"] is not src["nested"]
    assert redact("scalar") == "scalar"


def test_op_round_trip_and_affected_nesting():
    op = Op(
        id="op-20260101T120000-a1b2",
        kind="update",
        targets=["com.example.worker"],
        reason="upgrade",
        by={"tool": "example-tool"},
        started_at=1.0,
        deadline=2.0,
        expires_at=3.0,
        affected_pids=[10, 11],
        wait_for="idle",
        now=True,
        lease="~/leases/op.json",
    )
    d = op.to_dict()
    assert d["affected"] == {"pids": [10, 11]} and "affected_pids" not in d
    assert Op.from_dict(d) == op
    assert Op.from_dict(json.loads(json.dumps(d))) == op


def test_op_null_pids_and_defaults():
    op = Op(id="op-x", kind="restart", targets=[])
    d = op.to_dict()
    assert d["affected"] == {"pids": None}
    assert json.loads(json.dumps(d))["affected"] == {"pids": None}
    assert Op.from_dict(d) == op
    assert Op.from_dict({"id": "op-x", "kind": "restart", "targets": []}) == op
    with pytest.raises(SvcError):
        Op.from_dict({"id": "op-x", "kind": "restart", "targets": [], "affected": {"pids": ["1"]}})
    with pytest.raises(SvcError):
        Op.from_dict({"kind": "restart", "targets": []})


def test_op_default_by_is_not_shared():
    a, b = Op("a", "k", []), Op("b", "k", [])
    a.by["x"] = 1
    assert b.by == {}


def test_new_op_id_uses_utc_fake_clock():
    oid = new_op_id(clock=lambda: 1767268800.0)  # 2026-01-01T12:00:00Z
    assert re.fullmatch(r"op-20260101T120000-[0-9a-f]{4}", oid), oid
    assert re.fullmatch(r"op-\d{8}T\d{6}-[0-9a-f]{4}", new_op_id())


def test_matches():
    assert matches("update", ["restart*", "update"])
    assert matches("restart-all", ["restart*"])
    assert matches("com.example.worker", ["com.example.*"])
    assert not matches("com.example.worker", ["com.other.*"])
    assert not matches("Update", ["update"])  # case-sensitive everywhere
    assert not matches("anything", [])
    assert matches("anything", ("*",))

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from cli_svckit.contract.client import CallResult, DescribeCache, Participant, effective_ready, negotiate
from cli_svckit.contract.envelope import CONTRACT_VERSION, Error, Response, SvcError

SCRIPT = r"""
import json, os, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("example-tool 1.0.0"); sys.exit(0)
mode = os.environ.get("MODE", "ok")
method = args[1]
req = json.loads(sys.stdin.readline())
log = os.environ.get("LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"req": req, "env": sorted(os.environ)}) + "\n")
def out(d): print(json.dumps(d))
if mode == "garbage":
    print("secret-token-xyz not json"); sys.exit(1)
if mode == "hang":
    time.sleep(60)
if mode == "stubborn":
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
if mode == "busy":
    out({"svckit": "1.0", "id": req["id"], "ok": True, "result": {"ready": False}}); sys.exit(3)
if mode == "error":
    out({"svckit": "1.0", "id": req["id"], "ok": False, "error": {"code": "internal", "message": "boom"}}); sys.exit(1)
if mode == "unsupported":
    out({"svckit": "1.0", "id": req["id"], "ok": False, "error": {"code": "unsupported", "message": "no"}}); sys.exit(1)
if method == "describe":
    out({"svckit": "1.0", "id": req["id"], "ok": True,
         "result": {"tool": {"name": "example-tool", "version": "1.0.0"}}})
else:
    out({"svckit": "1.0", "id": req["id"], "ok": True, "result": {"ready": True}})
"""


@pytest.fixture
def script(tmp_path: Path) -> Path:
    p = tmp_path / "tool.py"
    p.write_text(SCRIPT)
    return p


def mk(script: Path, mode: str = "ok", **kw: Any) -> Participant:
    env = {"PATH": "/usr/bin:/bin", "MODE": mode, **kw.pop("env", {})}
    return Participant("example-tool", [sys.executable, str(script)], env=env, **kw)


def first_log(log: Path) -> dict[str, Any]:
    return json.loads(log.read_text().splitlines()[0])


def test_ok(script: Path) -> None:
    r = mk(script).call("prepare", {"op": 1})
    assert isinstance(r, CallResult) and r.ok and r.rc == 0 and not r.timed_out
    assert r.response is not None and r.response.result == {"ready": True}
    assert effective_ready(r) is True


def test_request_default_caller_and_deadline(script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SVCKIT_CALLER", raising=False)
    log = tmp_path / "log.jsonl"
    mk(script, env={"LOG": str(log)}).call("health", timeout_s=7)
    req = first_log(log)["req"]
    assert req["caller"] == {"tool": "cli-svckit"} and req["deadline_ms"] == 7000
    assert req["method"] == "health" and req["svckit"] == CONTRACT_VERSION and req["id"]


def test_caller_from_env(script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("SVCKIT_CALLER", '{"tool": "alice-tool"}')
    mk(script, env={"LOG": str(log)}).call("health")
    assert first_log(log)["req"]["caller"] == {"tool": "alice-tool"}


def test_busy(script: Path) -> None:
    r = mk(script, "busy").call("prepare")
    assert r.ok and r.rc == 3
    assert effective_ready(r) is False


def test_error_and_fail_policy(script: Path) -> None:
    r = mk(script, "error").call("prepare")
    assert not r.ok and r.error is not None and r.error.code == "internal"
    assert effective_ready(r, "open") is True
    assert effective_ready(r, "closed") is False


def test_effective_ready_uses_participant_fail_policy(script: Path) -> None:
    r = mk(script, "error", fail_policy="closed").call("prepare")
    assert r.fail_policy == "closed"
    assert effective_ready(r) is False
    assert effective_ready(mk(script, "error").call("prepare")) is True


def test_timeout_escalates_to_sigkill(script: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cli_svckit.contract.client.KILL_GRACE_S", 0.5)
    t = time.monotonic()
    r = mk(script, "stubborn", timeout_s=1.0).call("prepare")
    assert time.monotonic() - t < 4.0
    assert r.timed_out and r.error is not None and r.error.code == "timeout"


def test_corrupt_cache_entry_is_a_miss(script: Path, tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    p = mk(script)
    path.write_text(json.dumps({DescribeCache._key(p): {"at": "bad", "version": "1.0.0", "result": {"x": 1}}}))
    res = p.describe(DescribeCache(path))
    assert res is not None and res["tool"]["name"] == "example-tool"


def test_unsupported_not_applicable(script: Path) -> None:
    r = mk(script, "unsupported").call("prepare")
    assert effective_ready(r, "closed") is None


def test_garbage_never_leaks_output(script: Path) -> None:
    r = mk(script, "garbage").call("prepare")
    assert not r.ok and r.error is not None and r.error.code == "invalid"
    assert "secret-token" not in r.error.message and "rc=1" in r.error.message
    assert effective_ready(r, "closed") is False


def test_missing_command_does_not_raise() -> None:
    r = Participant("x", ["/nonexistent/binary"]).call("health")
    assert not r.ok and r.error is not None and r.rc is None


def test_timeout_kills(script: Path) -> None:
    t = time.monotonic()
    r = mk(script, "hang", timeout_s=1.0).call("prepare")
    assert time.monotonic() - t < 3.5
    assert r.timed_out and not r.ok and r.error is not None and r.error.code == "timeout"
    assert r.error.message == "no answer in 1s"
    assert effective_ready(r, "open") is True and effective_ready(r, "closed") is False


def test_env_cleaning(script: Path, tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    mk(script, env={"LOG": str(log), "HERMES_KANBAN_TASK": "1", "KEEP_ME": "1"}).call("health")
    names = first_log(log)["env"]
    assert "KEEP_ME" in names and "HERMES_KANBAN_TASK" not in names


def test_env_default_copies_os_environ(script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("HERMES_KANBAN_X", "1")
    monkeypatch.setenv("MODE", "ok")
    monkeypatch.setenv("LOG", str(log))
    Participant("t", [sys.executable, str(script)]).call("health")
    names = first_log(log)["env"]
    assert "HERMES_KANBAN_X" not in names and "LOG" in names


def test_describe_cache(script: Path, tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    now = [1000.0]
    cache = DescribeCache(tmp_path / "cache.json", clock=lambda: now[0])
    p = mk(script, env={"LOG": str(log)})
    first = p.describe(cache)
    assert first is not None and first["tool"]["version"] == "1.0.0"
    assert p.describe(cache) == first
    assert len(log.read_text().splitlines()) == 1  # hit: no new call
    now[0] += 7200  # stale, but the version probe matches: still no describe call
    assert p.describe(cache) == first
    assert len(log.read_text().splitlines()) == 1
    other = Participant("example-tool", [sys.executable, str(script), "--x"], env=p.env)
    other.describe(cache)  # different command: miss
    assert len(log.read_text().splitlines()) == 2


def test_describe_cache_version_change(script: Path, tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    now = [1000.0]
    cache_file = tmp_path / "cache.json"
    cache = DescribeCache(cache_file, clock=lambda: now[0])
    p = mk(script, env={"LOG": str(log)})
    p.describe(cache)
    data = json.loads(cache_file.read_text())
    for v in data.values():
        v["version"] = "0.9"
    cache_file.write_text(json.dumps(data))
    now[0] += 7200
    p.describe(cache)
    assert len(log.read_text().splitlines()) == 2


def test_describe_failure_returns_none(script: Path, tmp_path: Path) -> None:
    assert mk(script, "error").describe(DescribeCache(tmp_path / "c.json")) is None
    assert mk(script, "error").describe() is None


def test_negotiate() -> None:
    assert negotiate({"min": "1.0", "max": "1.0"}) == "1.0"
    assert negotiate({"min": "1.0", "max": "1.x"}) == "1.0"
    for bad in ({"min": "2.0", "max": "2.x"}, {"min": "0.1", "max": "0.9"}, {"min": "1.1", "max": "1.5"}, {}):
        with pytest.raises(SvcError) as ei:
            negotiate(bad)
        assert ei.value.code == "incompatible"


def test_effective_ready_types() -> None:
    r = CallResult("n", "prepare", False, None, Error("timeout", "x"), None, 1.0, True)
    assert effective_ready(r) is True
    ok = CallResult("n", "prepare", True, Response("1", True, {}), None, 0, 0.1, False)
    assert effective_ready(ok) is True

"""Tests for cli_svckit.testing and the toy participant example."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

from cli_svckit.contract.envelope import Error, SvcError
from cli_svckit.testing import FakeParticipant, assert_conforms, make_op, temp_lease_dir

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOY = ROOT / "examples" / "toy_participant.py"


def _toy_cmd(state: pathlib.Path) -> list[str]:
    return [sys.executable, str(TOY), "--state", str(state)]


def test_conforms_on_toy(tmp_path):
    report = assert_conforms(_toy_cmd(tmp_path / "state"))
    assert report.ok
    assert set(report.methods) == {"health", "prepare", "complete"}
    assert report.describe["coordination"]["watches_lease"] is True


def test_conforms_methods_subset(tmp_path):
    report = assert_conforms(_toy_cmd(tmp_path / "state"), methods=["health"])
    assert report.methods == ["health"]


def test_toy_prepare_busy_exit_code(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "busy").write_text("")
    req = json.dumps({"svckit": "1.0", "id": "r1", "method": "prepare", "params": {"op": {"id": "op-1"}, "poll": 0}})
    done = subprocess.run([*_toy_cmd(state), "svc", "prepare", "--json"], input=req, capture_output=True, text=True)
    assert done.returncode == 3
    assert json.loads(done.stdout.splitlines()[-1])["result"]["state"] == "busy"
    # busy is still a conforming answer
    assert_conforms(_toy_cmd(state))


BROKEN = """
import json, sys
method = sys.argv[-2]
req = json.loads(sys.stdin.read() or "{}")
if method == "describe":
    out = {"svckit": "1.0", "id": req["id"], "ok": True,
           "result": {"tool": {"name": "broken"}, "contract": {"min": "1.0", "max": "1.0"},
                      "methods": ["describe", "health", "prepare", "complete", "bogus"], "services": []}}
elif method == "health":
    out = {"svckit": "1.0", "id": "wrong", "ok": True, "result": {"status": "fail", "live": True,
           "ready": True, "checks": {}}}
elif method == "prepare":
    out = {"svckit": "1.0", "id": req["id"], "ok": True, "result": {"state": "busy", "op_id": "x",
           "quiesced": False, "in_flight": []}}
else:
    print("not json at all")
    sys.exit(0)
print(json.dumps(out))
"""


def test_conformance_reports_violations(tmp_path):
    script = tmp_path / "broken.py"
    script.write_text(BROKEN)
    with pytest.raises(AssertionError) as info:
        assert_conforms([sys.executable, str(script), "x"])
    msg = str(info.value)
    assert "describe.result.tool: missing required key 'version'" in msg
    assert "'bogus' is not a contract method" in msg
    assert "health: response id 'wrong' does not echo" in msg
    assert "health: exit code 0, expected 1" in msg
    assert "prepare: exit code 0, expected 3" in msg
    assert "complete: stdout has no JSON object line" in msg


def test_conformance_missing_command(tmp_path):
    with pytest.raises(AssertionError, match="cannot run command"):
        assert_conforms([str(tmp_path / "nope")])


def test_fake_participant_records_and_repeats_last():
    fake = FakeParticipant("alice", {"prepare": [{"state": "busy", "in_flight": []}, {"state": "ready"}]})
    first = fake.call("prepare", {"op": 1})
    second = fake.call("prepare")
    third = fake.call("prepare")
    assert first.rc == 3 and first.ok
    assert second.response.result == {"state": "ready"} and third.rc == 0
    assert fake.calls == [("prepare", {"op": 1}), ("prepare", {}), ("prepare", {})]
    assert fake.calls_to("prepare")[0] == {"op": 1}


def test_fake_participant_errors_and_defaults():
    fake = FakeParticipant(
        "bob",
        {"health": [Error("timeout", "slow")], "complete": [SvcError("forbidden", "no")]},
        fail_policy="closed",
        coordination={"kinds": ["service.*"], "services": ["svc"]},
    )
    t = fake.call("health")
    assert t.timed_out and not t.ok and t.rc == 75 and t.fail_policy == "closed"
    assert fake.call("complete").error.code == "forbidden"
    assert fake.call("doctor").error.code == "unsupported"
    desc = fake.describe()
    assert desc["coordination"]["kinds"] == ["service.*"]
    assert fake.services == ["svc"]


def test_fake_participant_callable():
    fake = FakeParticipant("c", lambda method, params: {"echo": method, **params})
    assert fake.call("health", {"a": 1}).response.result == {"echo": "health", "a": 1}


def test_temp_lease_dir_and_make_op(tmp_path):
    lease_dir = temp_lease_dir(tmp_path)
    assert os.path.isdir(lease_dir.path)
    op = make_op(targets=("svc", "other"), reason="r")
    assert op.kind == "service.restart" and op.targets == ["svc", "other"] and op.id.startswith("op-")
    lease = lease_dir.create(op)
    assert [x.op.id for x in lease_dir.list_active()] == [op.id]
    assert lease.phase == "prepare"


def _read_events(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_toy_daemon_logs_events(tmp_path):
    state = tmp_path / "state"
    lease_dir = temp_lease_dir(tmp_path)
    env = {**os.environ, "SVCKIT_WATCH_BACKEND": "poll"}
    proc = subprocess.Popen(
        [sys.executable, str(TOY), "daemon", "--lease-dir", lease_dir.path, "--state", str(state)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        op = make_op(kind="service.restart", targets=("example-svc",))
        time.sleep(0.5)
        lease_dir.create(op)
        events = state / "events.jsonl"
        deadline = time.time() + 10
        while time.time() < deadline and len(_read_events(events)) < 1:
            time.sleep(0.1)
        lease_dir.complete(op.id, "ok", {})
        while time.time() < deadline and len(_read_events(events)) < 2:
            time.sleep(0.1)
        got = _read_events(events)
        assert [(e["event"], e["op_id"]) for e in got] == [("prepare", op.id), ("complete", op.id)]
        assert all(isinstance(e["t"], float) for e in got)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

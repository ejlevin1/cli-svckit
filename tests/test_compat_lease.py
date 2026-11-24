"""Lease format v1 compatibility (policy P2): current code reads every stored v1 sample with the same meaning.

Samples live in ``tests/compat/lease-v1/`` (synthetic values; ``owner_uid`` is replaced with the current uid at test
time). Every test uses a temp lease directory and a fake clock.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from cli_svckit.coord.lease import Lease, LeaseDir

ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = ROOT / "tests" / "compat" / "lease-v1"
FROZEN_LEASE_SCHEMA = ROOT / "tests" / "compat" / "contract-1.0" / "lease.json"
NOW = 1500.0

# What each v1 sample means, written by hand: (op id, kind, phase, phase_at, outcome, expires_at, dir, active@NOW).
EXPECTED: dict[str, tuple[str, str, str, float, str | None, float, str, bool]] = {
    "prepare": ("op-20260101T120000-prep", "update", "prepare", 1000.0, None, 2000.0, "active", True),
    "running": ("op-20260101T120000-run", "restart", "running", 1150.0, None, 2500.0, "active", True),
    "expired": ("op-20260101T120000-stale", "restart", "prepare", 800.0, None, 1200.0, "active", False),
    "complete": ("op-20260101T120000-done", "update", "complete", 1250.0, "ok", 1800.0, "done", False),
    "cleared": ("op-20260101T120000-clear", "update", "complete", 1300.0, "aborted", 5000.0, "done", False),
}


class FakeClock:
    """A settable clock."""

    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def load_sample(name: str) -> dict[str, Any]:
    """Return sample ``name`` with ``owner_uid`` set to the uid running the tests."""
    data: dict[str, Any] = json.loads((SAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    data["owner_uid"] = os.geteuid()
    return data


def install(ld: LeaseDir, name: str, data: dict[str, Any] | None = None) -> Path:
    """Write sample ``name`` (or ``data``) into the lease dir or ``done/`` as ``<op id>.json``, mode 0600."""
    data = load_sample(name) if data is None else data
    where = EXPECTED[name][6]
    folder = Path(ld.path) if where == "active" else Path(ld.done_path)
    path = folder / f"{data['op']['id']}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ld(tmp_path: Path, clock: FakeClock) -> LeaseDir:
    d = LeaseDir(tmp_path / "leases", clock=clock)
    d.ensure()
    return d


def test_samples_present_and_valid_v1() -> None:
    """Every expected sample exists, is a v1 file and validates under the frozen contract 1.0 lease schema."""
    assert sorted(p.stem for p in SAMPLES_DIR.glob("*.json")) == sorted(EXPECTED)
    validator = Draft202012Validator(json.loads(FROZEN_LEASE_SCHEMA.read_text(encoding="utf-8")))
    for name in EXPECTED:
        data = load_sample(name)
        assert data["v"] == 1
        errors = [e.message for e in validator.iter_errors(data)]
        assert not errors, f"lease-v1/{name}.json is not a valid v1 lease: {errors}"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sample_reads_with_same_meaning(ld: LeaseDir, name: str) -> None:
    """``LeaseDir.get`` returns the sample with the op, phase, outcome and facts it was written with."""
    op_id, kind, phase, phase_at, outcome, expires_at, where, active = EXPECTED[name]
    raw = load_sample(name)
    path = install(ld, name, raw)

    lease = ld.get(op_id)
    assert lease is not None, f"lease-v1/{name}.json was ignored by the current reader"
    assert lease.path == str(path)
    assert (lease.op.id, lease.op.kind, lease.op.targets) == (op_id, kind, ["com.example.worker"])
    assert (lease.op.started_at, lease.op.deadline, lease.op.expires_at) == (
        raw["op"]["started_at"],
        raw["op"]["deadline"],
        expires_at,
    )
    assert lease.op.reason == "example-tool update"
    assert lease.op.by == {"tool": "example-tool", "user": "alice", "pid": 4321}
    assert lease.op.affected_pids == [4400, 4401]
    assert (lease.op.wait_for, lease.op.now) == ("interrupted", False)
    assert (lease.phase, lease.phase_at, lease.outcome) == (phase, phase_at, outcome)
    assert lease.facts == raw["facts"]
    assert lease.owner_uid == os.geteuid()
    assert lease.is_active(NOW) is active
    # the in-memory model writes back the same v1 meaning (unknown fields are dropped, known ones kept)
    known = {k: v for k, v in raw.items() if not k.startswith("x_")}
    back = lease.to_dict()
    assert back["v"] == 1
    assert {k: back[k] for k in known if k != "op"} == {k: v for k, v in known.items() if k != "op"}
    assert {k: back["op"][k] for k in raw["op"] if not k.startswith("x_")} == {
        k: v for k, v in raw["op"].items() if not k.startswith("x_")
    }


def test_specific_facts_keep_their_meaning(ld: LeaseDir) -> None:
    """Completion facts and the human-clear marker read back as written."""
    for name in ("complete", "cleared"):
        install(ld, name)
    done = ld.get(EXPECTED["complete"][0])
    assert done is not None and done.facts is not None
    target = done.facts["targets"]["com.example.worker"]
    assert (target["old_pid"], target["new_pid"], target["restarted"], target["healthy"]) == (4400, 4500, True, True)
    assert (done.facts["capped"], done.facts["forced_now"]) == (False, False)
    cleared = ld.get(EXPECTED["cleared"][0])
    assert cleared is not None and cleared.facts == {"cleared_by_human": True, "reason": "stuck in prepare"}


def test_unknown_fields_are_ignored() -> None:
    """The running sample carries unknown top-level and op fields; the reader accepts it."""
    raw = load_sample("running")
    assert "x_future_field" in raw and "x_future_op_field" in raw["op"]
    lease = Lease.from_dict(raw)
    assert lease.phase == "running" and lease.op.id == EXPECTED["running"][0]


def test_activity_with_fake_clock(ld: LeaseDir, clock: FakeClock) -> None:
    """Active/expired semantics of the v1 samples hold as the fake clock moves."""
    for name in EXPECTED:
        install(ld, name)

    def active_ids() -> list[str]:
        return [lease.op.id for lease in ld.list_active()]

    assert active_ids() == [EXPECTED["prepare"][0], EXPECTED["running"][0]]  # oldest started first
    assert [t.op.id for t in ld.tombstones()] == [EXPECTED["complete"][0], EXPECTED["cleared"][0]]

    clock.t = 2000.0  # prepare's expires_at: no longer active (expires_at must be > now)
    assert active_ids() == [EXPECTED["running"][0]]
    clock.t = 2500.0
    assert active_ids() == []
    clock.t = 1999.0
    assert active_ids() == [EXPECTED["prepare"][0], EXPECTED["running"][0]]


def test_expire_stale_moves_only_the_expired_sample(ld: LeaseDir, clock: FakeClock) -> None:
    """``expire_stale`` at NOW tombstones the expired sample with outcome ``expired`` and keeps its facts."""
    for name in EXPECTED:
        install(ld, name)
    stale_id = EXPECTED["expired"][0]
    assert ld.expire_stale() == [stale_id]
    tomb = ld.get(stale_id)
    assert tomb is not None
    assert tomb.path == os.path.join(ld.done_path, f"{stale_id}.json")
    assert (tomb.phase, tomb.outcome, tomb.facts, tomb.phase_at) == ("complete", "expired", None, NOW)
    assert [lease.op.id for lease in ld.list_active()] == [EXPECTED["prepare"][0], EXPECTED["running"][0]]


def test_tombstones_prune_by_age(ld: LeaseDir, clock: FakeClock) -> None:
    """v1 tombstones age out by ``phase_at`` exactly as stored."""
    install(ld, "complete")
    install(ld, "cleared")
    clock.t = 1300.0 + 100.0
    assert sorted(ld.prune_tombstones(max_age_s=100.0)) == [EXPECTED["complete"][0]]
    assert ld.get(EXPECTED["cleared"][0]) is not None


def _as_v2(name: str) -> dict[str, Any]:
    data = load_sample(name)
    data["v"] = 2
    data["new_in_v2"] = {"meaning": "unknown to a v1 reader"}
    return data


def test_v2_file_is_ignored_and_never_deleted(
    ld: LeaseDir, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``v: 2`` lease is not active for a v1 reader and survives expire_stale, prune and clear."""
    active = install(ld, "prepare", _as_v2("prepare"))
    stale = install(ld, "expired", _as_v2("expired"))
    done = install(ld, "complete", _as_v2("complete"))
    install(ld, "running")  # a v1 neighbour still works next to them

    assert [lease.op.id for lease in ld.list_active()] == [EXPECTED["running"][0]]
    assert ld.get(EXPECTED["prepare"][0]) is None
    assert ld.tombstones() == []

    clock.t = 10_000.0
    assert ld.expire_stale() == [EXPECTED["running"][0]]
    clock.t = 20_000.0
    assert ld.prune_tombstones(max_age_s=1.0) == [EXPECTED["running"][0]]
    assert ld.clear(EXPECTED["prepare"][0]) is None
    for path in (active, stale, done):
        assert path.is_file(), f"{path.name}: a v2 lease file was deleted by a v1 reader"
        assert json.loads(path.read_text(encoding="utf-8"))["v"] == 2
    assert "newer than 1" in capsys.readouterr().err


def test_samples_keep_the_placeholder_uid() -> None:
    """The stored samples keep their placeholder uid: tests substitute the real one on a copy, never in place."""
    for name in EXPECTED:
        assert json.loads((SAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))["owner_uid"] == 4242

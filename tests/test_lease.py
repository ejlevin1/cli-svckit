"""Tests for the lease directory (temp dirs and a fake clock only)."""

from __future__ import annotations

import json
import os
import stat

import pytest

from cli_svckit.contract.envelope import Op, SvcError
from cli_svckit.coord import lease as lease_mod
from cli_svckit.coord.lease import PHASES, Lease, LeaseDir


class FakeClock:
    """A settable clock."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_op(op_id: str = "op-20260101T120000-a1b2", *, expires_at: float = 2000.0, **kw: object) -> Op:
    return Op(
        id=op_id,
        kind=kw.pop("kind", "update"),
        targets=kw.pop("targets", ["com.example.worker"]),
        started_at=1000.0,
        deadline=1300.0,
        expires_at=expires_at,
        **kw,
    )  # type: ignore[arg-type]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ld(tmp_path, clock) -> LeaseDir:
    return LeaseDir(tmp_path / "leases", clock=clock)


def test_phases_constant():
    assert PHASES == ("prepare", "running", "complete")


def test_ensure_modes(ld):
    ld.ensure()
    assert stat.S_IMODE(os.stat(ld.path).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(os.path.join(ld.path, "done")).st_mode) == 0o700


def test_create_get_phase_complete_tombstone(ld, clock):
    op = make_op()
    lease = ld.create(op)
    assert lease.phase == "prepare" and lease.phase_at == 1000.0 and lease.outcome is None
    assert lease.owner_uid == os.geteuid()
    assert lease.path == os.path.join(ld.path, f"{op.id}.json")
    assert stat.S_IMODE(os.stat(lease.path).st_mode) == 0o600
    raw = json.loads(open(lease.path).read())
    assert set(raw) == {"v", "op", "phase", "phase_at", "outcome", "facts", "owner_uid"}
    assert raw["v"] == 1 and raw["op"]["affected"] == {"pids": None}

    got = ld.get(op.id)
    assert got is not None and got.op.id == op.id and got.phase == "prepare"
    assert [le.op.id for le in ld.list_active()] == [op.id]

    clock.t = 1010.0
    running = ld.set_phase(op.id, "running", facts={"step": 1})
    assert running.phase == "running" and running.phase_at == 1010.0 and running.facts == {"step": 1}
    assert ld.get(op.id).phase == "running"

    clock.t = 1020.0
    done = ld.complete(op.id, "ok", {"restarted": ["com.example.worker"]})
    assert done.phase == "complete" and done.outcome == "ok" and done.phase_at == 1020.0
    assert done.path == os.path.join(ld.path, "done", f"{op.id}.json")
    assert not os.path.exists(os.path.join(ld.path, f"{op.id}.json"))
    assert stat.S_IMODE(os.stat(done.path).st_mode) == 0o600
    assert ld.list_active() == []
    tombs = ld.tombstones()
    assert [t.op.id for t in tombs] == [op.id] and tombs[0].facts == {"restarted": ["com.example.worker"]}
    assert ld.get(op.id).path == done.path
    # complete again is idempotent
    assert ld.complete(op.id, "failed", None).outcome == "ok"
    assert ld.tombstones(since=1021.0) == []


def test_set_phase_complete_tombstones(ld):
    op = make_op()
    ld.create(op)
    done = ld.set_phase(op.id, "complete", outcome="partial")
    assert done.outcome == "partial" and "done" in done.path
    with pytest.raises(SvcError):
        ld.set_phase(op.id, "running")
    with pytest.raises(SvcError):
        ld.set_phase(op.id, "bogus")


def test_lease_roundtrip():
    lease = Lease(make_op(), "running", 5.0, None, None, 501, "")
    back = Lease.from_dict(lease.to_dict())
    assert back == lease


def test_atomic_write_failure_keeps_old_content(ld, monkeypatch):
    op = make_op()
    ld.create(op)
    path = os.path.join(ld.path, f"{op.id}.json")
    before = open(path).read()

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(lease_mod.os, "replace", boom)
    with pytest.raises(OSError):
        ld.set_phase(op.id, "running")
    monkeypatch.undo()
    assert open(path).read() == before
    assert ld.get(op.id).phase == "prepare"
    leftovers = [n for n in os.listdir(ld.path) if n.startswith(".tmp-")]
    assert leftovers == []


def test_create_failure_leaves_no_file(ld, monkeypatch):
    monkeypatch.setattr(lease_mod.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        ld.create(make_op())
    monkeypatch.undo()
    assert [n for n in os.listdir(ld.path) if n.endswith(".json")] == []


def test_create_refuses_expired_op(ld):
    with pytest.raises(SvcError) as ei:
        ld.create(make_op(expires_at=1000.0))
    assert ei.value.code == "invalid"


def test_create_refuses_bad_id_and_phase(ld):
    with pytest.raises(SvcError):
        ld.create(make_op("../evil"))
    with pytest.raises(SvcError):
        ld.create(make_op(), phase="complete")


def test_duplicate_create_is_idempotent(ld, clock):
    op = make_op()
    first = ld.create(op)
    clock.t = 1005.0
    ld.set_phase(op.id, "running")
    again = ld.create(make_op(op.id, expires_at=9999.0))
    assert again.phase == "running" and again.op.expires_at == first.op.expires_at
    assert len(ld.list_active()) == 1


def test_create_after_tombstone_refused(ld):
    op = make_op()
    ld.create(op)
    ld.complete(op.id, "ok", None)
    with pytest.raises(SvcError):
        ld.create(op)


def test_expiry(ld, clock):
    ld.create(make_op("op-a", expires_at=1100.0))
    ld.create(make_op("op-b", expires_at=3000.0))
    clock.t = 1100.0
    assert [le.op.id for le in ld.list_active()] == ["op-b"]
    assert [le.op.id for le in ld.list_active(now=1050.0)] == ["op-a", "op-b"]
    assert ld.get("op-a") is not None  # still on disk until expired
    assert ld.expire_stale() == ["op-a"]
    tomb = ld.get("op-a")
    assert tomb.outcome == "expired" and tomb.phase == "complete" and "done" in tomb.path
    assert ld.expire_stale() == []


def test_expire_stale_moves_leftover_complete(ld):
    op = make_op()
    lease = ld.create(op)
    data = lease.to_dict()
    data.update(phase="complete", outcome="ok")
    with open(lease.path, "w") as f:
        json.dump(data, f)
    assert ld.list_active() == []
    assert ld.expire_stale() == [op.id]
    assert ld.get(op.id).outcome == "ok"


def test_clear(ld):
    op = make_op()
    ld.create(op)
    tomb = ld.clear(op.id, "stuck controller")
    assert tomb.outcome == "aborted"
    assert tomb.facts == {"cleared_by_human": True, "reason": "stuck controller"}
    assert ld.list_active() == [] and [t.op.id for t in ld.tombstones()] == [op.id]
    assert ld.clear("op-missing") is None


def test_prune_tombstones(ld, clock):
    for oid in ("op-old", "op-new"):
        ld.create(make_op(oid, expires_at=10**6))
    ld.complete("op-old", "ok", None)
    clock.t = 50000.0
    ld.complete("op-new", "ok", None)
    clock.t = 1000.0 + 86400.0 + 1
    assert ld.prune_tombstones() == ["op-old"]
    assert [t.op.id for t in ld.tombstones()] == ["op-new"]


def test_owner_uid_mismatch_ignored(ld, capsys):
    lease = ld.create(make_op())
    data = lease.to_dict()
    data["owner_uid"] = os.geteuid() + 4242
    with open(lease.path, "w") as f:
        json.dump(data, f)
    assert ld.list_active() == []
    assert ld.get(lease.op.id) is None
    ld.list_active()
    err = capsys.readouterr().err
    assert err.count("does not match file owner") == 1  # logged once
    assert os.path.exists(lease.path)  # readers never delete


def test_file_owned_by_other_uid_ignored(tmp_path, clock, capsys):
    writer = LeaseDir(tmp_path / "l", clock=clock)
    writer.create(make_op())
    reader = LeaseDir(tmp_path / "l", uid=os.geteuid() + 4242, clock=clock)
    if os.geteuid() == 0:
        pytest.skip("root-owned files are always trusted")
    assert reader.list_active() == []
    assert "owned by uid" in capsys.readouterr().err


def test_corrupt_and_future_files_ignored(ld, capsys):
    ld.create(make_op("op-good"))
    bad = os.path.join(ld.path, "op-bad.json")
    with open(bad, "w") as f:
        f.write("{not json")
    future = ld.create(make_op("op-future")).to_dict()
    future["v"] = 2
    with open(os.path.join(ld.path, "op-future.json"), "w") as f:
        json.dump(future, f)
    assert [le.op.id for le in ld.list_active()] == ["op-good"]
    assert ld.expire_stale() == []
    assert os.path.exists(bad) and os.path.exists(os.path.join(ld.path, "op-future.json"))
    assert "not valid JSON" in capsys.readouterr().err
    with pytest.raises(SvcError):
        ld.create(make_op("op-bad"))


def test_overlapping_ops(ld, clock):
    ld.create(make_op("op-a", targets=["com.example.a"]))
    clock.t = 1001.0
    ld.create(make_op("op-b", targets=["com.example.b"]))
    assert [le.op.id for le in ld.list_active()] == ["op-a", "op-b"]
    ld.complete("op-a", "ok", None)
    assert [le.op.id for le in ld.list_active()] == ["op-b"]
    assert [t.op.id for t in ld.tombstones()] == ["op-a"]

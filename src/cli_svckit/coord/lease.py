"""Durable lease directory: one JSON file per active operation, tombstones in ``done/``.

A controller writes ``<dir>/<op_id>.json`` before it restarts or updates anything; participant daemons watch the
directory and quiesce while a matching lease is active. When the operation ends the file is rewritten with phase
``complete`` and moved to ``<dir>/done/<op_id>.json`` (a tombstone, kept 24 h). Leases past ``op.expires_at`` are
stale and count as inactive (fail open for a dead controller).

Trust: a lease counts only when the file is owned (``st_uid``) by our own uid or root, and its JSON ``owner_uid``
agrees with the file owner. Anything else, and any file that does not parse or has a newer format version, is
ignored (with one warning on stderr) but never deleted by a reader.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from cli_svckit.contract.envelope import Op, SvcError

PHASES = ("prepare", "running", "complete")
LEASE_VERSION = 1
DONE_DIR = "done"
LOCK_FILE = ".lock"
TOMBSTONE_MAX_AGE_S = 86400.0

_OP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


def _warn(msg: str) -> None:
    """Write one warning line to stderr (looked up at call time so tests can capture it)."""
    print(f"cli_svckit: {msg}", file=sys.stderr, flush=True)


def _check_op_id(op_id: str) -> str:
    """Return ``op_id`` when it is safe as a file name; raise ``SvcError("invalid")`` otherwise."""
    if not isinstance(op_id, str) or not _OP_ID.match(op_id):
        raise SvcError("invalid", f"bad op id for a lease file: {op_id!r}")
    return op_id


@dataclass
class Lease:
    """One lease file as read from (or written to) the lease directory.

    ``op`` is the announced operation, ``phase`` one of ``PHASES`` and ``phase_at`` when it was entered.
    ``outcome`` and ``facts`` are set when the op completes. ``owner_uid`` is the uid that wrote the file and
    ``path`` where the file was found (the active file or its ``done/`` tombstone).
    """

    op: Op
    phase: str
    phase_at: float
    outcome: str | None
    facts: dict[str, Any] | None
    owner_uid: int
    path: str

    def to_dict(self) -> dict[str, Any]:
        """Return the lease file JSON form (``path`` is not stored)."""
        return {
            "v": LEASE_VERSION,
            "op": self.op.to_dict(),
            "phase": self.phase,
            "phase_at": self.phase_at,
            "outcome": self.outcome,
            "facts": None if self.facts is None else dict(self.facts),
            "owner_uid": self.owner_uid,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], path: str = "") -> Lease:
        """Build a lease from its file JSON. Raises ``SvcError("invalid")`` or ``("incompatible")`` (``v`` > 1)."""
        if not isinstance(d, dict):
            raise SvcError("invalid", "lease must be a JSON object")
        v = d.get("v")
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise SvcError("invalid", f"lease has a bad format version: {v!r}")
        if v > LEASE_VERSION:
            raise SvcError("incompatible", f"lease format version {v} is newer than {LEASE_VERSION}")
        phase = d.get("phase")
        if phase not in PHASES:
            raise SvcError("invalid", f"lease has an unknown phase: {phase!r}")
        owner = d.get("owner_uid")
        if not isinstance(owner, int) or isinstance(owner, bool) or owner < 0:
            raise SvcError("invalid", "lease.owner_uid must be a non-negative integer")
        outcome = d.get("outcome")
        if outcome is not None and not isinstance(outcome, str):
            raise SvcError("invalid", "lease.outcome must be a string or null")
        facts = d.get("facts")
        if facts is not None and not isinstance(facts, dict):
            raise SvcError("invalid", "lease.facts must be an object or null")
        try:
            phase_at = float(d.get("phase_at") or 0.0)
        except (TypeError, ValueError) as exc:
            raise SvcError("invalid", f"lease.phase_at is malformed: {exc}") from exc
        return cls(
            op=Op.from_dict(d.get("op")),  # type: ignore[arg-type]
            phase=phase,
            phase_at=phase_at,
            outcome=outcome,
            facts=facts,
            owner_uid=owner,
            path=path,
        )

    def is_active(self, now: float) -> bool:
        """Return True while the op is not complete and its lease has not expired at ``now``."""
        return self.phase != "complete" and self.op.expires_at > now


class LeaseDir:
    """A lease directory at ``path`` (mode 0700, files 0600, tombstones in ``done/``).

    ``uid`` is the uid whose leases we trust besides root (default: our effective uid). ``clock`` supplies
    every timestamp, so tests can use a fake clock. Writes are atomic (temp file in the same directory, fsync,
    ``os.replace``); ``create`` and the other writers hold an ``flock`` on ``<path>/.lock``.
    """

    def __init__(self, path: str | os.PathLike[str], *, uid: int | None = None, clock: Callable[[], float] = time.time):
        self.path = os.fspath(path)
        self.uid = os.geteuid() if uid is None else uid
        self.clock = clock
        self._warned: set[tuple[str, str]] = set()

    # -- paths -------------------------------------------------------------------------------------------

    @property
    def done_path(self) -> str:
        """The tombstone directory ``<path>/done``."""
        return os.path.join(self.path, DONE_DIR)

    def _active_file(self, op_id: str) -> str:
        return os.path.join(self.path, f"{_check_op_id(op_id)}.json")

    def _done_file(self, op_id: str) -> str:
        return os.path.join(self.done_path, f"{_check_op_id(op_id)}.json")

    def _trusted_uids(self) -> set[int]:
        return {self.uid, 0}

    # -- low-level io ------------------------------------------------------------------------------------

    def ensure(self) -> None:
        """Create the directory and ``done/`` if missing and make sure both are mode 0700."""
        for d in (self.path, self.done_path):
            old = os.umask(0o077)
            try:
                os.makedirs(d, mode=0o700, exist_ok=True)
            finally:
                os.umask(old)
            os.chmod(d, 0o700)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive ``flock`` on ``<path>/.lock`` for the duration of the block."""
        self.ensure()
        old = os.umask(0o077)
        try:
            fd = os.open(os.path.join(self.path, LOCK_FILE), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        finally:
            os.umask(old)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _write(self, target: str, lease: Lease) -> None:
        """Atomically write ``lease`` to ``target``: temp file in the same dir, fsync, chmod 0600, os.replace."""
        data = json.dumps(lease.to_dict(), sort_keys=True).encode("utf-8") + b"\n"
        directory = os.path.dirname(target)
        old = os.umask(0o077)
        try:
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
        finally:
            os.umask(old)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        _fsync_dir(directory)

    def _warn_once(self, path: str, reason: str) -> None:
        key = (path, reason)
        if key not in self._warned:
            self._warned.add(key)
            _warn(f"ignoring lease file {path}: {reason}")

    def _read(self, path: str) -> Lease | None:
        """Read and trust-check one lease file; return None (warning once) when it must be ignored."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._warn_once(path, f"cannot open ({exc.strerror})")
            return None
        try:
            st = os.fstat(fd)
            with os.fdopen(fd, "rb") as f:
                fd = -1
                raw = f.read()
        finally:
            if fd >= 0:
                os.close(fd)
        if st.st_uid not in self._trusted_uids():
            self._warn_once(path, f"owned by uid {st.st_uid}, not {self.uid} or root")
            return None
        try:
            lease = Lease.from_dict(json.loads(raw.decode("utf-8")), path)
        except (ValueError, UnicodeDecodeError) as exc:
            self._warn_once(path, f"not valid JSON ({exc.__class__.__name__})")
            return None
        except SvcError as exc:
            self._warn_once(path, exc.message)
            return None
        if lease.owner_uid != st.st_uid:
            self._warn_once(path, f"owner_uid {lease.owner_uid} does not match file owner {st.st_uid}")
            return None
        return lease

    def _scan(self, directory: str) -> list[Lease]:
        """Return the trusted, parseable leases in ``directory`` (temp and hidden files skipped)."""
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            return []
        out: list[Lease] = []
        for name in names:
            if name.startswith(".") or not name.endswith(".json"):
                continue
            lease = self._read(os.path.join(directory, name))
            if lease is not None and lease.op.id == name[: -len(".json")]:
                out.append(lease)
            elif lease is not None:
                self._warn_once(lease.path, f"op id {lease.op.id!r} does not match the file name")
        return out

    def _tombstone(self, lease: Lease, outcome: str | None, facts: dict[str, Any] | None) -> Lease:
        """Rewrite ``lease`` as complete in place, then move it to ``done/``. Caller holds the lock."""
        done = dataclasses.replace(
            lease,
            phase="complete",
            phase_at=self.clock(),
            outcome=outcome,
            facts=None if facts is None else dict(facts),
            owner_uid=os.geteuid(),
        )
        self._write(lease.path, done)
        target = self._done_file(lease.op.id)
        os.replace(lease.path, target)
        _fsync_dir(self.done_path)
        _fsync_dir(self.path)
        return dataclasses.replace(done, path=target)

    # -- public api --------------------------------------------------------------------------------------

    def create(self, op: Op, phase: str = "prepare") -> Lease:
        """Write a new lease for ``op`` and return it.

        Idempotent: when an active lease for the same op id already exists it is returned unchanged. Refuses
        (``SvcError("invalid")``) an op whose ``expires_at`` is not in the future, a phase other than prepare or
        running, an op id that already has a tombstone, and an existing file it cannot read or trust.
        """
        _check_op_id(op.id)
        if phase not in PHASES or phase == "complete":
            raise SvcError("invalid", f"a new lease must start in phase prepare or running, not {phase!r}")
        with self._locked():
            now = self.clock()
            path = self._active_file(op.id)
            if os.path.lexists(path):
                existing = self._read(path)
                if existing is None:
                    raise SvcError("invalid", f"a lease file for {op.id} exists but cannot be trusted or read")
                if existing.is_active(now):
                    return existing
            if os.path.lexists(self._done_file(op.id)):
                raise SvcError("invalid", f"op {op.id} has already completed")
            if op.expires_at <= now:
                raise SvcError("invalid", f"op {op.id} expires_at {op.expires_at} is not after now ({now})")
            lease = Lease(
                op=dataclasses.replace(op, lease=path),
                phase=phase,
                phase_at=now,
                outcome=None,
                facts=None,
                owner_uid=os.geteuid(),
                path=path,
            )
            self._write(path, lease)
            return lease

    def get(self, op_id: str) -> Lease | None:
        """Return the lease for ``op_id``: the active file if present, else its tombstone, else None.

        Untrusted or unreadable files count as absent. Expired leases are returned too (check ``is_active``).
        """
        lease = self._read(self._active_file(op_id))
        if lease is None:
            lease = self._read(self._done_file(op_id))
        return lease

    def set_phase(
        self,
        op_id: str,
        phase: str,
        *,
        outcome: str | None = None,
        facts: dict[str, Any] | None = None,
    ) -> Lease:
        """Move the active lease ``op_id`` to ``phase`` (``complete`` behaves like :meth:`complete`).

        ``outcome``/``facts`` replace the stored ones when given. Raises ``SvcError("invalid")`` when there is no
        active lease file for the op or the phase is unknown.
        """
        if phase not in PHASES:
            raise SvcError("invalid", f"unknown lease phase {phase!r}")
        with self._locked():
            lease = self._read(self._active_file(op_id))
            if lease is None or lease.phase == "complete":
                raise SvcError("invalid", f"no active lease for {op_id}")
            new_outcome = lease.outcome if outcome is None else outcome
            new_facts = lease.facts if facts is None else facts
            if phase == "complete":
                return self._tombstone(lease, new_outcome, new_facts)
            updated = dataclasses.replace(
                lease,
                phase=phase,
                phase_at=self.clock(),
                outcome=new_outcome,
                facts=None if new_facts is None else dict(new_facts),
            )
            self._write(lease.path, updated)
            return updated

    def complete(self, op_id: str, outcome: str, facts: dict[str, Any] | None) -> Lease:
        """End ``op_id``: write phase complete with ``outcome``/``facts``, then move it to ``done/<op_id>.json``.

        Calling it again after the op is tombstoned returns the tombstone unchanged. Raises
        ``SvcError("invalid")`` when the op has neither an active lease nor a tombstone.
        """
        with self._locked():
            lease = self._read(self._active_file(op_id))
            if lease is None:
                done = self._read(self._done_file(op_id))
                if done is not None:
                    return done
                raise SvcError("invalid", f"no lease for {op_id}")
            return self._tombstone(lease, outcome, facts)

    def clear(self, op_id: str, reason: str = "") -> Lease | None:
        """Human end of a stuck lease: tombstone it with outcome ``aborted`` and ``cleared_by_human`` facts.

        Works on expired leases too. Returns the tombstone, the existing tombstone when the op had already ended,
        or None when there is no such lease.
        """
        with self._locked():
            lease = self._read(self._active_file(op_id))
            if lease is None:
                return self._read(self._done_file(op_id))
            return self._tombstone(lease, "aborted", {"cleared_by_human": True, "reason": reason})

    def list_active(self, now: float | None = None) -> list[Lease]:
        """Return the trusted leases that are active at ``now`` (default: the clock), oldest first."""
        t = self.clock() if now is None else now
        active = [lease for lease in self._scan(self.path) if lease.is_active(t)]
        return sorted(active, key=lambda le: (le.op.started_at, le.phase_at, le.op.id))

    def tombstones(self, since: float = 0.0) -> list[Lease]:
        """Return the trusted tombstones in ``done/`` whose ``phase_at`` >= ``since``, oldest first."""
        done = [lease for lease in self._scan(self.done_path) if lease.phase_at >= since]
        return sorted(done, key=lambda le: (le.phase_at, le.op.id))

    def expire_stale(self) -> list[str]:
        """Move leases whose ``expires_at`` has passed to ``done/`` with outcome ``expired``; return their ids.

        Leases already in phase complete but left in the active directory (a writer died between the rewrite
        and the move) are moved too, keeping their outcome.
        """
        moved: list[str] = []
        with self._locked():
            now = self.clock()
            for lease in self._scan(self.path):
                if lease.phase == "complete":
                    target = self._done_file(lease.op.id)
                    os.replace(lease.path, target)
                    moved.append(lease.op.id)
                elif lease.op.expires_at <= now:
                    self._tombstone(lease, "expired", lease.facts)
                    moved.append(lease.op.id)
            if moved:
                _fsync_dir(self.done_path)
                _fsync_dir(self.path)
        return moved

    def prune_tombstones(self, max_age_s: float = TOMBSTONE_MAX_AGE_S) -> list[str]:
        """Delete trusted tombstones whose ``phase_at`` is more than ``max_age_s`` ago; return their op ids."""
        pruned: list[str] = []
        with self._locked():
            cutoff = self.clock() - max_age_s
            for lease in self._scan(self.done_path):
                if lease.phase_at < cutoff:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(lease.path)
                    pruned.append(lease.op.id)
        return pruned


def _fsync_dir(directory: str) -> None:
    """Flush a directory entry change to disk (best effort: some filesystems refuse to fsync directories)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)

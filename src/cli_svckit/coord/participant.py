"""Participant side of lease coordination: which ops concern us, and firing quiesce/resume callbacks once.

A participant daemon builds a :class:`Quiescer` over the shared :class:`~cli_svckit.coord.lease.LeaseDir`, calls
:meth:`Quiescer.catch_up` on start and :meth:`Quiescer.refresh` on every watcher event. ``on_prepare(lease)`` runs
once per matching op that becomes active, ``on_complete(lease)`` once per such op when it ends (tombstone appears,
or its lease expires: fail open for a dead controller).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cli_svckit.contract.envelope import Op, matches
from cli_svckit.coord.lease import Lease, LeaseDir

STATE_VERSION = 1


@dataclass
class Interest:
    """The ops a participant cares about: ``kinds`` and ``services`` are shell-style globs.

    An op matches when its kind matches one of ``kinds`` and at least one of its targets matches one of
    ``services``.
    """

    kinds: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)

    def matches(self, op: Op) -> bool:
        """Return True when ``op`` concerns this participant."""
        return matches(op.kind, self.kinds) and any(matches(t, self.services) for t in op.targets)


def active_ops(lease_dir: LeaseDir, interest: Interest, now: float | None = None) -> list[Lease]:
    """Return the active leases in ``lease_dir`` (at ``now``, default the dir's clock) that match ``interest``."""
    return [lease for lease in lease_dir.list_active(now) if interest.matches(lease.op)]


class Quiescer:
    """Turns lease-directory changes into one ``on_prepare`` and one ``on_complete`` call per matching op.

    The op ids already fired are kept in memory and, when ``state_file`` is given, persisted there (JSON,
    written atomically) so a restarted daemon does not fire again. Tombstones are only considered when they are
    newer than the marker: the stored marker from ``state_file``, else the moment this Quiescer was built, so old
    history never fires. Thread-safe; callback exceptions are reported on stderr and do not stop other callbacks.
    """

    def __init__(
        self,
        lease_dir: LeaseDir,
        interest: Interest,
        on_prepare: Callable[[Lease], Any],
        on_complete: Callable[[Lease], Any],
        *,
        state_file: str | os.PathLike[str] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.lease_dir = lease_dir
        self.interest = interest
        self.on_prepare = on_prepare
        self.on_complete = on_complete
        self.state_file = None if state_file is None else os.fspath(state_file)
        self.clock = clock
        self._lock = threading.RLock()
        self._prepared: set[str] = set()
        self._completed: set[str] = set()
        self._seen: dict[str, Lease] = {}
        stored = self._load()
        self._marker: float = self.clock() if stored is None else stored
        self._baseline = self._marker

    # -- state -------------------------------------------------------------------------------------------

    @property
    def prepared(self) -> frozenset[str]:
        """Op ids that got ``on_prepare`` and are not finished yet."""
        with self._lock:
            return frozenset(self._prepared)

    @property
    def completed(self) -> frozenset[str]:
        """Op ids that got ``on_complete`` (kept until their tombstone is pruned)."""
        with self._lock:
            return frozenset(self._completed)

    def _load(self) -> float | None:
        """Read ``state_file``; return its marker, or None when there is no usable state."""
        if self.state_file is None:
            return None
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            print(f"cli_svckit: ignoring quiescer state {self.state_file}: {exc}", file=sys.stderr, flush=True)
            return None
        if not isinstance(data, dict) or data.get("v") != STATE_VERSION:
            print(f"cli_svckit: ignoring quiescer state {self.state_file}: unknown format", file=sys.stderr)
            return None
        self._prepared = {str(x) for x in data.get("prepared") or [] if isinstance(x, str)}
        self._completed = {str(x) for x in data.get("completed") or [] if isinstance(x, str)}
        marker = data.get("marker")
        if isinstance(marker, (int, float)) and not isinstance(marker, bool):
            return float(marker)
        return None

    def _save(self) -> None:
        """Write the state atomically to ``state_file`` (no-op without one; failures are reported, not raised)."""
        if self.state_file is None:
            return
        data = {
            "v": STATE_VERSION,
            "marker": self._marker,
            "prepared": sorted(self._prepared),
            "completed": sorted(self._completed),
        }
        directory = os.path.dirname(os.path.abspath(self.state_file))
        tmp = None
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_file)
            tmp = None
        except OSError as exc:
            print(f"cli_svckit: cannot save quiescer state {self.state_file}: {exc}", file=sys.stderr, flush=True)
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    # -- firing ------------------------------------------------------------------------------------------

    def _fire(self, callback: Callable[[Lease], Any], lease: Lease, what: str) -> None:
        try:
            callback(lease)
        except Exception:
            print(f"cli_svckit: {what} callback failed for {lease.op.id}:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

    def _prepare(self, lease: Lease) -> None:
        self._prepared.add(lease.op.id)
        self._seen[lease.op.id] = lease
        self._fire(self.on_prepare, lease, "on_prepare")

    def _complete(self, lease: Lease) -> None:
        self._prepared.discard(lease.op.id)
        self._completed.add(lease.op.id)
        self._seen.pop(lease.op.id, None)
        self._fire(self.on_complete, lease, "on_complete")

    def refresh(self) -> None:
        """Re-read the lease directory and fire the callbacks that are due. Call it on every watcher event.

        Newly active matching ops get ``on_prepare``. Matching tombstones get ``on_complete`` when we prepared
        for them, or ``on_prepare`` then ``on_complete`` when the op came and went between two refreshes (only
        for tombstones newer than the marker). A prepared op whose lease expired without a tombstone gets
        ``on_complete`` with outcome ``expired``.
        """
        with self._lock:
            before = (frozenset(self._prepared), frozenset(self._completed), self._marker)
            now = self.clock()
            active = active_ops(self.lease_dir, self.interest, now)
            for lease in active:
                oid = lease.op.id
                if oid not in self._prepared and oid not in self._completed:
                    self._prepare(lease)
                else:
                    self._seen[oid] = lease
            tombs = [t for t in self.lease_dir.tombstones() if self.interest.matches(t.op)]
            tomb_ids = {t.op.id for t in tombs}
            for tomb in tombs:
                oid = tomb.op.id
                if oid in self._completed:
                    continue
                if oid in self._prepared:
                    self._complete(tomb)
                elif tomb.phase_at > self._baseline:
                    self._prepare(tomb)
                    self._complete(tomb)
                else:
                    continue
                self._marker = max(self._marker, tomb.phase_at)
            active_ids = {lease.op.id for lease in active}
            for oid in sorted(self._prepared - active_ids - tomb_ids):
                current = self.lease_dir.get(oid)
                if current is not None and current.phase != "complete":
                    # Still there but no longer active: expired (fail open).
                    self._complete(dataclasses.replace(current, outcome="expired"))
                elif current is None:
                    last = self._seen.get(oid)
                    if last is not None:
                        # The lease file vanished (removed by hand): resume rather than stay quiesced.
                        self._complete(dataclasses.replace(last, outcome="missing"))
                    else:
                        self._prepared.discard(oid)
            # Forget completed ids whose files are gone for good (tombstone pruned), so the state stays small.
            for oid in sorted(self._completed - tomb_ids):
                if self.lease_dir.get(oid) is None:
                    self._completed.discard(oid)
            if (frozenset(self._prepared), frozenset(self._completed), self._marker) != before:
                self._save()

    def catch_up(self) -> None:
        """Run once on daemon start: fire for ops already active and for tombstones newer than the stored marker.

        Ids recorded in ``state_file`` as already fired are not fired again.
        """
        self.refresh()

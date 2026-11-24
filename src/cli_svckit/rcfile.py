"""Put a tool's command on PATH (rc-file line or symlink) and remove exactly what was installed.

Install records a receipt; uninstall only removes the line/link the receipt vouches for and refuses
(``SvcError("forbidden")``) when it was edited, duplicated or has no receipt. All rc-file work is done on
bytes so every other byte of the file is left untouched.
"""

from __future__ import annotations

import difflib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli_svckit.contract.envelope import SvcError
from cli_svckit.receipts import Receipts, sha256_text


@dataclass
class Action:
    """One change made (or planned) by an install helper; ``op`` is rc_line, link, remove or write."""

    op: str
    path: str
    line: str | None = None
    target: str | None = None
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the install-result ``actions`` item (unset fields omitted)."""
        d: dict[str, Any] = {"op": self.op, "path": self.path}
        for key in ("line", "target", "sha256"):
            value = getattr(self, key)
            if value is not None:
                d[key] = value
        return d


# ---------------------------------------------------------------- helpers


def _managed(line: str, marker: str) -> str:
    return f"{line}  # {marker}"


def _split(data: bytes) -> list[bytes]:
    """Split on newline keeping line endings; the last item has none when the file lacks a final newline."""
    parts = data.split(b"\n")
    out = [p + b"\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _content(raw: bytes) -> str:
    return raw.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8", "surrogateescape")


def _eol_of(data: bytes) -> bytes:
    i = data.find(b"\n")
    return b"\r\n" if i > 0 and data[i - 1 : i] == b"\r" else b"\n"


def _marked(lines: list[bytes], marker: str) -> list[int]:
    suffix = f"  # {marker}"
    return [i for i, raw in enumerate(lines) if _content(raw).endswith(suffix)]


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    real = Path(os.path.realpath(path))
    real.parent.mkdir(parents=True, exist_ok=True)
    mode = real.stat().st_mode & 0o7777 if real.exists() else 0o644
    tmp = real.with_name(real.name + ".svckit-tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, real)


def _backup(path: Path, clock: Callable[[], float]) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
    dest = path.with_name(f"{path.name}.svckit-backup-{stamp}")
    n = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.svckit-backup-{stamp}-{n}")
        n += 1
    dest.write_bytes(path.read_bytes())
    return dest


def _entries(receipts: Receipts, name: str) -> list[dict[str, Any]]:
    doc = receipts.read(name)
    return list(doc["entries"]) if doc else []


def _store(receipts: Receipts, name: str, entries: list[dict[str, Any]]) -> None:
    if entries:
        receipts.write(name, entries)
    else:
        receipts.remove(name)


def _refuse(message: str, path: Path, found: str) -> SvcError:
    return SvcError("forbidden", message, data={"path": str(path), "found": found})


# ---------------------------------------------------------------- rc line


def install_rc_line(
    rc_path: str | os.PathLike[str],
    line: str,
    marker: str,
    *,
    receipts: Receipts,
    name: str,
    dry_run: bool = False,
    backup: bool = True,
    clock: Callable[[], float] = time.time,
) -> list[Action]:
    """Append ``<line>  # <marker>`` to the rc file (creating it) unless it is already there.

    Idempotent: an identical managed line means no action. A marked line that differs raises ``forbidden``.
    A CRLF file gets a CRLF line; a file without a final newline gets one added first (recorded in the
    receipt so uninstall can restore the exact original bytes). Returns the actions (same on dry run).
    """
    path = Path(rc_path)
    text = _managed(line, marker)
    data = _read(path)
    existing = _marked(_split(data), marker) if data is not None else []
    if existing:
        lines = _split(data or b"")
        if len(existing) == 1 and _content(lines[existing[0]]) == text:
            return []
        raise _refuse("a line with this marker already exists and differs", path, "edited")
    action = Action("rc_line", str(path), line=text, sha256=sha256_text(text))
    if dry_run:
        return [action]
    old = data or b""
    eol = _eol_of(old)
    prefix = bool(old) and not old.endswith(b"\n")
    new = old + (eol if prefix else b"") + text.encode("utf-8", "surrogateescape") + eol
    if data is not None and backup:
        _backup(path, clock)
    _write_atomic(path, new)
    entries = [e for e in _entries(receipts, name) if not (e.get("kind") == "rc_line" and e.get("path") == str(path))]
    entries.append(
        {
            "kind": "rc_line",
            "path": str(path),
            "sha256": action.sha256,
            "line": text,
            "prefix_eol": prefix,
            "created": data is None,
        }
    )
    receipts.write(name, entries)
    return [action]


def uninstall_rc_line(
    rc_path: str | os.PathLike[str],
    marker: str,
    *,
    receipts: Receipts,
    name: str,
    dry_run: bool = False,
    force: bool = False,
    clock: Callable[[], float] = time.time,
) -> list[Action]:
    """Remove only the line carrying ``marker``, after backing the file up.

    Refuses with ``SvcError("forbidden", data={"path", "found"})`` when the receipt is missing, the line
    differs from the receipt's hash (``edited``), or the marker appears more than once (``duplicate``);
    ``force`` overrides this. Nothing else in the file changes. Nothing to remove returns ``[]``.
    """
    path = Path(rc_path)
    entries = _entries(receipts, name)
    entry = next((e for e in entries if e.get("kind") == "rc_line" and e.get("path") == str(path)), None)
    data = _read(path)
    lines = _split(data) if data is not None else []
    idx = _marked(lines, marker)
    if not idx:
        if entry is None:
            return []
        if not force:
            raise _refuse("the managed line is not in the file", path, "missing")
        if not dry_run:
            _store(receipts, name, [e for e in entries if e is not entry])
        return []
    if not force:
        if len(idx) > 1:
            raise _refuse("the marker appears more than once", path, "duplicate")
        if entry is None:
            raise _refuse("no receipt for this line", path, "missing")
        if sha256_text(_content(lines[idx[0]])) != entry.get("sha256"):
            raise _refuse("the managed line was edited", path, "edited")
    texts = [_content(lines[i]) for i in idx]
    actions = [Action("remove", str(path), line=t, sha256=sha256_text(t)) for t in texts]
    if dry_run:
        return actions
    drop = set(idx)
    if entry and entry.get("prefix_eol") and drop == {len(lines) - 1} and len(lines) > 1:
        # the install added a newline after the previous (unterminated) last line: take it back
        lines[-2] = lines[-2].removesuffix(b"\n").removesuffix(b"\r")
    new = b"".join(raw for i, raw in enumerate(lines) if i not in drop)
    _backup(path, clock)
    if entry and entry.get("created") and not new:
        path.unlink()
    else:
        _write_atomic(path, new)
    _store(receipts, name, [e for e in entries if e is not entry])
    return actions


# ---------------------------------------------------------------- symlinks


def _link_path(target: str | os.PathLike[str], link_dir: str | os.PathLike[str]) -> Path:
    return Path(link_dir) / Path(target).name


def install_link(
    target: str | os.PathLike[str],
    link_dir: str | os.PathLike[str],
    *,
    receipts: Receipts,
    name: str,
    dry_run: bool = False,
) -> list[Action]:
    """Symlink ``link_dir/<basename of target>`` to ``target`` and record it.

    An identical existing link is a no-op; anything else already at that path raises ``forbidden``.
    """
    link = _link_path(target, link_dir)
    tgt = str(target)
    if link.is_symlink() and os.readlink(link) == tgt:
        return []
    if link.is_symlink() or link.exists():
        raise _refuse("something that was not installed here already exists", link, "edited")
    action = Action("link", str(link), target=tgt, sha256=sha256_text(tgt))
    if dry_run:
        return [action]
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(tgt, link)
    entries = [e for e in _entries(receipts, name) if not (e.get("kind") == "link" and e.get("path") == str(link))]
    entries.append({"kind": "link", "path": str(link), "sha256": action.sha256, "target": tgt})
    receipts.write(name, entries)
    return [action]


def uninstall_link(
    target: str | os.PathLike[str],
    link_dir: str | os.PathLike[str],
    *,
    receipts: Receipts,
    name: str,
    dry_run: bool = False,
    force: bool = False,
) -> list[Action]:
    """Remove the link this tool created; refuse (``forbidden``) if it is not a receipted symlink to ``target``."""
    link = _link_path(target, link_dir)
    entries = _entries(receipts, name)
    entry = next((e for e in entries if e.get("kind") == "link" and e.get("path") == str(link)), None)
    if not (link.is_symlink() or link.exists()):
        if entry is not None and not dry_run:
            _store(receipts, name, [e for e in entries if e is not entry])
        return []
    if not force:
        if entry is None:
            raise _refuse("no receipt for this link", link, "missing")
        if not link.is_symlink() or os.readlink(link) != entry.get("target"):
            raise _refuse("the link was replaced or edited", link, "edited")
    action = Action("remove", str(link), target=str(target))
    if dry_run:
        return [action]
    link.unlink()
    _store(receipts, name, [e for e in entries if e is not entry])
    return [action]


# ---------------------------------------------------------------- diff


def plan_diff(rc_path: str | os.PathLike[str], actions: list[Action]) -> str:
    """Return a unified diff of what ``actions`` (rc_line / remove) would do to ``rc_path`` (for ``--dry-run``)."""
    path = Path(rc_path)
    data = _read(path) or b""
    old = _split(data)
    eol = _eol_of(data)
    new = list(old)
    for a in actions:
        if a.path != str(path) or a.line is None:
            continue
        raw = a.line.encode("utf-8", "surrogateescape")
        if a.op == "rc_line":
            if new and not new[-1].endswith(b"\n"):
                new[-1] += eol
            new.append(raw + eol)
        elif a.op == "remove":
            new = [r for r in new if _content(r) != a.line]
    old_t = [r.decode("utf-8", "replace") for r in old]
    new_t = [r.decode("utf-8", "replace") for r in new]
    return "".join(difflib.unified_diff(old_t, new_t, f"a/{path.name}", f"b/{path.name}"))

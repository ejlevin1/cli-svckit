"""Install receipts: a small record of exactly what an install changed, so uninstall removes only that."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def sha256_text(s: str) -> str:
    """Return the hex SHA-256 of ``s`` encoded as UTF-8 (lone surrogates from undecodable bytes are preserved)."""
    return hashlib.sha256(s.encode("utf-8", "surrogateescape")).hexdigest()


class Receipts:
    """Receipt store: one ``<dir>/<name>.json`` per installed tool, files mode 0600, directory 0700."""

    def __init__(self, dir: str | os.PathLike[str], *, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(dir)
        self._clock = clock

    def _path(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"invalid receipt name: {name!r}")
        return self.dir / f"{name}.json"

    def write(self, name: str, entries: list[dict[str, Any]]) -> Path:
        """Store ``entries`` for ``name`` (replacing any earlier receipt) and return the file path."""
        path = self._path(name)
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        doc = {"v": 1, "name": name, "entries": entries, "written_at": self._clock()}
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=self.dir)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path

    def read(self, name: str) -> dict[str, Any] | None:
        """Return the receipt document for ``name``, or None when absent or unreadable."""
        try:
            doc = json.loads(self._path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) and isinstance(doc.get("entries"), list) else None

    def remove(self, name: str) -> bool:
        """Delete the receipt for ``name``; return whether one existed."""
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            return False
        return True

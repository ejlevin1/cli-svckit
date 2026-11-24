"""Tests for the receipt store."""

from __future__ import annotations

import stat

from cli_svckit.receipts import Receipts, sha256_text


def test_write_read_remove_and_modes(tmp_path):
    r = Receipts(tmp_path / "rc", clock=lambda: 5.0)
    entries = [{"kind": "rc_line", "path": "x", "sha256": "h"}]
    p = r.write("example-tool", entries)
    assert r.read("example-tool") == {"v": 1, "name": "example-tool", "entries": entries, "written_at": 5.0}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    assert r.remove("example-tool") is True
    assert r.remove("example-tool") is False
    assert r.read("example-tool") is None


def test_read_corrupt_is_none(tmp_path):
    r = Receipts(tmp_path)
    (tmp_path / "bad.json").write_text("{nope")
    assert r.read("bad") is None


def test_sha256_text_known():
    assert sha256_text("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

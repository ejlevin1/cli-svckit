"""Tests for rc-file and symlink install helpers (temp dirs only)."""

from __future__ import annotations

import os

import pytest

from cli_svckit.contract.envelope import SvcError
from cli_svckit.rcfile import install_link, install_rc_line, plan_diff, uninstall_link, uninstall_rc_line
from cli_svckit.receipts import Receipts

LINE = 'export PATH="$HOME/.example-tool/bin:$PATH"'
MARK = "example-tool:path"


@pytest.fixture
def rec(tmp_path):
    return Receipts(tmp_path / "receipts")


def inst(rc, rec, **kw):
    return install_rc_line(rc, LINE, MARK, receipts=rec, name="example-tool", **kw)


def uninst(rc, rec, **kw):
    return uninstall_rc_line(rc, MARK, receipts=rec, name="example-tool", **kw)


def test_install_twice_one_line(tmp_path, rec):
    rc = tmp_path / ".zshrc"
    assert len(inst(rc, rec)) == 1
    assert inst(rc, rec) == []
    assert rc.read_text() == f"{LINE}  # {MARK}\n"


@pytest.mark.parametrize("original", [b"alias a=b\n", b"alias a=b", b"a\r\nb\r\n", b"a\r\nb", b"", b"x\n\ny\n"])
def test_roundtrip_exact_bytes(tmp_path, rec, original):
    rc = tmp_path / "rc"
    rc.write_bytes(original)
    inst(rc, rec)
    new = rc.read_bytes()
    assert new.startswith(original.rstrip(b"\r\n")) and f"# {MARK}".encode() in new
    if b"\r\n" in original:
        assert new.endswith(b"\r\n")
    uninst(rc, rec)
    assert rc.read_bytes() == original
    assert rec.read("example-tool") is None


def test_created_file_removed_on_uninstall(tmp_path, rec):
    rc = tmp_path / "new" / "rc"
    inst(rc, rec)
    assert rc.exists()
    uninst(rc, rec)
    assert not rc.exists()


def test_backups_made(tmp_path, rec):
    rc = tmp_path / "rc"
    rc.write_bytes(b"a\n")
    inst(rc, rec, clock=lambda: 0.0)
    uninst(rc, rec, clock=lambda: 0.0)
    names = sorted(p.name for p in tmp_path.iterdir() if "svckit-backup" in p.name)
    assert names == ["rc.svckit-backup-19700101T000000Z", "rc.svckit-backup-19700101T000000Z-1"]


def test_edited_line_refused(tmp_path, rec):
    rc = tmp_path / "rc"
    inst(rc, rec)
    rc.write_text(f"{LINE} --edited  # {MARK}\n")
    before = rc.read_bytes()
    with pytest.raises(SvcError) as ei:
        uninst(rc, rec)
    assert ei.value.code == "forbidden" and ei.value.data["found"] == "edited"
    assert rc.read_bytes() == before
    uninst(rc, rec, force=True)
    assert not rc.exists()  # the install created the file and nothing else was in it


def test_duplicate_refused(tmp_path, rec):
    rc = tmp_path / "rc"
    inst(rc, rec)
    rc.write_bytes(rc.read_bytes() * 2)
    before = rc.read_bytes()
    with pytest.raises(SvcError) as ei:
        uninst(rc, rec)
    assert ei.value.data["found"] == "duplicate"
    assert rc.read_bytes() == before


def test_missing_receipt_refused(tmp_path, rec):
    rc = tmp_path / "rc"
    rc.write_text(f"{LINE}  # {MARK}\n")
    with pytest.raises(SvcError) as ei:
        uninst(rc, rec)
    assert ei.value.data["found"] == "missing"
    assert rc.read_text() == f"{LINE}  # {MARK}\n"


def test_missing_line_and_nothing_to_do(tmp_path, rec):
    rc = tmp_path / "rc"
    assert uninst(rc, rec) == []
    inst(rc, rec)
    rc.write_text("other\n")
    with pytest.raises(SvcError) as ei:
        uninst(rc, rec)
    assert ei.value.data["found"] == "missing"


def test_install_refuses_edited_marked_line(tmp_path, rec):
    rc = tmp_path / "rc"
    rc.write_text(f"different  # {MARK}\n")
    with pytest.raises(SvcError):
        inst(rc, rec)
    assert rc.read_text() == f"different  # {MARK}\n"


def test_other_marker_untouched(tmp_path, rec):
    rc = tmp_path / "rc"
    rc.write_text(f"keep  # {MARK}-other\n")
    inst(rc, rec)
    uninst(rc, rec)
    assert rc.read_text() == f"keep  # {MARK}-other\n"


def test_dry_run_changes_nothing_same_actions(tmp_path, rec):
    rc = tmp_path / "rc"
    rc.write_bytes(b"a\n")
    planned = inst(rc, rec, dry_run=True)
    assert rc.read_bytes() == b"a\n" and rec.read("example-tool") is None
    assert list(tmp_path.iterdir()) == [rc]
    real = inst(rc, rec)
    assert planned == real
    snapshot = rc.read_bytes()
    planned_u = uninst(rc, rec, dry_run=True)
    assert rc.read_bytes() == snapshot and rec.read("example-tool") is not None
    assert uninst(rc, rec) == planned_u
    assert planned[0].to_dict()["op"] == "rc_line"


def test_plan_diff(tmp_path, rec):
    rc = tmp_path / "rc"
    rc.write_text("a\n")
    diff = plan_diff(rc, inst(rc, rec, dry_run=True))
    assert f"+{LINE}  # {MARK}" in diff
    inst(rc, rec)
    diff = plan_diff(rc, uninst(rc, rec, dry_run=True))
    assert f"-{LINE}  # {MARK}" in diff


def test_link_both_ways(tmp_path, rec):
    tool = tmp_path / "src" / "example-tool"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    bin_dir = tmp_path / "bin"
    kw = {"receipts": rec, "name": "example-tool"}
    planned = install_link(tool, bin_dir, dry_run=True, **kw)
    assert not bin_dir.exists()
    acts = install_link(tool, bin_dir, **kw)
    assert acts == planned and os.readlink(bin_dir / "example-tool") == str(tool)
    assert install_link(tool, bin_dir, **kw) == []
    assert uninstall_link(tool, bin_dir, dry_run=True, **kw)
    assert (bin_dir / "example-tool").is_symlink()
    assert len(uninstall_link(tool, bin_dir, **kw)) == 1
    assert not (bin_dir / "example-tool").exists() and rec.read("example-tool") is None
    assert uninstall_link(tool, bin_dir, **kw) == []


def test_link_refuses_foreign(tmp_path, rec):
    tool = tmp_path / "example-tool"
    tool.write_text("x")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "example-tool").write_text("mine")
    kw = {"receipts": rec, "name": "example-tool"}
    with pytest.raises(SvcError):
        install_link(tool, bin_dir, **kw)
    with pytest.raises(SvcError) as ei:
        uninstall_link(tool, bin_dir, **kw)
    assert ei.value.data["found"] == "missing"
    assert (bin_dir / "example-tool").read_text() == "mine"


def test_link_replaced_after_install_refused(tmp_path, rec):
    tool = tmp_path / "example-tool"
    tool.write_text("x")
    bin_dir = tmp_path / "bin"
    kw = {"receipts": rec, "name": "example-tool"}
    install_link(tool, bin_dir, **kw)
    (bin_dir / "example-tool").unlink()
    os.symlink(tmp_path / "elsewhere", bin_dir / "example-tool")
    with pytest.raises(SvcError) as ei:
        uninstall_link(tool, bin_dir, **kw)
    assert ei.value.data["found"] == "edited"


def test_backup_and_rc_keep_source_mode(tmp_path, rec):
    rc = tmp_path / "rc"
    rc.write_text("alias a=b\n")
    rc.chmod(0o600)
    inst(rc, rec)
    uninst(rc, rec)
    backups = list(tmp_path.glob("rc.svckit-backup-*"))
    assert len(backups) == 2
    assert all(b.stat().st_mode & 0o777 == 0o600 for b in backups)
    assert rc.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.svckit-tmp*"))


def test_install_duplicate_reported_as_duplicate(tmp_path, rec):
    rc = tmp_path / "rc"
    rc.write_text(f"{LINE}  # {MARK}\n{LINE}  # {MARK}\n")
    with pytest.raises(SvcError) as ei:
        inst(rc, rec)
    assert ei.value.data["found"] == "duplicate"


def test_force_removes_duplicates(tmp_path, rec):
    rc = tmp_path / "rc"
    rc.write_text(f"a\n{LINE}  # {MARK}\n{LINE}  # {MARK}\n")
    uninst(rc, rec, force=True)
    assert rc.read_text() == "a\n"


def test_install_through_symlinked_rc(tmp_path, rec):
    real = tmp_path / "real"
    real.write_bytes(b"alias a=b\n")
    rc = tmp_path / "rc"
    rc.symlink_to(real)
    inst(rc, rec)
    assert rc.is_symlink() and MARK in real.read_text()
    uninst(rc, rec)
    assert rc.is_symlink() and real.read_bytes() == b"alias a=b\n"


def test_receipt_key_normalised(tmp_path, rec, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    inst("~/rc", rec)
    uninst(tmp_path / "rc", rec)
    assert not (tmp_path / "rc").exists()

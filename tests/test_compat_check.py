"""Unit tests for scripts/compat_check.py: tag selection and the P3 decision, using a temp git repo."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compat_check.py"
_spec = importlib.util.spec_from_file_location("compat_check_under_test", SCRIPT)
assert _spec and _spec.loader
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

PKG = "demo_pkg"
API_OLD = "def keep() -> int:\n    return 1\n\n\ndef gone() -> int:\n    return 2\n"
API_NEW = "def keep() -> int:\n    return 1\n"


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=alice", "-c", "user.email=alice@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def write_state(root: Path, version: str, api: str, changelog: str) -> None:
    (root / "src" / PKG).mkdir(parents=True, exist_ok=True)
    (root / "src" / PKG / "__init__.py").write_text(api, encoding="utf-8")
    (root / "pyproject.toml").write_text(f'[project]\nname = "demo"\nversion = "{version}"\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")


def commit(root: Path, message: str, tag: str | None = None) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    if tag:
        git(root, "tag", tag)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    write_state(tmp_path, "0.1.0", API_OLD, "# Changelog\n\n## [0.1.0]\n\n### Added\n- first\n")
    commit(tmp_path, "one", "v0.1.0")
    write_state(tmp_path, "0.1.1", API_OLD, "# Changelog\n\n## [0.1.1]\n\n### Fixed\n- x\n")
    commit(tmp_path, "two", "v0.1.1")
    git(tmp_path, "tag", "v0.1.10-rc1")  # not X.Y.Z: ignored
    git(tmp_path, "tag", "v0.1.9")  # numeric, not lexical, ordering
    return tmp_path


def test_previous_tag_is_highest_ancestor_not_current(repo: Path) -> None:
    assert cc.previous_tag(repo, "0.1.1") == "v0.1.9"
    assert cc.previous_tag(repo, "0.1.9") == "v0.1.1"


def test_previous_tag_ignores_non_ancestors(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "side", "v0.1.0")
    write_state(repo, "0.2.0", API_OLD, "# C\n\n## [0.2.0]\n")
    commit(repo, "side", "v0.2.0")
    git(repo, "checkout", "-q", "main")
    assert cc.previous_tag(repo, "0.1.1") == "v0.1.9"


def test_previous_tag_none(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    write_state(tmp_path, "0.1.0", API_OLD, "")
    commit(tmp_path, "one")
    assert cc.previous_tag(tmp_path, "0.1.0") is None
    assert cc.check(tmp_path, package=PKG)["ok"] is True


def test_changelog_helpers() -> None:
    text = "# C\n\n## [0.2.0]\n\n### Breaking\n- removed gone()\n\n## [0.1.0]\n\n### Breaking\n- old\n"
    top = cc.top_changelog_section(text)
    assert "0.2.0" in top and "0.1.0" not in top
    assert cc.has_breaking_heading(top)
    assert not cc.has_breaking_heading("## [0.2.0]\n\n### Breaking\n\n### Added\n- x\n")
    assert not cc.has_breaking_heading("## [0.2.0]\n\n### Added\n- x\n")


@pytest.mark.parametrize(
    ("prev", "cur", "heading", "ok"),
    [
        ((0, 1, 0), (0, 1, 1), True, False),  # no minor bump
        ((0, 1, 0), (0, 2, 0), False, False),  # no Breaking heading
        ((0, 1, 0), (0, 2, 0), True, True),
        ((1, 2, 0), (1, 3, 0), True, False),  # from 1.0 a minor bump is not enough
        ((1, 2, 0), (2, 0, 0), True, True),
    ],
)
def test_decide(prev: tuple[int, int, int], cur: tuple[int, int, int], heading: bool, ok: bool) -> None:
    log = "## [x]\n\n### Breaking\n- gone\n" if heading else "## [x]\n\n### Added\n- y\n"
    assert cc.decide(prev, cur, log, 1)[0] is ok
    assert cc.decide(prev, cur, log, 0)[0] is True


def test_check_no_change_passes(repo: Path) -> None:
    result = cc.check(repo, package=PKG)
    assert result["ok"] and result["breakages"] == []


def test_removed_function_fails_without_bump(repo: Path) -> None:
    write_state(repo, "0.1.1", API_NEW, "# C\n\n## [0.1.1]\n\n### Breaking\n- removed gone()\n")
    result = cc.check(repo, against="v0.1.0", package=PKG)
    assert not result["ok"]
    assert any("gone" in b["path"] for b in result["breakages"])
    assert "minor" in result["reason"]
    assert cc.main(["--root", str(repo), "--package", PKG, "--against", "v0.1.0"]) == 1


def test_removed_function_needs_breaking_heading(repo: Path) -> None:
    write_state(repo, "0.2.0", API_NEW, "# C\n\n## [0.2.0]\n\n### Removed\n- gone()\n")
    result = cc.check(repo, against="v0.1.0", package=PKG)
    assert not result["ok"] and "Breaking" in result["reason"]


def test_removed_function_allowed_with_minor_bump_and_heading(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_state(repo, "0.2.0", API_NEW, "# C\n\n## [0.2.0]\n\n### Breaking\n- removed gone()\n")
    assert cc.main(["--root", str(repo), "--package", PKG, "--against", "v0.1.0", "--json"]) == 0
    assert '"ok": true' in capsys.readouterr().out

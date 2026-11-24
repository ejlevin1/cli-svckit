"""Check the public Python API against the previous release (policy P3 in docs/compatibility.md).

Usage: ``uv run --locked python scripts/compat_check.py [--against REF] [--json] [--package NAME]``

The previous release is the highest ``vX.Y.Z`` tag that is an ancestor of HEAD and differs from the version in
``pyproject.toml``. Griffe compares that tag's public API with the working tree. A breakage is allowed only when
the version bump permits it (while 0.x: the MINOR was bumped; from 1.0: the MAJOR was bumped) and the top CHANGELOG
section has a ``### Breaking`` heading. Exit code 0 = fine (or nothing to compare), 1 = disallowed breakage.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
Version = tuple[int, int, int]


def parse_version(text: str) -> Version | None:
    """Parse ``X.Y.Z`` (optionally ``v``-prefixed); anything else gives ``None``."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _git(root: Path, *args: str) -> str:
    """Run git in ``root`` and return stdout (raises ``CalledProcessError`` on failure)."""
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def current_version(root: Path) -> str:
    """The version declared in ``pyproject.toml`` of the working tree."""
    return str(tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])


def previous_tag(root: Path, version: str | None = None) -> str | None:
    """Highest ``vX.Y.Z`` tag that is an ancestor of HEAD and not equal to ``version`` (default: pyproject's)."""
    version = version if version is not None else current_version(root)
    own = parse_version(version)
    found: list[tuple[Version, str]] = []
    for tag in _git(root, "tag", "--merged", "HEAD").split():
        m = TAG_RE.match(tag)
        if m and (parsed := (int(m[1]), int(m[2]), int(m[3]))) != own:
            found.append((parsed, tag))
    return max(found)[1] if found else None


def top_changelog_section(text: str) -> str:
    """The first ``## `` section of a changelog (heading line included)."""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## ") and lines:
            break
        if line.startswith("## ") or lines:
            lines.append(line)
    return "\n".join(lines)


def has_breaking_heading(section: str) -> bool:
    """True when the section has a ``### Breaking`` heading followed by at least one non-blank line."""
    lines = section.splitlines()
    for i, line in enumerate(lines):
        if re.fullmatch(r"###\s+Breaking\s*", line):
            for nxt in lines[i + 1 :]:
                if nxt.startswith("#"):
                    return False
                if nxt.strip():
                    return True
    return False


def decide(prev: Version, cur: Version, changelog: str, n_breakages: int) -> tuple[bool, str]:
    """Apply P3: return ``(allowed, reason)`` for ``n_breakages`` API breakages going from ``prev`` to ``cur``."""
    if n_breakages == 0:
        return True, "no breaking API changes"
    if prev[0] >= 1:
        bumped = cur[0] > prev[0]
        need = "a major version bump (from 1.0 breakage needs a new major)"
    else:
        bumped = cur[0] == prev[0] and cur[1] > prev[1] or cur[0] > prev[0]
        need = "a minor version bump (while 0.x breakage needs a new minor)"
    if not bumped:
        return False, f"breaking changes need {need}; version went {_fmt(prev)} -> {_fmt(cur)}"
    if not has_breaking_heading(top_changelog_section(changelog)):
        return False, "breaking changes need a non-empty '### Breaking' heading in the top CHANGELOG section"
    return True, "breaking changes allowed: version bump and '### Breaking' CHANGELOG entry present"


def _fmt(v: Version) -> str:
    return ".".join(map(str, v))


def _version_at(root: Path, ref: str, tag_fallback: str) -> Version:
    """Version declared by ``pyproject.toml`` at ``ref`` (falls back to parsing the ref name)."""
    try:
        data = tomllib.loads(_git(root, "show", f"{ref}:pyproject.toml"))
        parsed = parse_version(str(data["project"]["version"]))
    except (subprocess.CalledProcessError, KeyError, tomllib.TOMLDecodeError):
        parsed = None
    parsed = parsed or parse_version(tag_fallback)
    if parsed is None:
        raise SystemExit(f"cannot determine the version at {ref}")
    return parsed


def find_breakages(root: Path, ref: str, package: str = "cli_svckit") -> list[Any]:
    """Griffe breakages of the current tree's public API relative to ``ref``."""
    import griffe

    search = [str(root / "src")]
    old = griffe.load_git(package, ref=ref, repo=root, search_paths=["src"])
    new = griffe.load(package, search_paths=search)
    return list(griffe.find_breaking_changes(old, new))


def check(root: Path, against: str | None = None, package: str = "cli_svckit") -> dict[str, Any]:
    """Run the whole check; returns a JSON-able result with ``ok`` and ``breakages``."""
    cur_text = current_version(root)
    cur = parse_version(cur_text)
    ref = against or previous_tag(root, cur_text)
    if ref is None:
        return {"ok": True, "previous": None, "current": cur_text, "breakages": [], "reason": "no previous release tag"}
    prev = _version_at(root, ref, ref)
    if cur is None:
        raise SystemExit(f"unparseable current version {cur_text!r}")
    breakages = find_breakages(root, ref, package)
    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else ""
    allowed, reason = decide(prev, cur, changelog, len(breakages))
    return {
        "ok": allowed,
        "previous": ref,
        "current": cur_text,
        "reason": reason,
        "breakages": [{"kind": str(b.kind.value), "path": b.obj.path, "explanation": b.explain()} for b in breakages],
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--against", metavar="REF", help="compare with this git ref instead of the previous release tag")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("--package", default="cli_svckit")
    ap.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    ns = ap.parse_args(argv)
    result = check(Path(ns.root), ns.against, ns.package)
    if ns.json:
        print(json.dumps(result, indent=2))
    elif result["previous"] is None:
        print(f"compat_check: {result['reason']}; nothing to compare")
    else:
        print(f"compat_check: {result['previous']} -> {result['current']}: {result['reason']}")
        for b in result["breakages"]:
            print(f"  - {b['explanation']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

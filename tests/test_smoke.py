import ast
import pathlib
import sys
import tomllib

import cli_svckit

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_version_matches_pyproject():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert cli_svckit.__version__ == meta["project"]["version"]


def test_core_has_no_runtime_dependencies():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["dependencies"] == []


def test_core_imports_stdlib_only():
    """Optional extras (typer, rich, psutil) may only be imported lazily, inside functions."""
    stdlib = set(sys.stdlib_module_names) | {"cli_svckit", "__future__"}
    for path in (ROOT / "src" / "cli_svckit").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module level only
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for n in names:
                assert n.split(".")[0] in stdlib, f"{path.name}: top-level import of non-stdlib {n!r}"

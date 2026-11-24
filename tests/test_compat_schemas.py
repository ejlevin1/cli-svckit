"""Contract 1.x compatibility: the current schemas must accept everything the frozen 1.0 schemas accept (policy P1).

The frozen copies live in ``tests/compat/contract-1.0/`` (byte-identical to the v0.1.0 release). For every message
shape (each ``$defs`` entry, each ``oneOf``/``anyOf`` branch of one, and a top-level schema that has a ``type``)
hypothesis generates instances from the frozen schema and each must validate under the current schema of the same
name. Two structural checks back that up: no current object schema requires a key the frozen one did not at the
same JSON pointer, and enum (and ``const``) value sets only grow. The ``test_selfcheck_*`` tests prove the checks
fail on a hand-made breaking change.
"""

from __future__ import annotations

import copy
import functools
import importlib.resources
import json
import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis import strategies as st
from hypothesis_jsonschema import from_schema
from jsonschema import Draft202012Validator

from cli_svckit.contract.envelope import load_schema

ROOT = Path(__file__).resolve().parents[1]
FROZEN_DIR = ROOT / "tests" / "compat" / "contract-1.0"
FROZEN_TAG = "v0.1.0"

Schema = dict[str, Any]
Store = Mapping[str, Schema]

# Derandomized in CI. The profile is only registered here (a conftest may register its own "ci" first) and
# applied per test through ``settings(parent)``, so loading this module changes no global hypothesis state.
try:
    settings.get_profile("ci")
except Exception:  # hypothesis raises InvalidArgument for an unknown profile
    settings.register_profile("ci", derandomize=True, database=None, deadline=None, max_examples=200)
_IN_CI = os.environ.get("HYPOTHESIS_PROFILE") == "ci" or bool(os.environ.get("CI"))
_PARENT = settings.get_profile("ci") if _IN_CI else settings()
HSETTINGS = settings(
    _PARENT,
    max_examples=200,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much, HealthCheck.data_too_large],
)


# -- schema plumbing ---------------------------------------------------------------------------------------------


def _load_frozen() -> dict[str, Schema]:
    """Return the frozen 1.0 schemas by name (file stem)."""
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(FROZEN_DIR.glob("*.json"))}


def _load_current() -> dict[str, Schema]:
    """Return every schema shipped with the current package by name (via ``load_schema``)."""
    folder = importlib.resources.files("cli_svckit.contract").joinpath("schemas")
    names = sorted(r.name[: -len(".json")] for r in folder.iterdir() if r.name.endswith(".json"))
    return {name: load_schema(name) for name in names}


FROZEN = _load_frozen()
CURRENT = _load_current()


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def resolve_pointer(doc: Any, pointer: str) -> Any | None:
    """Return the node at JSON pointer ``pointer`` ("" is the root) in ``doc``, or None when it does not exist."""
    node = doc
    if not pointer:
        return node
    for raw in pointer.lstrip("/").split("/"):
        token = _unescape(raw)
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return None
    return node


def inline(store: Store, name: str, pointer: str = "") -> Schema:
    """Return the subschema ``name.json#pointer`` with every ``$ref`` replaced by its target (recursively).

    Handles ``#/...`` (same file) and ``<other>.json#/...`` refs against ``store``; a ``$ref`` with sibling keywords
    becomes ``allOf: [target, siblings]``. The contract schemas are not recursive, so full inlining terminates.
    """

    def expand(node: Any, current: str, depth: int) -> Any:
        if depth > 50:
            raise RecursionError(f"$ref chain too deep in {name}.json#{pointer}")
        if isinstance(node, list):
            return [expand(x, current, depth) for x in node]
        if not isinstance(node, dict):
            return node
        out = {k: expand(v, current, depth) for k, v in node.items() if k not in ("$ref", "$defs", "$schema", "$id")}
        ref = node.get("$ref")
        if ref is None:
            return out
        file_part, _, ptr = ref.partition("#")
        target_name = file_part[: -len(".json")] if file_part else current
        target_doc = store.get(target_name)
        target = resolve_pointer(target_doc, ptr) if target_doc is not None else None
        if target is None:
            raise KeyError(f"{name}.json#{pointer}: unresolvable $ref {ref!r}")
        expanded = expand(target, target_name, depth + 1)
        return {"allOf": [expanded, out]} if out else expanded

    node = resolve_pointer(store[name], pointer)
    if node is None:
        raise KeyError(f"{name}.json has no subschema at {pointer!r}")
    return expand(node, name, 0)


def message_shapes(schema: Schema) -> list[str]:
    """Return the JSON pointers of the message shapes in ``schema``.

    A shape is the root when it declares a ``type``, every ``$defs`` entry, and every ``oneOf``/``anyOf`` branch
    of those (so a branch is checked on its own as well as through its parent).
    """
    pointers: list[str] = []
    if "type" in schema:
        pointers.append("")
    for key in sorted(schema.get("$defs", {})):
        ptr = f"/$defs/{key.replace('~', '~0').replace('/', '~1')}"
        pointers.append(ptr)
        node = schema["$defs"][key]
        for combinator in ("oneOf", "anyOf"):
            for i, _ in enumerate(node.get(combinator, [])):
                pointers.append(f"{ptr}/{combinator}/{i}")
    return pointers


def walk(node: Any, pointer: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(pointer, subschema)`` for every object in a schema document, depth first."""
    if isinstance(node, dict):
        yield pointer, node
        for key, value in node.items():
            yield from walk(value, f"{pointer}/{key.replace('~', '~0').replace('/', '~1')}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk(value, f"{pointer}/{i}")


def new_required(name: str, frozen: Schema, current: Schema) -> list[str]:
    """Return a problem per current object schema that requires a key its frozen counterpart did not.

    Subschemas with no counterpart at the same pointer (new optional structure) are skipped: whatever is new there
    is only reachable through fields the frozen schema did not require, and the instance check covers the rest.
    """
    problems: list[str] = []
    for ptr, node in walk(current):
        req = node.get("required")
        if not isinstance(req, list):
            continue
        old = resolve_pointer(frozen, ptr)
        if not isinstance(old, dict):
            continue
        added = sorted(set(req) - set(old.get("required", [])))
        if added:
            problems.append(
                f"{name}.json#{ptr}: new required field(s) {added} (frozen 1.0 required {old.get('required', [])})"
            )
    return problems


def _values(node: dict[str, Any]) -> list[Any] | None:
    if "enum" in node and isinstance(node["enum"], list):
        return node["enum"]
    if "const" in node:
        return [node["const"]]
    return None


def shrunk_enums(name: str, frozen: Schema, current: Schema) -> list[str]:
    """Return a problem per frozen ``enum``/``const`` whose values are not all allowed at the same current pointer."""
    problems: list[str] = []
    for ptr, node in walk(frozen):
        old = _values(node)
        if old is None:
            continue
        cur_node = resolve_pointer(current, ptr)
        if not isinstance(cur_node, dict):
            continue
        cur = _values(cur_node)
        if cur is None:
            continue  # the restriction was lifted: wider, not breaking
        missing = [v for v in old if v not in cur]
        if missing:
            problems.append(f"{name}.json#{ptr}: enum lost value(s) {missing} (frozen 1.0: {old}, current: {cur})")
    return problems


@functools.cache
def _strategy(name: str, pointer: str) -> st.SearchStrategy[Any]:
    return from_schema(inline(FROZEN, name, pointer))


@functools.cache
def _validators(name: str, pointer: str) -> tuple[Draft202012Validator, Draft202012Validator | None]:
    frozen = Draft202012Validator(inline(FROZEN, name, pointer))
    if name not in CURRENT or resolve_pointer(CURRENT[name], pointer) is None:
        return frozen, None
    return frozen, Draft202012Validator(inline(CURRENT, name, pointer))


def check_instance(
    name: str, pointer: str, instance: Any, frozen: Draft202012Validator, current: Draft202012Validator | None
) -> None:
    """Assert that ``instance`` (valid under frozen ``name.json#pointer``) validates under the current schema."""
    assert frozen.is_valid(instance), f"generator bug: instance not valid under frozen {name}.json#{pointer}"
    shown = json.dumps(instance, sort_keys=True, default=repr)
    if len(shown) > 800:
        shown = shown[:800] + "..."
    assert current is not None, (
        f"{name}.json#{pointer}: message shape of frozen contract 1.0 is missing from the current schema "
        f"(removing a shape is a breaking change; it needs contract 2.0). Example instance: {shown}"
    )
    errors = sorted(current.iter_errors(instance), key=lambda e: list(e.absolute_path))
    if errors:
        err = errors[0]
        raise AssertionError(
            f"{name}.json#{pointer}: an instance valid under frozen contract 1.0 is rejected by the current schema "
            f"(breaking change; policy P1). Error at instance path {err.json_path}: {err.message}. "
            f"Example instance: {shown}"
        )


# -- the checks ----------------------------------------------------------------------------------------------------

SHAPES = [(name, ptr) for name in sorted(FROZEN) for ptr in message_shapes(FROZEN[name])]


def test_frozen_copies_are_present() -> None:
    """The frozen folder holds the full 1.0 schema set and every frozen schema still exists in the package."""
    assert sorted(FROZEN) == [
        "complete",
        "describe",
        "doctor",
        "envelope",
        "health",
        "install",
        "lease",
        "prepare",
    ]
    assert (FROZEN_DIR / "README.md").is_file()
    missing = sorted(set(FROZEN) - set(CURRENT))
    assert not missing, f"current package dropped frozen contract 1.0 schema(s): {missing}"
    assert len(SHAPES) >= 20, SHAPES


def test_frozen_copies_match_release_tag() -> None:
    """The frozen files are byte-identical to the schemas at the 1.0 release tag (skipped without git or the tag)."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not available")
    probe = subprocess.run(
        [git, "-C", str(ROOT), "rev-parse", "--verify", "-q", f"{FROZEN_TAG}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"tag {FROZEN_TAG} not available (run git fetch --tags)")
    for path in sorted(FROZEN_DIR.glob("*.json")):
        released = subprocess.run(
            [git, "-C", str(ROOT), "show", f"{FROZEN_TAG}:src/cli_svckit/contract/schemas/{path.name}"],
            capture_output=True,
            check=True,
        ).stdout
        assert path.read_bytes() == released, f"{path.name} differs from {FROZEN_TAG}: frozen copies must never change"


@pytest.mark.parametrize(("name", "pointer"), SHAPES, ids=[f"{n}#{p or '/'}" for n, p in SHAPES])
@HSETTINGS
@given(data=st.data())
def test_frozen_instances_validate_under_current(name: str, pointer: str, data: st.DataObject) -> None:
    """Every instance valid under a frozen 1.0 shape validates under the current schema of the same name."""
    frozen, current = _validators(name, pointer)
    instance = data.draw(_strategy(name, pointer), label=f"{name}.json#{pointer}")
    check_instance(name, pointer, instance, frozen, current)


@pytest.mark.parametrize("name", sorted(FROZEN))
def test_no_new_required_fields(name: str) -> None:
    """No current object schema requires a key that the frozen 1.0 schema at the same pointer did not."""
    problems = new_required(name, FROZEN[name], CURRENT[name])
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("name", sorted(FROZEN))
def test_enums_only_grow(name: str) -> None:
    """Every enum/const value allowed by frozen 1.0 is still allowed at the same pointer."""
    problems = shrunk_enums(name, FROZEN[name], CURRENT[name])
    assert not problems, "\n".join(problems)


# -- self-tests: the checks bite ---------------------------------------------------------------------------------


def _counterexample(store_current: Store, name: str, pointer: str) -> Any:
    """Find an instance valid under frozen ``name.json#pointer`` that the given current store rejects."""
    current = Draft202012Validator(inline(store_current, name, pointer))
    return find(
        from_schema(inline(FROZEN, name, pointer)),
        lambda inst: not current.is_valid(inst),
        settings=settings(HSETTINGS, max_examples=500),
    )


def test_selfcheck_extra_required_field_fails() -> None:
    """A current schema that adds a required field to prepare params is caught by both checks."""
    broken = copy.deepcopy(FROZEN["prepare"])
    broken["$defs"]["params"]["required"].append("lane")
    problems = new_required("prepare", FROZEN["prepare"], broken)
    assert len(problems) == 1 and problems[0].startswith("prepare.json#/$defs/params: new required field(s) ['lane']")

    store = {**FROZEN, "prepare": broken}
    example = _counterexample(store, "prepare", "/$defs/params")
    assert "lane" not in example
    with pytest.raises(AssertionError, match=r"prepare\.json#/\$defs/params: an instance valid under frozen"):
        check_instance(
            "prepare",
            "/$defs/params",
            example,
            Draft202012Validator(inline(FROZEN, "prepare", "/$defs/params")),
            Draft202012Validator(inline(store, "prepare", "/$defs/params")),
        )


def test_selfcheck_removed_enum_value_fails() -> None:
    """A current schema that drops ``busy`` from the prepare result state is caught by both checks."""
    broken = copy.deepcopy(FROZEN["prepare"])
    broken["$defs"]["result"]["properties"]["state"]["enum"] = ["ready"]
    problems = shrunk_enums("prepare", FROZEN["prepare"], broken)
    assert problems == [
        "prepare.json#/$defs/result/properties/state: enum lost value(s) ['busy'] "
        "(frozen 1.0: ['ready', 'busy'], current: ['ready'])"
    ]
    example = _counterexample({**FROZEN, "prepare": broken}, "prepare", "/$defs/result")
    assert example["state"] == "busy"


def test_selfcheck_change_in_referenced_file_fails() -> None:
    """Narrowing ``lease.json#/$defs/op`` breaks prepare params through the cross-file ``$ref``."""
    broken = copy.deepcopy(FROZEN["lease"])
    broken["$defs"]["op"]["properties"]["kind"]["enum"] = ["restart"]
    example = _counterexample({**FROZEN, "lease": broken}, "prepare", "/$defs/params")
    assert example["op"]["kind"] != "restart"


def test_selfcheck_removed_shape_fails() -> None:
    """A message shape missing from the current schema fails with the schema name and pointer."""
    frozen = Draft202012Validator(inline(FROZEN, "doctor", "/$defs/check"))
    instance = {"id": "x", "area": "a", "status": "ok", "detail": ""}
    with pytest.raises(AssertionError, match=r"doctor\.json#/\$defs/check: message shape .* is missing"):
        check_instance("doctor", "/$defs/check", instance, frozen, None)


def test_selfcheck_identical_schemas_pass() -> None:
    """Negative control: the structural checks report nothing for an unchanged schema."""
    for name, schema in FROZEN.items():
        assert new_required(name, schema, copy.deepcopy(schema)) == []
        assert shrunk_enums(name, schema, copy.deepcopy(schema)) == []

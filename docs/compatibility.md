# Compatibility policy

cli-svckit has three public surfaces that other tools depend on: the JSON contract, the lease file format and the
Python API. This page says what may change in a release and which CI checks enforce it. The checks compare the
current tree against the **previous release**: the highest `vX.Y.Z` tag that is an ancestor of HEAD and is not the
version in `pyproject.toml` (so a build of a release tag compares against the release before it).

## The policy

**P1. Contract 1.x is never breaking.** A schema change that rejects any instance valid under the frozen 1.0
schema, or that adds a required field, fails CI. A breaking contract change needs a contract major (2.0) and
frozen 2.0 copies of the schemas.

**P2. Lease format v1 stays readable.** Current code must read every stored v1 sample with the same meaning.
A breaking change needs `"v": 2`. Readers ignore lease files whose `v` is greater than they understand.

**P3. Python API.** While the version is 0.x, a breakage found by Griffe fails CI unless both hold:

1. the MINOR version is bumped compared with the previous tag, and
2. the top section of `CHANGELOG.md` has a `### Breaking` heading that lists the breakage.

From 1.0 on, a breakage needs a major bump. Deprecations keep the old name for at least one minor release and
emit a `DeprecationWarning`.

**P4. Cross-version.** The previous release's example participant must pass the current `assert_conforms` and
complete a coordinated operation with the current controller. The current example participant must do the same
with the previous release's controller.

## What each check does

| Policy | Check | What it does |
| --- | --- | --- |
| P1 | `tests/test_compat_schemas.py` | Generates instances with hypothesis-jsonschema from the frozen 1.0 schemas in `tests/compat/contract-1.0/` and validates each against the current schemas. Also validates the stored sample documents. |
| P2 | `tests/test_compat_lease.py` | Reads each stored v1 lease in `tests/compat/lease-v1/` with the current code and checks the meaning (phase, outcome, expiry). |
| P3 | `scripts/compat_check.py` | Uses Griffe to compare the public API (`cli_svckit.__all__`) with the previous tag, then applies the version-bump and CHANGELOG rule. |
| P4 | `tests/test_crossversion.py` | Runs the previous release's example participant against the current controller, and the reverse. Skipped unless `SVCKIT_PREV_SRC` is set. |

## Running the checks locally

API check (needs tags: `git fetch --tags`):

```sh
uv run --locked python scripts/compat_check.py            # against the previous release
uv run --locked python scripts/compat_check.py --against v0.1.0
```

Exit code 0 means fine, or nothing to compare; 1 means a disallowed breakage.

Schema and lease tests:

```sh
uv run --locked --all-extras -q pytest -q tests/test_compat_schemas.py tests/test_compat_lease.py
```

Hypothesis runs `max_examples=200`. Set `HYPOTHESIS_PROFILE=ci` to derandomize, as CI does.

Cross-version test: check out the previous release into a separate directory and point `SVCKIT_PREV_SRC` at it.

```sh
git worktree add /tmp/svckit-prev v0.1.0
SVCKIT_PREV_SRC=/tmp/svckit-prev uv run --locked --all-extras -q pytest -q tests/test_crossversion.py
git worktree remove /tmp/svckit-prev
```

## Making an allowed breaking change

- **Contract:** add the new major, freeze copies of its schemas next to the 1.0 ones, and keep the 1.0 checks
  running for as long as 1.x is supported.
- **Lease:** write `"v": 2`; keep reading v1.
- **Python API (0.x):** bump the minor in `pyproject.toml` and `src/cli_svckit/__init__.py`, run `uv lock`, and add
  a `### Breaking` heading to the top `CHANGELOG.md` section that names each breakage. Prefer deprecating first.

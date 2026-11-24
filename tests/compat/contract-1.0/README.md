# Frozen contract 1.0 schemas

These files are byte-identical copies of `src/cli_svckit/contract/schemas/*.json` at the `v0.1.0` tag, the release
that published contract `svckit: "1.0"`. They were produced with
`git show v0.1.0:src/cli_svckit/contract/schemas/<file>`.

**Never edit them.** `tests/test_compat_schemas.py` checks that every instance valid under a frozen schema is still
valid under the current schema of the same name, that no current object schema requires a key the frozen one did
not, and that enum sets only grow. A change that fails those checks is a breaking contract change (policy P1 in
`docs/compatibility.md`).

A contract 2.0 does not touch this folder: it adds a new `tests/compat/contract-2.0/` with frozen 2.0 copies, and
the 1.x checks keep running against this one for as long as 1.x is supported.

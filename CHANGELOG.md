# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [0.1.1]

### Added
- Compatibility checks: a Griffe check of the public Python API against the previous release, schema and
  lease-format compatibility tests (hypothesis-jsonschema against the frozen 1.0 schemas) and a cross-version run in CI.
- Documented compatibility policy (`docs/compatibility.md`).

### Fixed
- Contract doc examples now use the operation kinds the library and its consumers use.

## [0.1.0]

### Added
- Contract envelope (`cli_svckit.contract.envelope`): versioned JSON request/response types, error codes, exit codes,
  secret redaction, operation records, glob matching and bundled JSON schemas for every method.
- Contract client (`Participant`, `CallResult`, `negotiate`): runs a participant's `svc <method> --json` command with a
  timeout and a fail-open or fail-closed policy, and negotiates the contract version.
- Contract server (`dispatch`, `run`): lets a tool answer contract calls from a simple provider object.
- Participant discovery (`participants_from_registry`): builds participants from a registry table.
- Lease directory (`LeaseDir`, `Lease`): a durable on-disk record of each restart or update operation, with phases,
  expiry of stale leases and tombstones for finished operations.
- Participant side (`Interest`, `active_ops`, `Quiescer`): daemons learn which operations affect them, quiesce once on
  prepare, resume once on completion, and catch up after a restart.
- Lease watcher (`LeaseWatcher`): notices lease directory changes using kqueue, inotify or polling.
- Controller (`Operation`): coordinates participants through prepare, running and complete phases.
- Health and doctor helpers (`HealthResult`, `run_checks`) plus a Typer/argparse CLI wiring for the contract.

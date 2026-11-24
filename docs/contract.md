# The svckit contract (1.0)

This is the contract between a **controller** (one tool that restarts or updates services) and **participants**
(tools that must quiesce before that happens and resume afterwards). A participant is any command-line tool; it
does not have to use this library, it only has to speak the JSON described here. The machine-readable form is the
JSON Schema 2020-12 files in `src/cli_svckit/contract/schemas/` (`load_schema("prepare")` loads one).

## Calling form

```
<cmd> svc <method> --json [--request-file PATH]
```

- `<method>` is one of `describe`, `health`, `doctor`, `install`, `uninstall`, `prepare`, `complete`.
- The **request** is one JSON object on stdin, or in the file named by `--request-file`.
- The participant may print log lines on stdout and anything on stderr. The **response** is the **last non-empty
  line of stdout that parses as a JSON object** and must be a single line. Everything before it is ignored.
- A method the tool does not implement answers `ok: false` with error code `unsupported`.

### Request

| key | type | meaning |
| --- | --- | --- |
| `svckit` | `"major.minor"` | contract version the caller speaks |
| `id` | string | request id, echoed in the response |
| `method` | string | the method name (same as on the command line) |
| `caller` | object or null | who is calling, free-form (`{"tool": "example-tool", "user": "alice"}`) |
| `deadline_ms` | integer or null | how long the caller will wait for an answer |
| `params` | object | method parameters (below) |

### Response

| key | type | meaning |
| --- | --- | --- |
| `svckit` | `"major.minor"` | contract version the participant speaks |
| `id` | string | the request id |
| `ok` | boolean | true: `result` is set; false: `error` is set |
| `result` | object | method result (below) |
| `error` | object | `{code, message, retry_after_s?, data?}` |

Error codes: `unsupported`, `invalid`, `forbidden`, `requires_root`, `timeout`, `internal`, `incompatible`.
`busy` is **not** an error: a participant that cannot quiesce yet answers `prepare` with `ok: true` and
`result.state: "busy"`.

### Exit codes

| code | name | meaning |
| --- | --- | --- |
| 0 | `EXIT_OK` | success |
| 1 | `EXIT_FAIL` | the method failed (see `error`) |
| 2 | `EXIT_USAGE` | bad arguments or malformed request |
| 3 | `EXIT_BUSY` | `prepare` answered `busy` |
| 4 | `EXIT_WARN` | success with warnings (`health`/`doctor` status `warn`) |
| 75 | `EXIT_TEMPFAIL` | temporary failure, retry later (`retry_after_s` says when) |

The JSON response is authoritative; the exit code is a summary for shells.

### Versioning

- `svckit` is the contract `major.minor`. This document is `1.0`.
- A minor version only **adds optional fields**. Readers ignore fields they do not know (must-ignore), everywhere.
- A new major version may change or remove anything.
- `describe` reports `contract: {min, max}`. The controller's `negotiate(contract)` picks the highest minor both
  sides support within the same major and refuses (error `incompatible`) when the majors differ.

All times are float UNIX timestamps (seconds); durations end in `_s` or `_ms`.

## The operation record (`op`)

`prepare` and `complete` carry the operation as `params.op`; the lease file stores the same object.

<!-- schema: lease.op -->
```json
{"id": "op-20260101T120000-a1b2", "kind": "update", "targets": ["com.example.worker"], "reason": "upgrade to 1.4.0", "by": {"tool": "example-tool", "user": "alice", "pid": 4242}, "started_at": 1767268800.0, "deadline": 1767269100.0, "expires_at": 1767270600.0, "affected": {"pids": [5150, 5151]}, "wait_for": "interrupted", "now": false, "lease": "~/.local/state/example-tool/leases/op-20260101T120000-a1b2.json"}
```

- `kind` and `targets` are matched by participants against their declared globs (`coordination.kinds`,
  `coordination.services`) with shell-style patterns.
- `deadline`: when the controller stops waiting. `expires_at`: when a leftover lease counts as stale.
- `affected.pids`: the processes the operation will stop, or `null` when unknown.
- `wait_for: "interrupted"`: ready means in-flight work may be interrupted safely. `now: true`: do not wait.

## Methods

### describe

Request:

<!-- schema: envelope.request -->
```json
{"svckit": "1.0", "id": "req-1", "method": "describe", "caller": {"tool": "example-tool"}, "deadline_ms": 5000, "params": {}}
```

Result:

<!-- schema: describe.result -->
```json
{"tool": {"name": "example-tool", "version": "1.3.2"}, "contract": {"min": "1.0", "max": "1.0"}, "methods": ["describe", "health", "doctor", "install", "uninstall", "prepare", "complete"], "services": [{"label": "com.example.worker", "kind": "agent"}], "coordination": {"kinds": ["update", "restart*"], "services": ["com.example.*"], "phases": ["prepare", "complete"], "prepare_timeout_s": 120, "complete_timeout_s": 30, "watches_lease": true}, "doctor": {"checks": [{"id": "config", "title": "Config file parses"}]}}
```

### health

Result (IETF health-check draft style; `checks` keys are `<component>:<measure>`):

<!-- schema: health.result -->
```json
{"status": "warn", "live": true, "ready": true, "version": "1.3.2", "checks": {"queue:depth": [{"status": "warn", "observedValue": 812, "observedUnit": "jobs", "output": "queue above 500"}], "worker:uptime": [{"status": "pass", "observedValue": 3600, "observedUnit": "s"}]}}
```

### doctor

Params:

<!-- schema: doctor.params -->
```json
{"fix": false, "deep": true}
```

Result:

<!-- schema: doctor.result -->
```json
{"status": "fail", "checks": [{"id": "config", "area": "config", "status": "ok", "detail": "~/.config/example-tool/config.toml parses"}, {"id": "shim", "area": "cli", "status": "fail", "detail": "example-tool shim is missing", "fix": {"argv": ["example-tool", "svc", "install", "--json"], "hint": "reinstall the CLI shim"}}]}
```

### install / uninstall

Both take the same params and return the same result. `dry_run: true` reports the actions without doing them.
Uninstall removes only what install created.

<!-- schema: install.params -->
```json
{"dry_run": true, "confirm": false, "scope": "cli"}
```

<!-- schema: install.result -->
```json
{"changed": true, "actions": [{"op": "write", "path": "~/.local/bin/example-tool", "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"}, {"op": "rc_line", "path": "~/.zshrc", "line": "export PATH=\"$HOME/.local/bin:$PATH\"  # example-tool"}, {"op": "launchctl", "argv": ["launchctl", "bootstrap", "gui/501", "~/Library/LaunchAgents/com.example.worker.plist"]}], "requires_root": {"argv": ["sudo", "example-tool", "svc", "install", "--json", "--scope", "all"]}, "receipts": "~/.local/state/example-tool/receipts.json"}
```

### prepare

The controller asks the participant to quiesce. `poll` counts the calls for this op (0 first). The participant
answers quickly: `ready` once it is quiesced, `busy` while work is still in flight (the controller calls again).

<!-- schema: prepare.params -->
```json
{"op": {"id": "op-20260101T120000-a1b2", "kind": "update", "targets": ["com.example.worker"], "deadline": 1767269100.0, "affected": {"pids": null}}, "poll": 0}
```

Busy (a result, not an error):

<!-- schema: prepare.result -->
```json
{"state": "busy", "op_id": "op-20260101T120000-a1b2", "quiesced": false, "eta_s": 40, "reason": "2 jobs running", "in_flight": [{"id": "job-17", "kind": "build", "what": "compile docs", "pid": 5151, "since": 1767268750.0, "interrupted": false}, {"id": "job-18", "kind": "sync", "what": "upload results", "interrupted": false}]}
```

Ready:

<!-- schema: prepare.result -->
```json
{"state": "ready", "op_id": "op-20260101T120000-a1b2", "quiesced": true, "in_flight": []}
```

Full response line for the busy answer:

<!-- schema: envelope.response -->
```json
{"svckit": "1.0", "id": "req-7", "ok": true, "result": {"state": "busy", "op_id": "op-20260101T120000-a1b2", "quiesced": false, "eta_s": 40, "in_flight": []}}
```

### complete

The controller reports how the operation ended; the participant resumes.

<!-- schema: complete.params -->
```json
{"op": {"id": "op-20260101T120000-a1b2", "kind": "update", "targets": ["com.example.worker"]}, "outcome": "ok", "facts": {"targets": {"com.example.worker": {"old_pid": 5150, "new_pid": 6001, "old_version": "1.3.2", "new_version": "1.4.0", "healthy": true, "restarted": true}}, "duration_s": 12.5, "waited_s": 41.0, "capped": false, "forced_now": false}}
```

<!-- schema: complete.result -->
```json
{"state": "resumed", "actions": ["resumed queue", "reloaded config"]}
```

## Errors

An error response (exit code 1, or 75 with `retry_after_s` for a temporary failure):

<!-- schema: envelope.response -->
```json
{"svckit": "1.0", "id": "req-9", "ok": false, "error": {"code": "requires_root", "message": "installing the system service needs root", "data": {"argv": ["sudo", "example-tool", "svc", "install", "--json"]}}}
```

<!-- schema: envelope.response -->
```json
{"svckit": "1.0", "id": "req-10", "ok": false, "error": {"code": "unsupported", "message": "method 'doctor' is not implemented"}}
```

## Lease files

The controller also writes the op to a durable **lease directory** that participant daemons watch, so a daemon
quiesces even if it is never called. One file per active op (`<op_id>.json`); finished ops move to
`done/<op_id>.json` (tombstones). Leases past `op.expires_at` are stale and are ignored (fail open).

<!-- schema: lease -->
```json
{"v": 1, "op": {"id": "op-20260101T120000-a1b2", "kind": "update", "targets": ["com.example.worker"], "reason": "upgrade to 1.4.0", "by": {"tool": "example-tool", "user": "alice"}, "started_at": 1767268800.0, "deadline": 1767269100.0, "expires_at": 1767270600.0, "affected": {"pids": [5150]}, "wait_for": "interrupted", "now": false, "lease": null}, "phase": "prepare", "phase_at": 1767268800.0, "outcome": null, "facts": null, "owner_uid": 501}
```

`phase` is `prepare` → `running` → `complete`; `outcome` (`ok`, `failed`, `partial`, `aborted`) is set at
`complete`. A human clearing a stuck lease ends it with outcome `aborted`.

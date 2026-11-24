# Coordination: prepare, run, complete

This page walks through one operation, "restart service X" (`com.example.worker`), with one controller and one
participant tool, `example-tool`, that also runs a daemon. Rationale is in [design.md](design.md); the method
payloads are in [contract.md](contract.md).

Setup: `example-tool` declares in `describe` that it cares about kinds `restart*` and services `com.example.*`
(`coordination.kinds`, `coordination.services`). Its daemon watches the lease directory.

## The sequence

1. **Relevance.** The controller calls `describe` on each participant (cached through `describe_cache`). A
   participant is relevant when the operation's kind matches one of its `kinds` globs and, if it lists `services`,
   one of the targets matches. No `coordination` block means not relevant.
2. **Announce.** `Operation.start()` builds the `Op` (`id` like `op-20260101T120000-a1b2`, `deadline = now +
   max_wait_s`, `expires_at = deadline + margin_s`) and writes the lease with phase `prepare`, **before** any
   `prepare` call. The daemon's watcher fires, `Quiescer.refresh()` sees a matching active lease and calls
   `on_prepare(lease)`: the daemon stops taking new work. This works even if the controller never calls the tool.
3. **Prepare.** The controller runs `example-tool svc prepare --json` with `params = {"op": {...}, "poll": n}`.
   The tool answers `{"state": "ready"}` or `{"state": "busy", "in_flight": [...]}` (a result, not an error; exit
   code 3). On `busy` the controller prints progress, sleeps `poll_s` and polls again (`poll` counts up) until all
   relevant participants are ready or `deadline` passes. At the cap it proceeds or aborts according to `on_cap`.
4. **Run.** The controller sets the lease phase to `running` and performs the restart. Facts it learns go in
   `op.facts` (for example old and new pid).
5. **Complete.** On leaving the `with` block (or `finish()`), the controller adds `duration_s`, `waited_s`,
   `capped`, `forced_now` (and `error` on failure) to the facts and calls
   `example-tool svc complete --json` with `{"op": {...}, "outcome": "ok", "facts": {...}}`. The tool answers
   `{"state": "resumed"}`.
6. **Tombstone.** The lease is rewritten with phase `complete`, its outcome and the facts, and moved to
   `done/<op_id>.json`. The watcher fires again; `Quiescer` sees the tombstone and calls `on_complete(lease)`:
   the daemon resumes. If it was down, `catch_up()` on start fires for tombstones newer than the stored marker.

```
controller                    lease dir                      example-tool daemon
    | write <op>.json (prepare) -->|--- watch event ------------> on_prepare: stop taking work
    | svc prepare --json --------------------------------------> busy (job in flight)
    | ... poll_s ...
    | svc prepare --json --------------------------------------> ready
    | set phase running ---------->|
    | restart com.example.worker
    | svc complete --json -------------------------------------> resumed
    | move to done/<op>.json ----->|--- watch event ------------> on_complete: resume
```

If the controller dies between steps 2 and 6, nothing resumes early: the lease stays until `expires_at`, then
readers treat it as stale and the daemon gets `on_complete` with outcome `expired`. A human can end it at once with
`LeaseDir.clear(op_id, reason)` (outcome `aborted`).

## The lease file

`<lease_dir>/<op_id>.json`, mode 0600, written atomically (temp file, fsync, rename) under a lock:

```json
{"v": 1, "op": {"id": "op-20260101T120000-a1b2", "kind": "restart", "targets": ["com.example.worker"],
 "reason": "config change", "by": {"tool": "example-tool", "user": "alice"}, "started_at": 1767268800.0,
 "deadline": 1767269700.0, "expires_at": 1767270900.0, "affected": {"pids": [5150]},
 "wait_for": "interrupted", "now": false, "lease": null},
 "phase": "prepare", "phase_at": 1767268800.0, "outcome": null, "facts": null, "owner_uid": 501}
```

- `phase`: `prepare`, `running`, `complete`. `phase_at` is when it last changed.
- `outcome`: `ok`, `failed`, `partial` or `aborted`, set at `complete` (the daemon may also see `expired` or
  `missing`, which are derived, not stored).
- `facts`: free-form results of the operation; times are float UNIX timestamps.
- `affected.pids`: processes the operation will stop, or `null` when unknown.
- `v`: format version; a reader ignores files with a newer `v`.
- Tombstones live in `done/` and are pruned after 24 hours.

## How a participant decides `interrupted`

Each item in `prepare.result.in_flight` carries `interrupted`: would the operation break this item? A participant
decides per item from the op it was given:

- `op.affected.pids` lists pids: an item whose process (or a child) is in that list is interrupted. With `null`
  pids (unknown) the participant falls back to the targets: an item that runs inside one of `op.targets` is
  interrupted.
- An item that does not depend on the restarted service (it only talks to the daemon after it comes back, or can
  be retried safely) is `interrupted: false`.

The controller then applies `wait_for`. With the default `interrupted`, a `busy` answer blocks only while some
item has `interrupted: true`; with `all` every busy participant is waited for. `op.now` or `now=True` ignores all of
it after one `prepare` call. A participant that is unsure should say `interrupted: true`: waiting costs seconds,
interrupting costs work.

## Controller variants

- `with Operation(...)`: blocking, as above.
- `tick()` for periodic jobs: each call polls once and returns `"waiting"`, `"ready"` or `"capped"`. Pass the same
  `op_id` from other processes to reuse the lease; call `finish(outcome)` at the end.
- `mirror(event, op)` is called with `"start"` and `"end"` so legacy tools can keep their own marker files in step.

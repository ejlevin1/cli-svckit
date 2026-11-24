# Design

cli-svckit coordinates a **controller** (one tool that restarts or updates things) with **participants** (tools
whose daemons must quiesce first). This page explains the choices. The sequence itself is in
[coordination.md](coordination.md), the wire format in [contract.md](contract.md).

## Why CLI hooks plus a lease directory

Two mechanisms, each doing the job it is good at:

- **CLI hooks** (`<tool> svc prepare|complete --json`) are the conversation. The controller asks a tool
  "are you quiet?" and gets a structured answer with what is still in flight. Any tool can implement this in any
  language, and nothing has to be running for the call to work.
- **A lease directory** is the durable announcement. The controller writes one JSON file per operation before it
  touches anything. Participant daemons watch the directory and quiesce when a matching lease appears, even if
  nobody called them.

Why not a central coordinator process that tracks operations in memory:

- A coordinator that is restarted in the middle of an operation **loses its state**, or comes back and **resumes
  participants early**, while the thing being restarted is still down. The lease is a file: it outlives the
  controller, and a new controller (or a human) can read it, finish it (`LeaseDir.complete`) or clear it
  (`LeaseDir.clear`, outcome `aborted`).
- It would need a port or socket, and with it discovery, authentication and a service of its own to keep alive.
  The lease directory needs only file permissions (directory mode 0700, files 0600, owner checked on read).
- The controller itself is often the thing being restarted. Putting the coordination state somewhere else than
  the controller process is the point.

## Delay-only semantics

Coordination can only **delay** an operation, never veto it. A participant answers `ready` or `busy`; `busy` makes
the controller wait and ask again until the participant is ready or the **cap** (`max_wait_s`) is reached. At the cap
the controller either proceeds (`on_cap="proceed"`, the default) or aborts (`on_cap="abort"`, outcome `aborted`).
`now=True` skips waiting: one `prepare` call is made so participants still hear about it, then the operation goes on.

By default (`wait_for="interrupted"`) `busy` only blocks when something in flight would actually be
interrupted (an `in_flight` item with `interrupted: true`). Items that would survive are reported but do not hold
things up. `wait_for="all"` (on the operation or on the participant) waits for every busy participant.

## Fail policies

A participant that cannot answer (not installed, crashes, times out, bad JSON) is treated by its `fail_policy`:

- `open` (default): the operation goes on; an unreachable tool never blocks an update. When `describe` fails the
  participant is skipped.
- `closed`: an unreachable participant counts as not ready and its `describe` failure makes it relevant, so the
  controller waits until the cap.

An error with code `unsupported` always counts as ready: the tool has nothing to quiesce. `complete` failures are
reported through `say` and never raised, because the operation already happened.

## Expiry

Every lease carries `deadline` (when the controller stops waiting) and `expires_at` (`deadline` plus a margin,
`margin_s`, default 1200 s). A lease past `expires_at` is **stale** and ignored by readers (fail open): a controller
that was killed cannot hold daemons quiesced forever. A daemon that was quiesced for such a lease gets `on_complete`
with outcome `expired`; if the file was removed by hand it gets outcome `missing`. `LeaseDir.expire_stale()` moves stale
leases to `done/` and `prune_tombstones()` deletes tombstones older than 24 hours.

## Overlapping operations

Several operations can be active at once, each with its own lease file. `Quiescer` fires `on_prepare` once per
matching op and `on_complete` once per op, and exposes `prepared` (op ids still in progress). A participant should
stay quiesced while `prepared` is non-empty, and resume only when the last one completes.

## Trust

A lease counts only when the file is owned by the daemon's own uid or root and its `owner_uid` field agrees. Files
that do not parse, or have a newer format version, are ignored with one warning and never deleted by a reader.

## Reaction times

Measured with the toy participant in general terms, from writing a lease to the daemon's `on_prepare`:

| backend | typical reaction | notes |
| --- | --- | --- |
| kqueue (macOS/BSD), inotify (Linux) | about 0.1 s | event driven, plus a 50 ms debounce |
| polling | about 0.5 s | `poll_interval` defaults to 1.0 s, so the worst case is about one interval |

Both are well under 2 s. `LeaseWatcher(backend="auto")` picks the best one available and `.backend` reports it.
The callback also fires once right after `start()`, so a daemon never misses a lease written while it was starting.

## Non-goals

No process management (launchd and systemd stay the supervisors), no network protocol, no authentication beyond
file ownership, no persistent history beyond 24 hours of tombstones.

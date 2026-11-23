# cli-svckit

A small toolkit for command-line tools that ship local background services, so that one controller can
manage a whole set of them the same way.

> **Status: pre-release (0.0.x).** Nothing usable yet; the API below is the plan, not the code.

## What it will do

1. **Service lifecycle.** A tool declares its background service once (a declarative spec). The library
   renders and manages it on the host's service manager: macOS launchd first (user agents and system
   daemons), with room for systemd later. Install, uninstall, start, stop, restart and status are
   idempotent, support dry runs, and uninstall removes only what install created.
2. **CLI install.** Put a tool's command on `PATH` with one marked line in a shell rc file, or a symlink
   into a user bin directory. Removal deletes only that exact marked line, backs the file up first, and
   refuses if the line was edited.
3. **A shared JSON contract.** Every participating tool answers
   `<tool> svc describe|health|doctor|install|uninstall|prepare|complete --json` with the same versioned
   envelope, so a controller can discover tools, read their service specs, check health and run doctor
   checks without hard-coding any of them.
4. **Operation coordination.** Before a disruptive operation (a service restart, a platform update, a
   reboot) the controller writes a durable lease and asks each interested tool to `prepare`; tools answer
   `ready` or `busy` and quiesce. After the operation, `complete` tells them the outcome so they can
   recover. Leases expire, so a controller that dies half way never freezes anyone. Daemons watch the
   lease directory (kqueue, with a polling fallback) and react in well under a second. No ports, no
   central daemon.
5. **Shared CLI conventions.** `--json`, `--dry-run`, exit codes 0 ok / 1 warn / 2 fail, health tables.
   Typer and Rich are optional extras; the core is standard library only.

## Design rules

- Generic: every name, label, path, host and port comes from the consuming tool. Nothing site-specific
  lives here.
- Small: launchd and systemd remain the supervisors. The library renders specs and talks to them; it is
  not a process manager.
- Python 3.11+, zero runtime dependencies in the core.

## Development

```sh
uv sync
scripts/install-hooks.sh    # once per clone: secret + system-info checks on commit and push
uv run pytest -q
uv run ruff check
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)

# cli-svckit

A small toolkit for command-line tools that ship local background services, so that one controller can
manage a whole set of them the same way.

> **Status: 0.1.0 (phase 0).** The contract and the coordination layer work today. Install from the git tag:
>
> ```sh
> pip install "cli-svckit @ git+https://github.com/ejlevin1/cli-svckit@v0.1.0"
> ```

## What works now

- **The JSON contract** (`cli_svckit.contract`): every participating tool answers
  `<tool> svc describe|health|doctor|install|uninstall|prepare|complete --json` with one versioned envelope
  (`Request`, `Response`, `Error`, exit codes, JSON Schemas). `server.run` serves a provider object;
  `client.Participant` calls a tool and `discovery` finds participants. Spec: [docs/contract.md](docs/contract.md).
- **Lease coordination** (`cli_svckit.coord`): before a disruptive operation the controller writes a durable lease
  file; leases expire, so a controller that dies never freezes anyone. `LeaseDir`, `Lease`.
- **Watcher** (`coord.watch.LeaseWatcher`): kqueue, inotify or polling, with a callback after every change.
- **Controller** (`coord.controller.Operation`): a context manager that writes the lease, calls `prepare` until
  every relevant participant is ready (or a cap is hit), runs your action, then calls `complete`.
- **Participant helpers** (`coord.participant`): `Interest` and `Quiescer` turn watcher events into one
  `on_prepare` and one `on_complete` per operation, and survive daemon restarts.
- **Health and doctor** (`cli_svckit.health`, `cli_svckit.doctor`): health results, HTTP/port/pid probes, a doctor
  runner with exit codes.
- **rc-file install** (`cli_svckit.rcfile`, `cli_svckit.receipts`): one marked line in a shell rc file or a
  symlink on `PATH`; removal backs up first and refuses if the line was edited.
- **Testing helpers** (`cli_svckit.testing`): `FakeParticipant`, `temp_lease_dir`, `assert_conforms`.

## Not yet

- launchd and systemd service backends (a declarative service spec rendered to the host's service manager).
- Rich renderers for health and doctor tables.
- A PyPI release (install from the git tag for now).

## A participant in about 20 lines

A daemon that pauses its work while an `update` of its service is in progress. The `svc` entry point answers the
controller's `prepare` and `complete` calls; the lease watcher covers the case where nobody calls it.

```python
import sys
from cli_svckit.contract.server import run
from cli_svckit.coord.lease import LeaseDir
from cli_svckit.coord.participant import Interest, Quiescer
from cli_svckit.coord.watch import LeaseWatcher

class Provider:
    def describe(self, params):
        return {"tool": {"name": "example-tool", "version": "1.0"},
                "coordination": {"kinds": ["update"], "services": ["com.example.*"], "watches_lease": True}}
    def prepare(self, params):
        return {"state": "ready", "op_id": params["op"]["id"], "quiesced": True, "in_flight": []}
    def complete(self, params):
        return {"state": "resumed", "actions": []}

def daemon(lease_path):
    leases = LeaseDir(lease_path)
    leases.ensure()
    q = Quiescer(leases, Interest(["update"], ["com.example.*"]),
                 on_prepare=lambda lease: print("pausing for", lease.op.id),
                 on_complete=lambda lease: print("resuming:", lease.outcome))
    q.catch_up()
    LeaseWatcher(leases.path, q.refresh).start()

if __name__ == "__main__":
    sys.exit(run(Provider(), sys.argv[2:]))   # example-tool svc <method> --json
```

A longer, runnable version is `examples/toy_participant.py`.

## A controller in about 15 lines

```python
import os
from cli_svckit.contract.client import Participant
from cli_svckit.coord.controller import Operation
from cli_svckit.coord.lease import LeaseDir

leases = LeaseDir(os.path.expanduser("~/.local/state/example-tool/leases"))
workers = [Participant("example-tool", ["example-tool"])]

op = Operation("update", ["com.example.worker"], lease_dir=leases, participants=workers,
               reason="upgrade to 1.4.0", by={"tool": "example-tool", "user": "alice"},
               max_wait_s=300, on_cap="proceed")
with op:                                   # writes the lease, calls prepare until ready, sets phase running
    restart_service("com.example.worker")  # your code
    op.facts["new_version"] = "1.4.0"      # stored on the lease and passed to complete
# leaving the block calls complete on each participant and moves the lease to done/
```

`restart_service` stands for your own code. If it raises, the operation ends with outcome `failed`. If participants
are not ready at the cap and `on_cap="abort"`, `OperationAborted` is raised and the lease ends as `aborted`.

## Further reading

- [docs/design.md](docs/design.md): why hooks plus a lease directory, and the semantics.
- [docs/coordination.md](docs/coordination.md): the full prepare, run, complete sequence and the lease file.
- [docs/contract.md](docs/contract.md): the wire contract.

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

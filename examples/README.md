# Examples

## `toy_participant.py`

A complete participant written with the standard library and `cli_svckit` only.

```sh
# Contract entry point (request JSON on stdin, one JSON line out)
echo '{}' | python toy_participant.py svc describe --json

# Daemon: watches a lease directory and logs events
python toy_participant.py daemon --lease-dir /tmp/example-leases --state /tmp/example-state
```

- `svc describe|health|prepare|complete` implement the contract. The tool declares coordination kinds
  `service.*` and `x-example.*` for the service `example-svc`, and `watches_lease: true`.
- `prepare` answers `busy` (exit code 3) while a file named `busy` exists in the state directory, otherwise `ready`.
  The state directory comes from `--state`, then `$EXAMPLE_TOY_STATE`, then a folder in the system temp dir.
- `daemon` runs a `LeaseWatcher` feeding a `Quiescer` and appends lines such as
  `{"event": "prepare", "op_id": "op-20250101T000000-ab12", "t": 1735689600.0}` to `<state>/events.jsonl`
  (`event` is `prepare` or `complete`). Set `SVCKIT_WATCH_BACKEND` to `auto`, `kqueue`, `inotify` or `poll`.

Check any participant against the contract in a test:

```python
from cli_svckit.testing import assert_conforms


def test_my_tool_conforms():
    assert_conforms(["python", "toy_participant.py"])
```

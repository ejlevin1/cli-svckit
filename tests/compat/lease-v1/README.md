# Lease format v1 samples

Synthetic lease files in the `"v": 1` format as written by cli-svckit 0.1.0 (`coord/lease.py`). All values are
made up; `owner_uid` is a placeholder that `tests/test_compat_lease.py` replaces with the uid running the tests
(the reader only trusts files whose `owner_uid` matches the file owner).

| file            | where it lives  | meaning                                                   |
|-----------------|-----------------|-----------------------------------------------------------|
| `prepare.json`  | lease dir       | active op in phase `prepare`                              |
| `running.json`  | lease dir       | active op in phase `running`; carries unknown fields      |
| `expired.json`  | lease dir       | op whose `expires_at` has passed (stale, not active)      |
| `complete.json` | `done/`         | tombstone, outcome `ok` with completion facts             |
| `cleared.json`  | `done/`         | tombstone of a human `clear`: outcome `aborted`           |

**Never edit these files** to make a test pass: they are the v1 format as released (policy P2 in
`docs/compatibility.md`). A change that cannot read them with the same meaning needs lease format `v: 2`; add new
samples in a `lease-v2/` folder instead.

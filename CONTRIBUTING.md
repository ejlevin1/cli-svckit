# Contributing

## Setup
```sh
brew install gitleaks uv      # or your platform's equivalent
uv sync
scripts/install-hooks.sh      # sets core.hooksPath=.githooks for this clone
```

## Public-repo guard
Every commit and push runs `scripts/check-public.sh`:

- **gitleaks** for secrets;
- **generic system-info patterns** (home paths, private and CGNAT IPs, email addresses other than
  example.com / GitHub noreply, Cloudflare Access domains);
- **your personal patterns**, read from `~/.config/public-check/patterns` (or `$PUBLIC_CHECK_PATTERNS`).
  Put your own names, domains and host names there, one extended regex per line. That file is never
  committed: a committed deny-list would itself be the leak.

The pre-push hook re-checks every pushed commit, so a commit made with `--no-verify` is still caught.
CI runs the same checks over the tree and the full history.

A false positive goes in `.public-check-allow` as a narrow regex with a comment explaining it.

## Commits
- Conventional commit messages (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Author with your GitHub noreply address
  (`git config user.email <id>+<login>@users.noreply.github.com`).

## Compatibility
The [compatibility policy](docs/compatibility.md) protects the contract schemas, the lease format and the Python
API against the previous release. Before opening a pull request, run:

```sh
git fetch --tags
uv run --locked python scripts/compat_check.py
uv run --locked --all-extras -q pytest -q tests/test_compat_schemas.py tests/test_compat_lease.py
```

The cross-version test needs `SVCKIT_PREV_SRC` (see the policy page).

An allowed breaking change to the Python API (while 0.x) bumps the MINOR version and adds a `### Breaking` heading
listing it to the top `CHANGELOG.md` section. Contract and lease breakages need a new contract major or `"v": 2`
instead; see the policy page.

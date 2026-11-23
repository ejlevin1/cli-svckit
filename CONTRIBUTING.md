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

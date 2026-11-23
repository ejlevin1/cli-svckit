# Agent rules for this repo

This repository is **public**. Treat every byte you commit as published.

## Hard rules
1. No secrets, tokens, keys or credentials, ever. Not in code, tests, fixtures, docs or commit messages.
2. No system or personal information: no real names, home-directory paths (`/Users/<name>/`,
   `/home/<name>/`), host names, private IPs, internal domains, ports of real deployments, or email
   addresses other than `@example.com` / GitHub noreply. Use `~`, `<home>`, `example.com`,
   `alice` / `bob` and `203.0.113.x` (TEST-NET) in examples.
3. Never paste captured command output from a real machine into the repo; write synthetic examples.
4. Run `scripts/check-public.sh --staged` before every commit (the pre-commit hook does this once
   `scripts/install-hooks.sh` has been run). Never commit with `--no-verify`.
5. Core package (`src/cli_svckit/`) uses the standard library only. Typer/Rich/psutil are optional
   extras and must be imported lazily.
6. Everything site-specific is passed in by the consuming tool: the library has no defaults that name a
   real product, path or host.
7. Tests must not touch the real service manager, real shell rc files or real lease directories: use
   temp dirs and fakes.

## Commands
- Tests: `uv run pytest -q`
- Lint: `uv run ruff check`
- Public check: `scripts/check-public.sh --all` (tree) / `--history` (all commits)

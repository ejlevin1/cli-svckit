#!/usr/bin/env bash
# check-public.sh — keep secrets and machine/system details out of a public repo.
#   --staged (pre-commit) | --range "<revs>" (pre-push) | --all (tree, CI) | --history (every commit, CI)
# Layers: gitleaks (secrets) + generic system-info regexes below + PERSONAL regexes from a file that is never
# committed: ${PUBLIC_CHECK_PATTERNS:-$HOME/.config/public-check/patterns}. Exceptions: .public-check-allow (narrow
# regexes matched against the offending line). Reviewed pre-public history: .public-check-baseline (one sha per line;
# --history skips commits reachable from them; gitleaks still scans everything).
# Scans with ONE git grep -E -f pass and filters only the hits: a per-line x per-pattern loop takes > 10 min on real repos.
set -euo pipefail
mode="${1:---staged}"; arg="${2:-}"
root="$(git rev-parse --show-toplevel)"; cd "$root"
red() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
GENERIC=(
  '/Users/[A-Za-z0-9._-]+/'
  '/home/[a-z_][a-z0-9_-]*/'
  '[A-Za-z]:\\\\Users\\\\'
  '(^|[^0-9.])10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
  '(^|[^0-9.])192\.168\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
  '(^|[^0-9.])172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
  '(^|[^0-9.])100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)'
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}'
  '/\.hermes/installs/[0-9a-f]{8,}'
  '\.cloudflareaccess\.com'
)
EMAIL_OK='@(example\.(com|org|net)|users\.noreply\.github\.com|localhost)\b|noreply@'
personal="${PUBLIC_CHECK_PATTERNS:-$HOME/.config/public-check/patterns}"
patterns=("${GENERIC[@]}")
if [[ -f "$personal" ]]; then
  while IFS= read -r l; do [[ -z "${l// }" || "$l" == \#* ]] || patterns+=("$l"); done < "$personal"
elif [[ -z "${CI:-}" ]]; then red "public-check: no personal pattern file at $personal (generic checks only)."; fi
allow=()
[[ -f .public-check-allow ]] && while IFS= read -r l; do [[ -z "${l// }" || "$l" == \#* ]] || allow+=("$l"); done < .public-check-allow

# ---- 1. secrets ----
command -v gitleaks >/dev/null 2>&1 || { red "public-check: gitleaks not installed"; exit 1; }
case "$mode" in
  --staged)  gitleaks git --pre-commit --staged --no-banner --redact -l warn . ;;
  --range)   gitleaks git --log-opts="$arg" --no-banner --redact -l warn . ;;
  --all)     gitleaks dir --no-banner --redact -l warn . ;;
  --history) gitleaks git --log-opts="--all" --no-banner --redact -l warn . ;;
  *) red "usage: $0 --staged | --range REVS | --all | --history"; exit 2 ;;
esac || { red "public-check: gitleaks found a secret. Remove it; rotate it if it was ever pushed."; exit 1; }

# ---- 2. system info ----
patfile="$(mktemp)"; trap 'rm -f "$patfile"' EXIT
printf '%s\n' "${patterns[@]}" > "$patfile"
pathspec=(-- . ':!scripts/check-public.sh' ':!.public-check-allow' ':!.public-check-baseline')
history_excludes() { [[ -f .public-check-baseline ]] && grep -oE '^[0-9a-f]{7,40}' .public-check-baseline | sed 's/^/^/' || true; }
hits_raw() {
  case "$mode" in
    --staged|--range)
      if [[ "$mode" == --staged ]]; then git diff --cached -U0 --no-color --diff-filter=ACMR; else git log -p -U0 --no-color --format= $arg; fi |   # $arg word-split on purpose
      awk '/^\+\+\+ b\//{f=substr($0,7); next} /^@@/{split($3,a,","); n=substr(a[1],2)+0; next}
           /^\+/ && f!="" {print f "\t" n "\t" substr($0,2); n++}' |
      awk -F'\t' '$1!="scripts/check-public.sh" && $1!=".public-check-allow" && $1!=".public-check-baseline"' |
      while IFS=$'\t' read -r f n text; do
        grep -qE -f "$patfile" <<<"$text" 2>/dev/null && printf '%s:%s:%s\n' "$f" "$n" "$text"
      done ;;
    --all)     git grep -n -I -E -f "$patfile" "${pathspec[@]}" || true ;;
    --history) git grep -n -I -E -f "$patfile" $(git rev-list --all $(history_excludes)) "${pathspec[@]}" 2>/dev/null |
               sed -E 's/^[0-9a-f]{40}://' | sort -u || true ;;
  esac
}
hits=0
while IFS= read -r rec; do
  [[ -z "$rec" ]] && continue
  f="${rec%%:*}"; rest="${rec#*:}"; text="${rest#*:}"; real=0
  for p in "${patterns[@]}"; do
    m="$(grep -oE -- "$p" <<<"$text" | head -1 || true)"; [[ -z "$m" ]] && continue
    if [[ "$m" == *@* ]] && grep -qE -- "$EMAIL_OK" <<<"$m"; then continue; fi
    ok=0; for a in "${allow[@]+"${allow[@]}"}"; do grep -qE -- "$a" <<<"$text" && { ok=1; break; }; done
    (( ok )) && continue
    red "system info: ${f}:${rest%%:*}: matched /${p}/"; real=1; break   # never echo the match itself
  done
  (( real )) && hits=$((hits+1))
done < <(hits_raw)
(( hits )) && { red "public-check: $hits line(s) carry machine/system details; use ~, <home>, example.com."; exit 1; }
echo "public-check: ok ($mode)"

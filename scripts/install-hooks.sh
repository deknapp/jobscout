#!/usr/bin/env bash
# Install a pre-commit hook that refuses to commit personal material.
#
# The privacy tests already cover this, but a hook catches it a step earlier —
# before the mistake is in a commit at all.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p .git/hooks
cat > .git/hooks/pre-commit <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail

staged=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$staged" ] && exit 0

bad=$(echo "$staged" | grep -Ei '\.(pdf|docx|doc|rtf)$|(^|/)\.env$|(^|/)history\.jsonl$|(^|/)profile\.json$|(^|/)companies\.json$' || true)
if [ -n "$bad" ]; then
  echo "pre-commit: refusing to commit personal material:" >&2
  echo "$bad" | sed 's/^/  /' >&2
  echo "These belong in your data dir, outside the repo." >&2
  exit 1
fi

# The privacy test and this installer both name these patterns on purpose.
checkable=$(echo "$staged" | grep -Ev 'tests/test_privacy\.py|scripts/install-hooks\.sh' || true)
if [ -n "$checkable" ] && git diff --cached -U0 -- $checkable | grep -Eq '^\+.*(/Users/|/home/[a-z])'; then
  echo "pre-commit: a home-directory path is being added to a tracked file." >&2
  echo "Personal paths belong in .env, which is git-ignored." >&2
  exit 1
fi
HOOK

chmod +x .git/hooks/pre-commit
echo "installed .git/hooks/pre-commit"

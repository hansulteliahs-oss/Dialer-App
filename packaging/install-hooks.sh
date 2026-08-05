#!/usr/bin/env bash
# Installs the secret-scanning pre-commit hook. Run once after cloning.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p .git/hooks
cat > .git/hooks/pre-commit <<'HOOK'
#!/usr/bin/env bash
# Refuse any commit carrying live Twilio or Airtable credentials.
# This repo has a GitHub remote and holds keys that can place billable calls.
set -uo pipefail

staged=$(git diff --cached --name-only --diff-filter=ACM)
[[ -z "$staged" ]] && exit 0

# Real tokens only - .env.example placeholders (xxxx...) must not trip this.
patterns=(
  'AC[0-9a-f]{32}'                                  # Twilio Account SID
  'SK[0-9a-f]{32}'                                  # Twilio API Key SID
  'pat[A-Za-z0-9]{14}\.[A-Za-z0-9]{64}'             # Airtable PAT
  'key[A-Za-z0-9]{14}'                              # legacy Airtable API key
)

fail=0
for f in $staged; do
  [[ "$f" == ".git/hooks/pre-commit" ]] && continue
  for p in "${patterns[@]}"; do
    if git show ":$f" 2>/dev/null | grep -nEq "$p"; then
      echo "BLOCKED: $f matches credential pattern /$p/" >&2
      git show ":$f" | grep -nE "$p" | sed 's/^/    /' | cut -c1-120 >&2
      fail=1
    fi
  done
done

if [[ "$fail" -ne 0 ]]; then
  cat >&2 <<'MSG'

Commit refused - live credentials detected in staged content.
Move the value into .env (gitignored) and reference it via os.environ.
Override only if you are certain: git commit --no-verify
MSG
  exit 1
fi
exit 0
HOOK

chmod +x .git/hooks/pre-commit
echo "installed .git/hooks/pre-commit"

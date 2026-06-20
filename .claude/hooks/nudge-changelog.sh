#!/usr/bin/env bash
#
# PreToolUse hook (matcher: Bash) — NON-BLOCKING. On `git commit`, nudge if the staged
# diff touches behavior/schema surfaces (backend/, ddl/, docs/schemas/) but does not also
# touch CHANGELOG.md. CLAUDE.md asks every behavior/schema/dependency/build change to add
# a CHANGELOG [Unreleased] entry.
#
# Never blocks: emits a systemMessage + additionalContext on stdout and exits 0.
set -euo pipefail

input="$(cat)"
command="$(printf '%s' "$input" | jq -r '.tool_input.command // ""')"
cwd="$(printf '%s' "$input" | jq -r '.cwd // ""')"

# Only fire on a `git commit`.
if ! printf '%s' "$command" | grep -Eq '(^|[^[:alnum:]_])git[[:space:]]+commit([[:space:]]|$)'; then
    exit 0
fi

[[ -n "$cwd" ]] && cd "$cwd" 2>/dev/null || true

staged="$(git diff --cached --name-only 2>/dev/null || true)"
[[ -z "$staged" ]] && exit 0

if printf '%s' "$staged" | grep -Eq '(^|/)(backend|ddl|docs/schemas)/' \
   && ! printf '%s' "$staged" | grep -q 'CHANGELOG.md'; then
    cat <<'JSON'
{
  "systemMessage": "Staged changes touch backend/ddl/schema but not CHANGELOG.md.",
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": "CLAUDE.md asks every PR that changes behavior, schema, dependencies, or build steps to add a CHANGELOG.md [Unreleased] entry (what + why + PR ref). Consider running /changelog before committing; the commit was NOT blocked."
  }
}
JSON
fi

exit 0

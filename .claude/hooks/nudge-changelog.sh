#!/usr/bin/env bash
#
# PreToolUse hook (matcher: Bash) — NON-BLOCKING. On `git commit`, nudge if the staged
# diff touches behavior/schema surfaces (backend/, ddl/, docs/schemas/) but does not also
# touch CHANGELOG.md. CLAUDE.md asks every behavior/schema/dependency/build change to add
# a CHANGELOG [Unreleased] entry.
#
# Never blocks: emits a systemMessage + additionalContext on stdout and exits 0.
#
# Parsing uses python3 (guaranteed by the project's Python stack), not jq. As an advisory
# nudge it fails OPEN: if no parser is available it stays silent (exit 0).
set -euo pipefail

command -v python3 >/dev/null 2>&1 || exit 0

input="$(cat)"
command="$(printf '%s' "$input" | python3 -c 'import sys, json
try:
    print(json.load(sys.stdin).get("tool_input", {}).get("command", ""))
except Exception:
    pass')"
cwd="$(printf '%s' "$input" | python3 -c 'import sys, json
try:
    print(json.load(sys.stdin).get("cwd", ""))
except Exception:
    pass')"

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

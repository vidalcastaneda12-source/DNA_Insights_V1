#!/usr/bin/env bash
#
# PreToolUse hook (matcher: Bash) — NON-BLOCKING. On `git commit`, warn if the staged
# diff still contains a `GATE-FILL` marker (a placeholder meant to be replaced with a
# real-data number confirmed at the verification gate before the change is committed).
#
# Never blocks: emits a systemMessage + additionalContext on stdout and exits 0, so the
# commit proceeds and the model/user simply sees the nudge.
#
# Parsing uses python3 (guaranteed by the project's Python stack), not jq. As an
# advisory nudge it fails OPEN: if no parser is available it stays silent (exit 0) rather
# than interfering with the commit.
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

if git diff --cached 2>/dev/null | grep -q 'GATE-FILL'; then
    cat <<'JSON'
{
  "systemMessage": "GATE-FILL marker found in staged changes.",
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": "The staged diff still contains a GATE-FILL placeholder. These mark numbers to be replaced with real-data values confirmed at the verification gate. Confirm this is intentional before committing; the commit was NOT blocked."
  }
}
JSON
fi

exit 0

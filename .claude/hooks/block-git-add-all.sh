#!/usr/bin/env bash
#
# PreToolUse hook (matcher: Bash) — enforce local-first privacy (CLAUDE.md decision #9)
# and the raw-DNA-exports-untracked rule: never bulk-stage the working tree. Raw 23andMe
# / Ancestry exports and runtime data live in the tree (gitignored), and a stray
# `git add -A` / `git add .` is exactly how an unignored export or secret slips into a
# commit. Stage by explicit path instead.
#
# Hard block (no override). Denies `git add` invoked with -A / --all / a bare `.`.
#
# Contract: reads the PreToolUse JSON envelope on stdin; emits a permissionDecision JSON
# on stdout with exit 0. Parsing uses python3 (guaranteed by the project's Python stack);
# NOT jq, which is not universally installed and would make this hook exit non-zero =
# fail OPEN where absent. If no parser is available the hook FAILS CLOSED — a hard block
# must never silently stop guarding.
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Blocked (fail-closed): the git-add guardrail needs python3 to parse the hook input and it is not on PATH. Install python3, or stage by explicit path. The guardrail refuses to run blind rather than let a bulk `git add` through."
  }
}
JSON
    exit 0
fi

input="$(cat)"
command="$(printf '%s' "$input" | python3 -c 'import sys, json
try:
    print(json.load(sys.stdin).get("tool_input", {}).get("command", ""))
except Exception:
    pass')"

# Only consider commands that invoke `git add`...
if printf '%s' "$command" | grep -Eq '(^|[^[:alnum:]_])git[[:space:]]+add([[:space:]]|$)'; then
    # ...and stage everything via -A / --all / a standalone `.` token.
    if printf '%s' "$command" | grep -Eq '(^|[[:space:]])(-A|--all|\.)([[:space:]]|$)'; then
        cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Blocked: `git add -A | --all | .` bulk-stages the working tree. Raw DNA exports and runtime data live untracked in the tree (CLAUDE.md privacy decision #9); a bulk add is how an unignored export or secret slips into a commit. Stage by explicit path: `git add <path> ...`."
  }
}
JSON
        exit 0
    fi
fi

exit 0

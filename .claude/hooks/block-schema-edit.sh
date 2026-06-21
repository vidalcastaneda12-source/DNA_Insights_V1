#!/usr/bin/env bash
#
# PreToolUse hook (matcher: Edit|Write) — enforce CLAUDE.md "Things never to do" #1:
# the schema markdown under docs/schemas/ and the DDL extracted from it (ddl/) are
# immutable except via a deliberate, documented schema change. Blocks any Edit/Write
# whose target path is under docs/schemas/ or ddl/.
#
# Override (the deliberate-change escape): set GENOME_ALLOW_SCHEMA_CHANGE=1.
#
# Contract: reads the PreToolUse JSON envelope on stdin; emits a permissionDecision JSON
# on stdout with exit 0 (deny to block, no output to fall through). Parsing uses python3
# (guaranteed by the project's Python stack); NOT jq, which is not universally installed
# and would make this hook exit non-zero = fail OPEN where absent. If no parser is
# available the hook FAILS CLOSED — a schema-immutability block must never silently lapse.
set -euo pipefail

# The deliberate-change escape takes precedence over everything (incl. the parser guard).
if [[ "${GENOME_ALLOW_SCHEMA_CHANGE:-0}" == "1" ]]; then
    exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Blocked (fail-closed): the schema-immutability guardrail needs python3 to parse the hook input and it is not on PATH. Install python3, or set GENOME_ALLOW_SCHEMA_CHANGE=1 for a deliberate, documented schema change. The guardrail refuses to run blind rather than let a schema edit through."
  }
}
JSON
    exit 0
fi

input="$(cat)"
file_path="$(printf '%s' "$input" | python3 -c 'import sys, json
try:
    print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))
except Exception:
    pass')"

if printf '%s' "$file_path" | grep -Eq '(^|/)(docs/schemas|ddl)/'; then
    cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Blocked: docs/schemas/ and ddl/ are immutable except via a deliberate, documented schema change (CLAUDE.md 'Things never to do' #1). Do NOT mutilate the schema to dodge an FTS5 failure. To make an intentional schema change, re-run with GENOME_ALLOW_SCHEMA_CHANGE=1 and follow the rebuild protocol (rm -rf data/ && uv run genome init + re-ingest)."
  }
}
JSON
    exit 0
fi

exit 0

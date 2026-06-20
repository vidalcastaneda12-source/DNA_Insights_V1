#!/usr/bin/env bash
#
# PreToolUse hook (matcher: Edit|Write) — enforce CLAUDE.md "Things never to do" #1:
# the schema markdown under docs/schemas/ and the DDL extracted from it (ddl/) are
# immutable except via a deliberate, documented schema change. Blocks any Edit/Write
# whose target path is under docs/schemas/ or ddl/.
#
# Override (the deliberate-change escape): set GENOME_ALLOW_SCHEMA_CHANGE=1.
#
# Contract: reads the PreToolUse JSON envelope on stdin; emits a permissionDecision
# JSON on stdout with exit 0 (deny to block, no output to fall through to the default
# flow). See docs/findings/finding-034-agent-team-plan-phase.md and `claude-code-guide`.
set -euo pipefail

input="$(cat)"
file_path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // ""')"

if [[ "${GENOME_ALLOW_SCHEMA_CHANGE:-0}" == "1" ]]; then
    exit 0
fi

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

#!/usr/bin/env bash
#
# Convenience executor for the verification protocol documented in
# docs/runbooks/verification.md. Runs the five core commands in order
# with section headers and clear pass/fail output. The runbook remains
# the canonical protocol; this script is a thin wrapper for the
# always-run portion. Schema changes and pipeline changes have
# additional steps documented in the runbook.

set -euo pipefail

# Keep pytest + DuckDB scratch off the system /tmp (small/slow on WSL2) and
# give each run a clean slate. See docs/runbooks/verification.md.
REPO_ROOT="$(git rev-parse --show-toplevel)"
export TMPDIR="${REPO_ROOT}/.verify-tmp"
rm -rf "${TMPDIR}"
mkdir -p "${TMPDIR}"

run_step() {
    local label="$1"
    shift
    printf '\n=== %s ===\n' "$label"
    if ! "$@"; then
        printf '\nFAILED at %s\n' "$label" >&2
        exit 1
    fi
}

run_step "uv sync" uv sync
run_step "pytest" uv run pytest
run_step "ruff check" uv run ruff check
run_step "ruff format --check" uv run ruff format --check
run_step "mypy --strict backend/src" uv run mypy --strict backend/src

printf '\nAll checks passed\n'
printf 'For schema or pipeline changes, see docs/runbooks/verification.md for additional steps.\n'

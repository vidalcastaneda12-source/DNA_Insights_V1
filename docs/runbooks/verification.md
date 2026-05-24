# Verification Protocol Runbook

This document is the canonical merge gate for changes to this repository.
It is run by VSC-User (the human operator) in the integrated terminal,
independently of any tests VSC-Claude executed during implementation.

## Purpose

VSC-Claude runs tests while implementing a change. That run happens
inside the model's working session, against the files the model just
wrote, and is part of the same loop that produced the change. The
operator's independent run is what catches the cases where the
implementation loop produced a clean signal that doesn't reflect the
underlying truth — selective test runs (e.g. only the new tests),
test mutation (e.g. fixtures shaped to match the implementation rather
than the source), and number-interpretation slippage (e.g. accepting
real-data drift as expected because the model said so). The protocol
below is what the operator runs from a fresh shell, against the
current branch, before merging.

The protocol scales with the kind of change. Every PR runs the core
commands; PRs that touch the schema add the rebuild step; PRs that
touch the pipeline add the real-data verification step. Optional
steps are explicitly out of contract — if a PR's change class is
covered by the core commands alone, that is the full protocol.

## Core commands

For convenience, `scripts/verify.sh` runs all five checks in order with
section headers and clear pass/fail output. The commands below remain
the canonical protocol; the script is a thin wrapper for the
always-run portion, and the schema-rebuild and pipeline-verification
sections below still apply on top of either invocation.

Run from the repository root, in the order listed:

```
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy --strict backend/src
```

All five must complete cleanly. `uv sync` is included because a
stale virtualenv against a `pyproject.toml` change can mask import or
type errors that the freshly-resolved environment would surface.

`ruff check` (lint) and `ruff format --check` (formatting) are
distinct gates. A file can satisfy the lint rules while still drifting
from the formatter's canonical layout, so both must run. `ruff
format --check` reports what it would reformat without writing any
changes; the local fix is `uv run ruff format <path>`.

## Additional steps for schema changes

If the PR touches `docs/schemas/` or `ddl/`, the local DuckDB and
SQLite files do not auto-migrate. Run the rebuild before the core
commands:

```
rm -rf data/
uv run genome init
```

Then re-ingest the real-data corpus and re-run any pipeline stages
the change exercises. The per-source runbooks document the
re-ingest and refresh sequences:

* Phase 2 / 3 / 4 (chip ingest, merge, imputation):
  [`imputation.md`](imputation.md) — in particular the
  "Rebuilding from a preserved archive" section, which covers the
  prepare → run → import sequence after `rm -rf data/`.
* Phase 5 (annotation refreshes):
  [`annotations.md`](annotations.md) — the "After a schema rebuild"
  section lists every `genome annotate refresh --source <db>`
  invocation needed to restore the annotation state.

After the re-ingest, verify the expected row counts and stable
identifiers documented in CLAUDE.md "Real-data observations" before
running the core commands.

## Additional steps for functional changes that touch the pipeline

If the PR changes ingest, merge, imputation, or any pipeline stage
that produces the durable real-data numbers, run the relevant
pipeline step against the real-data corpus and confirm the stable
identifiers documented in CLAUDE.md "Real-data observations":

* Phase 3 merge: `consensus_genotypes` row count (942,620 chip-derived
  rows in the Phase 4 extended view; 3,210,371 total),
  `both_concordant=120,516`, `disagreement_resolved=106`,
  `single_source=821,998`, shared-call concordance=1.0000,
  `strand_flip_resolutions=106`, palindromic shared variants=31.
* Phase 4 imputation: input 204,153 polymorphic SNVs (chr1–chr22 + X),
  imputed output at DR² > 0.3 = 2,369,171, mean DR² 0.8242,
  high-quality (DR² > 0.8) = 1,592,735, chrX imputed = 0 for males,
  full-genome runtime ~30 min on 16 threads / 8 GB heap.

Drift in any of these numbers against the same input corpus is a
regression signal, not noise. The numbers are recorded as stable
identifiers in CLAUDE.md precisely so that an independent
verification run can compare against a known answer rather than
re-deriving the expectation from the same code that produced the
output.

PRs that touch annotation loaders should additionally confirm the
real-data drift identifiers locked in `annotations.md` for the
affected source (e.g. the gnomAD `rows_loaded`, `match_rate`, and
per-population AF presence numbers in the
`### gnomAD (sub-phase 5.5)` section).

## When the protocol fails

If any step fails, do not attempt to fix the failure locally before
reporting. Send the verbatim failure output back to the planning chat
along with the failing command. The planning chat decides whether the
failure is a real regression in the PR, drift in something the PR
did not touch, or environment skew on the operator's machine, and
hands the appropriate scoped session back to VSC-Claude.

Attempting a local fix before reporting risks two failure modes:
silently fixing a real regression without it being captured in the
PR, and fixing an environment problem that should have been flagged
as a pre-existing condition rather than rolled into the PR's
changes.

The one exception is `uv sync` — if `uv sync` itself fails (network
error, lockfile drift), retry once before reporting; intermittent
sync failures are not a useful signal.

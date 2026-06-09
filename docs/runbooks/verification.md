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

`verify.sh` exports `TMPDIR` to a gitignored repo-local directory
(`.verify-tmp/`) and clears it at the start of each run, so pytest and
DuckDB scratch never touch the system `/tmp` and cannot accumulate
across runs. A bare `uv run pytest` during development does not set
`TMPDIR`, so it still writes to `/tmp/pytest-of-$USER`; if a dev run
fills `/tmp`, clear it with `rm -rf /tmp/pytest-of-$USER`.

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
  **Post-PR-3 (canonicalize step) these numbers re-lock per
  finding-020 — shared-call concordance specifically DROPS from
  1.0000 by design. If `< 1.0000`, this is the post-PR-3 re-lock
  value, not a regression; see the finding's bedrock anchor table
  for the exact post-canonicalize numbers and the
  correction-not-regression framing.**
* Phase 4 imputation: input 204,153 polymorphic SNVs (chr1–chr22 + X),
  imputed output at DR² > 0.3 = 2,369,171, mean DR² 0.8242,
  high-quality (DR² > 0.8) = 1,592,735, chrX imputed = 0 for males,
  full-genome runtime ~30 min on 16 threads / 8 GB heap.
* PR-3 canonicalize step (`genome annotate canonicalize-variants` →
  `genome merge` → `genome annotate align-tier3-consensus` →
  `genome annotate refresh-index`): the bedrock anchor table in
  finding-020 lists every post-PR-3 locked number. Headline checks:
  `gnomad_matches` / `clinvar_matches` rise dramatically from
  101,501 / 2,559; `gwas_matches=66,726` and `pharmgkb_matches=1,737`
  stay unchanged; the post-`align-tier3` `consensus_genotypes WHERE
  consensus_method='disagreement_resolved'` count drops to 53 (one per
  pair on the canonical side) from the post-merge 106.

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

### Canonicalize backfill gate (PR-3 / finding-020) — VSC-User only

The PR-3 canonicalize re-lock is verified on the **swept real-data DB** — the #66
`genome imputation normalize-rsids` sweep must already have run (see the reload
sequence in `annotations.md`). VSC-ClaudeCode does NOT run this; it runs only the
synthetic-fixture dev-loop. Two checkpoints bracket the canonicalize run so any
wrong number at B is attributable to canonicalize, not a bad starting state.

Sequence:

1. Restore the pre-canonicalize snapshot onto the swept DB; confirm the prior
   discrepancy count.
2. **Checkpoint A** (pre-canonicalize) — capture the merge anchors (below) and
   assert the negative control: 942,620 chip-consensus / 120,516 `both_concordant`
   / 821,998 `single_source` / concordance 1.0000 / `strand_flip_resolutions` 106
   / palindromic 31. A wrong number here = STOP (bad start, or the sweep didn't
   run).
3. `genome annotate canonicalize-variants`  *(transiently empties `consensus_genotypes`)*
4. `genome merge`                            *(transiently empties `variant_annotations_index`)*
5. `genome annotate align-tier3-consensus`
6. `genome annotate refresh-index`
7. **Checkpoint B** (after the full sequence) — re-capture both blocks and compare
   against finding-020's bedrock anchor table.

Capture — merge anchors (`consensus_genotypes`; run at A and at B):

```sql
SELECT
  COUNT(*) FILTER (WHERE NOT is_imputed)                                              AS chip_consensus_rows,
  COUNT(*) FILTER (WHERE is_imputed)                                                  AS imputed_only_rows,
  COUNT(*) FILTER (WHERE NOT is_imputed AND consensus_method='both_concordant')       AS both_concordant,
  COUNT(*) FILTER (WHERE NOT is_imputed AND consensus_method='single_source')         AS single_source,
  COUNT(*) FILTER (WHERE NOT is_imputed AND consensus_method='disagreement_resolved') AS disagreement_resolved
FROM consensus_genotypes;
```

`concordance_rate`, `strand_flip_resolutions`, `genotype_mismatch`, and the
palindromic-shared count are merge-computed — read them from the `merge.complete`
structlog event each `genome merge` emits (at A, the prior run's locked
1.0000 / 106 / … / 31). `survivors_enriched` and `rsid_conflicts` come from the
`canonicalize.complete` event.

Capture — index match anchors (`variant_annotations_index`, after `refresh-index`):

```sql
SELECT
  COUNT(*)                                       AS row_count,
  COUNT(*) FILTER (WHERE af_global IS NOT NULL)  AS gnomad_matches,
  COUNT(*) FILTER (WHERE clinvar_count > 0)      AS clinvar_matches,
  COUNT(*) FILTER (WHERE gwas_trait_count > 0)   AS gwas_matches,
  COUNT(*) FILTER (WHERE has_pgx)                AS pharmgkb_matches,
  COUNT(*) FILTER (WHERE is_rare)                AS is_rare,
  COUNT(*) FILTER (WHERE is_ultrarare)           AS is_ultrarare
FROM variant_annotations_index;
```

Tripwires: concordance should drop (bounded by finding-020's locked rate, for the
documented reason); imputed-only (2,267,751) must NOT move; the recovery line —
`gwas_matches`→66,726, `pharmgkb_matches`→1,737, `rsid_conflicts`→0, rsID
invariant 0-lost — is the coordination tripwire. If the recovery numbers diverge
from those targets, that is a SEMANTIC escalation (the swept-data interaction may
have made part of the coalescing redundant) — STOP, leave the finding-020 /
CLAUDE.md placeholder markers unfilled, and route to the planning chat.

**Pre-squash placeholder check (must pass before the squash-merge).** The gate
numbers backfill the placeholder markers planted across `finding-020` and
`CLAUDE.md` (each written as the literal word `GATE` joined by a hyphen to
`FILL`). Once the step-7 review validates the gate and the markers are filled,
confirm none survive — repo-wide, no path filter:

```
git grep -nE 'GATE[-]FILL'
# → must print nothing across the whole tree. Any hit = an un-gated number is
#   about to ship into a durable doc. STOP; fill or remove it before squashing.
```

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

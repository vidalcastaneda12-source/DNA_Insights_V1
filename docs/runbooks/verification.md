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

`uv run genome docs check` is the decision-tracking gate (the repo-root
`MEMORY.md` decision ledger + per-finding frontmatter — CAPTURE / RETRIEVAL /
LIFECYCLE; finding-036). It is **DB-free and config-free** — it runs on a fresh
checkout with no SQLCipher built and needs no `APP_DB_PASSPHRASE` — and must exit
0. It now runs **automatically**: as a step in `scripts/verify.sh`, at the
pre-commit boundary where the tracked hook is installed
(`./scripts/install-hooks.sh`), and as the `docs-check` GitHub Action on every PR.
To make a CI failure block merge, a repo admin adds the `docs-check` job as a
required status check on `main` (Settings → Branches → branch protection); until
then the Action is advisory. The local hook is bypassable with
`git commit --no-verify`, so the verify.sh run and the CI gate are authoritative.

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
  high-quality (DR² > 0.8) = 1,592,735, chrX imputed = 0 for males
  (**Phase-4 boundary value; superseded by PR #74 / 5a — see the
  post-chrX re-lock below**), full-genome runtime ~30 min on 16
  threads / 8 GB heap.
* PR-3 canonicalize step (`genome annotate canonicalize-variants` →
  `genome merge` → `genome annotate align-tier3-consensus` →
  `genome annotate refresh-index`): the bedrock anchor table in
  finding-020 lists every post-PR-3 locked number. Headline checks
  (gate-measured): `gnomad_matches` 2,796,952 / `clinvar_matches`
  61,458 (up from 101,501 / 2,559); `pharmgkb_matches` 1,737 holds;
  `gwas_matches` 66,701 — **not** unchanged, −23 from collapse-dedup
  (finding-020 recon C). The post-`align-tier3` `consensus_genotypes
  WHERE consensus_method='disagreement_resolved'` count is **1** (2
  post-merge; `align-tier3` deletes the non-canonical side of its 1
  examined pair → 1 final). (This bullet previously predicted a
  mid-double-digit figure; that estimate was stale — it assumed the
  tier-3 strand-flip pairs survive to merge,
  but canonicalize subsumes them upstream, so 104 of the 106 post-merge
  rows reclassify to `single_source`. See finding-020 recon B.)
* Post-chrX re-lock (PR #74 / 5a — M3-physical chrX; gate-measured,
  run_0002): chrX imputed is now **92,832** (non-PAR **90,999** + PAR
  **1,833**), not 0; `consensus_total` = `variants_master` **3,160,364**;
  `imputed_only` **2,218,539**; `single_source` **821,285**;
  `both_concordant` **120,513**; `disagreement_resolved` **0**;
  `unresolvable` **27**; shared-call concordance **0.9997760079641613**
  (UNCHANGED by chrX). Autosomal negative control byte-identical to
  pre-chrX: both **115,509** / single **793,917** / imputed_only
  **2,146,302** / unresolvable **26**. Male non-PAR quality is
  dosage-confidence + 5-fold LOO (concordance **0.985550**, run_0002 —
  Beagle is non-deterministic, band ≈0.985–0.986; PASS bar ≥95%), not
  DR² (structurally dead for single-sample male non-PAR). The
  imputation-derived counts (chrX yield, dconf split, LOO) are
  tolerance-banded; the consensus counts are exact. See CLAUDE.md obs #3
  and findings 029/031/033. gnomAD/index match counts were re-locked in
  PR C (gate-run 2026-06-22) — see the post-chrX `user_only` boundary block
  below + CLAUDE.md obs #4.

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

**PR 4 (tier-2 rsID matching, finding-025) re-locks the rsid-keyed anchors only.**
`gwas_matches` **66,701 → 66,764** (+63) and `pharmgkb_matches` **1,737 → 1,738**
(+1); `row_count` **2,824,229 → 2,824,236** (+7). The coord-keyed anchors
(`gnomad_matches` 2,796,952 / `clinvar_matches` 61,458 / `is_rare` 163,160 /
`is_ultrarare` 103,261) **must not move** — rsID merges don't touch a coordinate
join, so any movement is a STOP. Pre-run, two integrity checks on the active
dbSNP alias map must both be 0 (single-hop terminal-survivor; one survivor per
alias), and the exact per-leg delta is pre-derivable as canonical-match-set −
raw-match-set at variant grain (a tens-of-thousands rise = over-collapse = STOP).
`refresh-index` ~120 s here is commit-dominated (2.8M-row rebuild), not a PR-4
regression.

**Post-chrX `user_only` re-lock (PR C, gate-run 2026-06-22).** The anchors above
are the **pre-chrX three-way** PR-4 boundary (re-running the canonicalize gate on
that corpus reproduces them). PR C — the post-chrX `user_only` gnomAD reload
(`refresh --source gnomad --force --jobs 8`) + `refresh-index` — establishes a
**new** boundary: `gnomad_matches` **3,054,426** / `row_count` **3,077,001** /
`clinvar_matches` **61,926** / `gwas_matches` **66,742** / `pharmgkb_matches`
**1,737** / `is_rare` **173,689** / `is_ultrarare` **109,013**. The
`gnomad_matches` rise (+71,995 over the pre-reload `user_only` 2,982,431) is
**entirely chrX** (chrX index matches 22,640 → 94,635; chrX `gnomad_frequencies`
36,867 → 138,299); the autosomal coord/rsid-keyed legs are unchanged by the
reload, and the merge-anchor negative control is byte-identical. See CLAUDE.md
obs #4 + annotations.md §5.5. Query chrX as `chrom = 'X'` (bare enum), not
`'chrX'`.

Tripwires (gate-measured and reconciled — these are now the *expected* values, not
open escalations): concordance drops to **0.999776**, driven entirely by 27
palindromic `strand_ambiguous` no-calls with `genotype_mismatch`=0 (finding-020
recon A — which VSC-User ran and **confirmed correct unification**, 27 distinct
palindromic sites). The
recovery line is `gwas_matches`→**66,701** (−23 from collapse-dedup, finding-020
recon C — **not** the pre-gate 66,726), `pharmgkb_matches`→1,737 (holds),
`rsid_conflicts`→**1** (one genuine real-rs#-vs-real-rs# collision survives the #66
sweep, finding-021 amendment — **not** 0), rsID invariant 0-lost (held). Imputed-only
**moved** to **2,146,324** (Δ −121,427 == `survivors_enriched`, population C — it is
**not** the negative anchor an earlier draft assumed). `palindromic shared` holds
at **31** (the het, both-alleles-observed definition); the post-canon *site-level*
palindromic count (6,681, incl. hom-only-recovery reveals) is **not** the anchor —
see finding-023. These divergences from the
pre-gate predictions *were* the SEMANTIC escalation, and the step-7 review already
resolved them in finding-020 / finding-021; a re-run that reproduces them is
correct. A re-run that *re-diverges* from these reconciled values is the new STOP
signal — route to the planning chat.

**Pre-squash placeholder check (must pass before the squash-merge).** The gate
numbers backfill the placeholder markers planted across `finding-020` and
`CLAUDE.md` (each written as the literal word `GATE` joined by a hyphen to
`FILL`). The full set is **18** markers (the prior "16" undercounted — it predates
the `survivors_enriched` / `rsid_conflicts` tokens at finding-020:96-97). The
gate re-lock filled all **18** across three passes: the step-7 backfill; a
recon-results pass that locked concordance / `both_concordant` / `single_source` /
chip-consensus once VSC-User ran recons A/B/C (A confirmed correct-unification; B
confirmed the reorient-movers + post-align `disagreement_resolved`=1; C confirmed
`gwas_matches` −23); and a final fill pass for the last 3 markers
(`palindromic shared` held at 31 — see finding-023; `is_rare` 163,160 /
`is_ultrarare` 103,261; `chip+imputed overlap` 222,847). The check is now clean:

```
git grep -nE 'GATE[-]FILL' -- CLAUDE.md ROADMAP.md 'docs/findings/' 'docs/runbooks/' ':!docs/findings/finding-034*'
# → prints nothing. All 18 gate numbers are locked; the PR is no longer
#   placeholder-gated. Any hit = an un-gated number about to ship into a durable
#   post-gate doc — STOP and fill or remove it before squashing.
#
#   Positive allowlist of the durable post-gate ledgers only. docs/plans/ is out by
#   design (an approved plan legitimately carries pre-gate placeholders until its own
#   implementation gate), as are the agent-team tooling (.claude/), the CHANGELOG, and
#   finding-034 — which *describe* the placeholder-marker mechanism, not carry a marker.
```

### PR 6 genes seed gate (minimal `genes` seed / finding-020 amendment)

PR 6 seeds the previously-empty `genes` table via `genome annotate seed-genes`
(set-union of the ACMG SF v3.3 panel + active CPIC/PharmGKB symbols, under a fresh
`hgnc` `annotation_source_versions` row, **no `annotation_sources` pointer flip**).
It is a static backfill, so every number is **exact / deterministic** — any drift
against the same active CPIC/PharmGKB versions is a regression, not run-to-run noise.
Run against the live corpus and compare against the locked answer (CLAUDE.md
"Real-data observations" #7). The gate-confirmed boundary (Human Gate 2, 2026-06-23,
active versions `clinvar 2026_06_15, gwas_catalog 2026_06_01, gnomad 4.1.1,
pharmgkb 2025_07_05, dbsnp 157`):

**1. Run + summary line (first run).**

```
genome annotate seed-genes
# → genes seeded: source_version_id=11 already_populated=False genes_rows=1153 \
#     acmg_sf_genes=84 pgx_genes=1086 cpic_covered=True pharmgkb_covered=True
```

`source_version_id` = **11** (the live `MAX(source_version_id)` was 10 pre-seed → 11);
version label `acmg_sf_v3.3+pgx_derived`; `record_count` = 1153.

**2. Composition + provenance + coverage** (`genome.duckdb`):

```sql
SELECT
  COUNT(*)                                          AS genes_rows,       -- 1153
  COUNT(*) FILTER (WHERE is_acmg_sf)                AS is_acmg_sf,       -- 84
  COUNT(*) FILTER (WHERE is_pgx_relevant)           AS is_pgx_relevant,  -- 1086
  COUNT(*) FILTER (WHERE source_version_id IS NULL) AS null_svid,        -- 0
  COUNT(*) FILTER (WHERE retrieval_date IS NULL)    AS null_retrieval,   -- 0
  COUNT(DISTINCT source_version_id)                 AS distinct_svid     -- 1 (={11})
FROM genes;
```

`genes`=1153 = |84 ACMG ∪ 1086 PGx| (ACMG ∩ pgx overlap = **17**; pgx-union 1086 =
cpic-distinct-current 19 ∪ pharmgkb-distinct-NOT-NULL-current 1086). Coverage gate:
the cpic and pharmgkb EXCEPT-probes (active-source symbols NOT IN `genes`) must each
return **0** → `cpic_covered=True`, `pharmgkb_covered=True`.

**3. Idempotence** — a second `genome annotate seed-genes` returns
`already_populated=True` with identical counts, and `annotation_source_versions` still
holds **exactly one** `hgnc` row (no second version row, no re-insert).

**4. Keystone FK probe** — a `derived_acmg_sf_findings` insert with a **seeded**
`gene_symbol` SUCCEEDS; the same insert with an **unseeded** `gene_symbol` RAISES the
`genes` FK; roll back clean. (Proves the FK gate the seed exists to clear is actually
satisfied.)

**5. Negative control — byte-unchanged** (the seed touches only `genes` + the one new
`annotation_source_versions` row; it does **not** run `refresh-index`):

```sql
SELECT COUNT(*) FROM variants_master;                                   -- 3,160,364
SELECT COUNT(*) FROM annotation_sources;                               -- 7 (NO hgnc pointer)
-- gnomad pointer still source_version_id = 10; obs #4 index counts UNCHANGED:
SELECT
  COUNT(*)                                       AS row_count,          -- 3,077,001
  COUNT(*) FILTER (WHERE af_global IS NOT NULL)  AS gnomad_matches,     -- 3,054,426
  COUNT(*) FILTER (WHERE clinvar_count > 0)      AS clinvar_matches,    -- 61,926
  COUNT(*) FILTER (WHERE gwas_trait_count > 0)   AS gwas_matches,       -- 66,742
  COUNT(*) FILTER (WHERE has_pgx)                AS pharmgkb_matches,    -- 1,737
  COUNT(*) FILTER (WHERE is_rare)                AS is_rare,            -- 173,689
  COUNT(*) FILTER (WHERE is_ultrarare)           AS is_ultrarare       -- 109,013
FROM variant_annotations_index;
```

Any movement in block 5 means the seed did more than seed `genes` — STOP. See
CLAUDE.md obs #7, ROADMAP "Pre-Phase-6 sequence" PR 6, and
[`finding-020`](../findings/finding-020-canonical-refalt-backfill.md) "Out of scope"
amendment.

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

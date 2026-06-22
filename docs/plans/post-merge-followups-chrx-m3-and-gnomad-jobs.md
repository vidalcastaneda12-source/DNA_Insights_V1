# Post-merge follow-ups — PR #74 (chrX M3) + PR #75 (gnomAD `--jobs`)

> **Status update (2026-06-22):** PR A merged (#82 — Item 1 SAFE locks + Item 5
> notes) and PR B merged (#83 — **Item 2 RESOLVED: VSC-User ruled `user_only`;
> the `_build_filter_set` strategy swap + doc reconciliation shipped**). **Item 3
> (three-way `--jobs` exact-count check) is now MOOT** — three-way was not kept.
> **Remaining live work: Item 4** (the post-chrX gnomAD reload = "PR C", which
> produces the authoritative `user_only` gnomAD/index numbers and closes CLAUDE.md
> obs #4). The gating decision below is answered — do not re-ask it.

**Purpose:** the five remaining follow-up items after PR #74 (chrX imputation via
M3-physical region split + dosage-confidence QC) and PR #75 (gnomAD parallel import
via `--jobs`) were squash-merged to `main` on 2026-06-19.

**How to use:** hand this file to a fresh Claude Code session —
"read `docs/plans/post-merge-followups-chrx-m3-and-gnomad-jobs.md` and execute the
plan." It is self-contained; a cold session can act on it.

**State when written:** `main` carries both PRs (squash commits `10aac2f` / `83a6182`)
and is green (ruff/format/mypy clean, pytest 1070 passed). The live
`data/genome.duckdb` is the validation-gate build = both PRs **plus an uncommitted
`user_only` gnomAD narrowing** — see the caveat below.

---

You are picking up post-merge cleanup for the DNA-insights project (a local-first
personal-genomics app). Before starting, read: `CLAUDE.md`, `ROADMAP.md`
(Pre-Phase-6 → PR 5/5a), and findings 019, 020, 029, 031, 033, 035. **Start in plan
mode** per CLAUDE.md: produce a plan covering all five follow-ups below with their
dependencies, surface the one decision that gates the rest, and get VSC-User approval
before implementing. Branch from `main`.

## Critical context: the live DB is a `user_only` build

The validation gate that produced the "clean" numbers was a from-scratch rebuild that
ran both PRs **plus an UNCOMMITTED `user_only` gnomAD filter narrowing** (finding-035)
that is in **neither** merged PR — `main` ships the **three-way** gnomAD filter
(finding-011). So `data/genome.duckdb` right now = `user_only` gnomAD + both PRs.
Therefore:

- chrX numbers and canonicalize/merge **count anchors** are filter-independent → safe
  to read from the live DB and lock.
- **gnomAD-derived numbers** (`gnomad_matches`, the index `row_count`) are
  `user_only`-specific → do **not** lock them as the three-way baseline.

## The gating decision (Item 2) — resolve with VSC-User first

finding-035's audit found every reader of `gnomad_frequencies` INNER-JOINs to
`variants_master`, so the ClinVar/GWAS-only rows (~76%) are loaded but never read;
`user_only` cuts the gnomAD load ~4–5× with no consumed-data loss, but it reverses
finding-011 and touches CLAUDE.md "Things never to do" #3. The gate showed VSC-User
leaning "adopt," but the code shipped three-way. The answer determines: (a) whether to
commit the one-line `_build_filter_set` strategy swap + re-lock the runbook; (b) whether
Item 3 is even relevant; (c) what gnomAD numbers Item 1 locks and how Item 4 is run.
**Ask this before implementing any gated part.**

## The five items

### 1. Lock the clean-rebuild numbers (findings 029/031/033 + CLAUDE.md obs #3/#4/#6)

finding-029 still says "capture at the gate"; CLAUDE.md obs #3 still says "chrX imputed
variants: 0" — both need the real M3 numbers.

- **SAFE NOW (filter-independent):** chrX M3 anchors + canonicalize/merge count anchors.
- Mark imputation-derived numbers (non-PAR yield, LOO concordance) as
  **tolerance-banded, not exact** — Beagle is multi-threaded / not bit-reproducible
  (the gate saw yield −86 and LOO 0.9856 vs 0.9860 run-to-run).
- **DEFER (gated on Item 2):** `gnomad_matches` + index `row_count`. ClinVar/GWAS/PharmGKB
  match counts *are* filter-independent and may be locked now.

Reference values — **re-derive before writing** (the M1 episode showed handoffs can
mislead):

- _[verified from DB/artifact]_
  - chrX non-PAR imputed yield **90,999**; male non-PAR het anomaly **1**;
    consensus **3,160,364** / imputed_only **2,218,539**
  - LOO (`archive/imputation/run_0002/loo/REPORT.json`): **0.985550** @ dconf 0.9,
    6957/7059 concordant, 5276 not-in-panel; production dconf split 87,578 ≥0.99 /
    3,421 in [0.9, 0.99)
  - negative control (autosomes byte-identical): chr1–22 both **115,509** /
    single **793,917** / imputed_only **2,146,302** / unresolvable **26**
- _[per validation handoff — CONFIRM against DB]_
  - canonicalize: reoriented 101,948 / hom-recovered 722,154 / collapsed 121,454 /
    survivors_enriched 121,427; variants_master 3,088,917 → 3,088,233 (−684)
  - merge: consensus 3,088,233 / single 821,391 / both 120,513 / disagreement 0 /
    unresolvable 27; shared-call concordance 0.9997760079641613
  - refresh-aliases (value-level micro-drift): rows 839,380, user-old-rsid-hits 1,191
- _[USER_ONLY — do NOT lock as three-way]_
  - gnomad_matches 2,982,431; index row_count 3,005,358; gnomAD load 4,467,370

### 2. `user_only` gnomAD filter decision (finding-035) — the gating decision above

If approved: change `strategy="three_way"` → `"user_only"` in
`gnomad._build_filter_set`, re-lock `docs/runbooks/annotations.md` filter-set
composition + `rows_loaded`, and reconcile CLAUDE.md "Things never to do" #3 /
finding-011. Schema-free; its own small PR.

### 3. Three-way `--jobs 8` exact-count check (only if keeping three-way)

The parallel mechanism is proven, but the exact three-way row count (**7,275,664**) was
never reproduced via `--jobs` (the gate ran `user_only`). Provide VSC-User the command
`genome annotate refresh --source gnomad --jobs 8` (needs `external_calls_enabled=true`,
~2–3 h) plus the exact capture list (`rows_loaded`, per-chrom counts, `match_rate`
0.988, AF buckets, per-pop presence) to confirm it reproduces the locked identifiers.
Long real-data op — VSC-User executes; you prepare + verify.

### 4. chrX gnomAD annotation gap

gnomAD was loaded **before** the chrX M3 import, so the 72,237 new chrX imputed
positions mostly lack gnomAD annotations (+135 only). Close it with a post-chrX gnomAD
reload (`genome annotate refresh --source gnomad --force --jobs 8` at the chosen filter)
then `genome annotate refresh-index`. **This run produces the authoritative gnomAD/index
numbers** → feeds the deferred part of Item 1 and closes obs #4. Long real-data op
(`user_only` ~1.5–3 h, three-way longer); needs `external_calls_enabled`. Prepare
commands + capture list; VSC-User executes; then lock the resulting numbers.

### 5. Minor operational docs (docs-only)

Record finding-030's `prepare-chrx` ~80-min cost (`count_haploid_gts` is
O(variants × samples)) as a runbook expectation; add a note to set `TMPDIR` on the big
disk for the parallel gnomAD staging (`_stream_chromosome_to_parquet` uses
`tempfile.mkdtemp`).

## Suggested sequencing

- **PR A** (docs-only, no rebuild, do first): Item 1 SAFE locks (chrX + canon/merge,
  tolerance bands) + Item 5 notes.
- **Decision:** Item 2 (gates the rest).
- **PR B** (if `user_only` approved): strategy swap + runbook re-lock.
- **Items 3/4:** VSC-User runs them; then a short follow-up doc-lock of the gnomAD/index
  numbers (deferred Item 1) + close obs #4.

## Sources of truth (re-derive; don't trust pasted numbers)

- LOO: `archive/imputation/run_0002/loo/REPORT.json`
- DB read-only: `duckdb.connect(<genome.duckdb>, read_only=True)`.
  - chrX imputed by region = `genotype_calls` (source `'beagle_imputed'`) ⋈
    `variants_master` with the PAR predicate
    (`pos_grch38 BETWEEN 10001 AND 2781479 OR BETWEEN 155701383 AND 156030895`)
  - het anomaly = `SELECT COUNT(*) FROM consensus_chrx_dosage_v WHERE male_nonpar_het_anomaly`
  - non-PAR dconf = `genotype_calls.imputation_r2` where
    `list_contains(quality_flags, 'nonpar_dosage_conf')` — note `quality_flags` is `VARCHAR[]`
- Findings 019 (aliases), 020 (canonicalize), 029/031/033 (chrX), 035 (filter audit).

## Process

Plan-mode first; ask the Item-2 decision before any gated work; Items 3/4 need
`external_calls_enabled=true` and hours, so prepare exact commands + capture lists
rather than assuming you can run them; keep the dev-loop green
(`ruff` / `ruff format` / `mypy --strict backend/src` / `pytest`); `/handoff` at the end.

---

## PR C run sheet — chrX gnomAD gap reload + final number-lock (Item 4 + deferred Item 1)

> **Status (2026-06-22):** Phase 1 (this artifact — exact commands + capture list)
> ready on branch `pr-c-chrx-gnomad-gap-reload`. **Item 3 is MOOT** — PR B kept
> `user_only`, so the three-way `--jobs` exact-count reproduction is not run.
> Phase 2 (re-derive + number-lock) is gated on VSC-User executing the reload
> below and handing back the captured output. Per CLAUDE.md ("Things never to do"
> #3 / obs #4) **no gnomAD number is locked until this runs** — every locked value
> is re-derived read-only from the post-reload `data/genome.duckdb`, cross-checked
> against the load summary, never copied from the pre-reload `user_only` gate
> figures (`gnomad_matches` ~2,982,431 / index `row_count` ~3,005,358 / load
> ~4,467,370 — lower-bound sanity only).

**Why.** gnomAD was loaded *before* the chrX M3 import, so the **72,237** chrX
imputed-only positions now in `variants_master` (finding-029) are absent from the
`user_only` filter the live load was built from — only **~135** carry a gnomAD AF.
This reload rebuilds the `user_only` filter (now chrX-inclusive), re-streams gnomAD,
and rebuilds the index, producing the authoritative post-chrX numbers.

**Preconditions.** Run against the current live `data/genome.duckdb` (the
validation-gate build = PR #74 chrX M3 + PR #75 `--jobs` + the merged PR-B
`user_only` swap). Multi-hour remote-streaming op (`user_only` ~1.5–3 h at
`--jobs 8`); `refresh-index` ~120 s. The reload runs no `merge`/`canonicalize`, so
`consensus_genotypes` + `variants_master` stay untouched (the negative control).

### Step 0 — pre-reload baseline (read-only; proves the before→after delta)

Read-only connection: `duckdb -readonly data/genome.duckdb` (or
`duckdb.connect("data/genome.duckdb", read_only=True)`). Save the output.

```sql
-- (A) gnomAD chrX coverage — the gap being closed (expect small now)
SELECT COUNT(*) AS gnomad_chrx_rows_active
FROM gnomad_frequencies gn
JOIN annotation_sources s
  ON s.source_db = 'gnomad' AND s.current_source_version_id = gn.source_version_id
WHERE gn.chrom = 'chrX';

SELECT COUNT(*) AS index_chrx_gnomad_matches
FROM variant_annotations_index vai
JOIN variants_master vm ON vm.variant_id = vai.variant_id
WHERE vm.chrom = 'chrX' AND vai.af_global IS NOT NULL;

-- (B) index match anchors (== verification.md "index match anchors" block;
--     these are what Phase 2 LOCKS, re-derived from the DB)
SELECT
  COUNT(*)                                       AS row_count,
  COUNT(*) FILTER (WHERE af_global IS NOT NULL)  AS gnomad_matches,
  COUNT(*) FILTER (WHERE clinvar_count > 0)      AS clinvar_matches,
  COUNT(*) FILTER (WHERE gwas_trait_count > 0)   AS gwas_matches,
  COUNT(*) FILTER (WHERE has_pgx)                AS pharmgkb_matches,
  COUNT(*) FILTER (WHERE is_rare)                AS is_rare,
  COUNT(*) FILTER (WHERE is_ultrarare)           AS is_ultrarare
FROM variant_annotations_index;

-- (C) NEGATIVE CONTROL — must be byte-identical after the reload (PR C touches
--     only gnomad_frequencies + variant_annotations_index)
SELECT COUNT(*) AS variants_master_total FROM variants_master;   -- expect 3,160,364

SELECT
  COUNT(*) FILTER (WHERE NOT is_imputed)                                              AS chip_consensus_rows,
  COUNT(*) FILTER (WHERE is_imputed)                                                  AS imputed_only,          -- 2,218,539
  COUNT(*) FILTER (WHERE NOT is_imputed AND consensus_method='both_concordant')       AS both_concordant,       -- 120,513
  COUNT(*) FILTER (WHERE NOT is_imputed AND consensus_method='single_source')         AS single_source,         -- 821,285
  COUNT(*) FILTER (WHERE NOT is_imputed AND consensus_method='disagreement_resolved') AS disagreement_resolved, -- 0
  COUNT(*) FILTER (WHERE consensus_method='unresolvable')                             AS unresolvable           -- 27
FROM consensus_genotypes;
```

### Step 1 — gnomAD reload at user_only (now picks up chrX) — VSC-User runs

```bash
export TMPDIR=/path/on/big/disk          # NOT /tmp — --jobs 8 staged Parquet overflows a size-capped tmpfs (runbook §5.5)
genome config set external_calls_enabled true
genome annotate refresh --source gnomad --force --jobs 8
```

Capture from the `gnomad.refresh.complete` structlog line: `rows_loaded` ·
`filter_set_composition` (`{user, union_total}`, equal under `user_only` —
= distinct post-chrX `variants_master` positions) · `distinct_variants_per_chrom`
(watch **chrX**) · `match_rate` · `af_buckets_user_overlap` · `mean_af_user_overlap` ·
`pop_af_presence` · `chromosomes_succeeded` / `chromosomes_failed` · wall-clock ·
plus the aggregate `gnomad.chrom.htslib_recover` reopen count.

### Step 2 — rebuild the rollup — VSC-User runs

```bash
genome annotate refresh-index
genome config set external_calls_enabled false   # optional: restore fail-closed
```

Capture from `index_refresh.complete`: `row_count`, `gnomad_matches`,
`clinvar_matches`, `gwas_matches`, `pharmgkb_matches`, `curated_count`,
`elapsed_ms`.

### Step 3 — post-reload capture (read-only) — re-run Step 0 (A)/(B)/(C)

The (A)/(B) values are the authoritative lock source. (C) is the tripwire.

### Pass criteria

| Check | Expectation |
|---|---|
| `chromosomes_succeeded` / `failed` | **23 / 0** (autosomes 1-22 + X) |
| `match_rate` | ≈ **0.988** (investigate only if `< 0.95`; a dip from chrX imputed positions gnomAD lacks is corpus-expected, not a regression — runbook §5.5) |
| chrX gnomAD coverage (A) | **rises** materially (~135 → N) — the gap closed |
| index `gnomad_matches` / `row_count` (B) | **rise** vs the pre-reload ~2,982,431 / ~3,005,358 start (lower-bound sanity; lock the actual re-derived values, never these) |
| negative control (C) | **byte-identical** before vs after: `variants_master` 3,160,364 · `imputed_only` 2,218,539 · `both_concordant` 120,513 · `single_source` 821,285 · `disagreement_resolved` 0 · `unresolvable` 27. **Any drift = STOP** — the reload must not reach consensus/variants_master. |

### Hand back to VSC-Claude for Phase 2 (the lock)

Provide the Step-1/Step-2 structlog lines verbatim + the Step-0/Step-3 SQL output.
Phase 2 re-derives read-only and locks, in one follow-up commit on this branch:

| Doc | Lock |
|---|---|
| `CLAUDE.md` obs #4 | post-chrX `user_only` index block (`row_count`, `gnomad`/`clinvar`/`gwas`/`pharmgkb_matches`, `curated_count`, `is_rare`, `is_ultrarare`); supersede the post-PR-4 three-way block |
| `CLAUDE.md` obs #3 | replace "Index match counts … **DEFERRED to PR C** …" with "locked in PR C — see obs #4" |
| runbook §5.5 (gnomAD) | full locked `user_only` drift table (`rows_loaded`, `match_rate`, `mean_af_user_overlap`, `filter_set_composition`, per-chrom, AF buckets, per-pop, `--jobs 8` wall-clock); keep the three-way table as the superseded/revert baseline |
| runbook §5.7 (index) | re-point the "capture and re-lock then" forward-note at obs #4; keep the finding-018 first-run table as history |
| `verification.md` | the "index match anchors" block lists the pre-chrX PR-4 values as "must not move" — add the post-chrX `user_only` re-lock so the gate doc isn't stale |
| finding-029 | replace "Index anchors — DEFERRED to PR C" (≈ L214-220, L324) with the locked values |
| finding-035 | flip status (L3, L102-106): PR C completed, authoritative `user_only` numbers locked |
| this plan doc | mark Item 4 done / PR C complete |
| `CHANGELOG.md` | finalize the PR C entry with the locked numbers |

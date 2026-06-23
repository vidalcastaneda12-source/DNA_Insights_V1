# PR 6 — Minimal `genes` seed (Option A) — approved plan

**Status:** Plan approved at Human Gate 1. Ready for implementation (ClaudeCodeDevelopment).
**Scope slot:** ROADMAP.md → Pre-Phase-6 sequence → **PR 6**. Phase-6 entry is gated on this PR.
**Risk tier:** 1 (data-backfill; `C=2 B=1 P=0 → S=3`; no schema/DDL change, no anchor exposure).
**Provenance:** produced by the per-scope agent team Plan phase (`plan-phase.js`, finding-034) — dispatcher → 2 planners (minimal-diff, gate-backward) → combined judge → synthesizer → pre-mortem → auditor. Verdict `ready`, pre-mortem `proceed`. The four open decisions were resolved by VSC-User (below); Escalation A (the ACMG panel) was supplied and **verified against the official ACMG SF v3.3 supplementary** (see Dataset).

---

## Summary

`genes` is empty (`SELECT COUNT(*) FROM genes = 0`). Four Phase-6 derived tables carry
`NOT NULL ... REFERENCES genes(gene_symbol)`:
`derived_pgx_phenotypes` (ddl/group_3_derived.sql:117), `derived_carrier_findings` (:160),
`derived_acmg_sf_findings` (:195), `derived_compound_het` (:430). With `genes` empty, every
Phase-6 insert into those tables fails the FK. PR 6 seeds the **FK-satisfying gene-symbol
subset only**: the union of the ACMG SF v3.3 panel and the in-DB PGx/carrier gene lists.
Full `genes`/`traits`/`pathways` dictionaries remain deferred to Phase 7.

Deliverable: a new standalone module `backend/src/genome/annotate/seed_genes.py` + CLI
subcommand `genome annotate seed-genes`. **Zero schema/DDL edits.**

---

## Locked decisions (VSC-User, resolved at the gate)

1. **PGx/carrier symbol source** → **derive in-code** at seed time:
   `SELECT DISTINCT gene_symbol FROM cpic_guidelines` (current-version-scoped) `UNION`
   `SELECT DISTINCT gene_symbol FROM pharmgkb_annotations` (current-version-scoped,
   `gene_symbol IS NOT NULL`). Verbatim casing — guarantees the seed matches what Phase-6
   PharmCAT/carrier actually read. **Not** hand-curated.
2. **Provenance (decision #8)** → **allocate one real `annotation_source_versions` row**
   under `source_db='hgnc'` (already in `KNOWN_SOURCE_DBS`), with a synthetic version label;
   every `genes` row sets `source_version_id` = that new svid. (Live `MAX(source_version_id)=10`,
   so the new svid is **11**.) NOT `source_version_id=NULL`.
3. **Transaction ordering** → **clinvar-exact**: `insert_source_version(...)` in AUTOCOMMIT
   *before* `conn.begin()`; bulk `INSERT` inside the `begin()` block; on failure
   `conn.rollback()` **then** a best-effort `_cleanup_orphan_version_row` DELETE; re-raise.
   Byte-matches `clinvar.py:736/738`, already exercised by the loader suite, and
   finding-015-safe (rollback discards partial `genes` first, so the cleanup DELETE has no FK
   children). The "begin-first single transaction" alternative was **not** adopted.
4. **finding-020 stale note** → **amend in this PR** (fold the one-line amendment of
   finding-020's stale "genes seed → Phase 7" text + the CHANGELOG entry into the
   implementation §7). Not deferred to a separate doc PR.

---

## Dataset — the ACMG SF v3.3 panel (verified)

The hand-curated half of the seed is the **ACMG SF v3.3** secondary-findings panel —
**the latest version** (released 2025-07-09; effective 2026-01-12). It is **84 distinct
genes** = v3.2's 81 + the three v3.3 additions (**ABCD1, CYP27A1, PLN**); no genes removed.

- **File:** [`pr-6-acmg-sf-v3.3-genes.csv`](pr-6-acmg-sf-v3.3-genes.csv) — 84 rows,
  columns `gene_symbol, acmg_sf_disease, acmg_sf_inheritance, acmg_sf_version`.
- **Source of truth:** the official ACMG SF v3.3 supplementary spreadsheet
  (`mmc1.xlsx`, doi:10.1016/j.gim.2025.101454). Gene symbol, inheritance, and
  per-gene `SF List Version` were taken **verbatim** from that file; only the umbrella
  disease label is editorial (multi-phenotype genes collapsed to one label, since
  `genes.gene_symbol` is the PK). Generation was validated: the editorial set and the
  xlsx gene set match exactly, and the per-gene version distribution reconciles to the
  published version totals (59 → 73 → 78 → 81 → 84).
- **`acmg_sf_version` is per-gene** (the version each gene was *added* in), not a uniform
  list version: `v1.0`×57, `v2.0`×2 (ATP7B, OTC), `v3.0`×14, `v3.1`×5 (BAG3, DES, RBM20,
  TNNC1, TTR), `v3.2`×3 (CALM1/2/3), `v3.3`×3 (ABCD1, CYP27A1, PLN).
- Inheritance: 72 AD / 9 AR / 3 XL. All `is_acmg_sf=TRUE`.

The implementer bakes this CSV into `seed_genes.py` as the static ACMG constant (a typed,
frozen structure). The `genes` table has **no** "variants to report" column — that ACMG
reporting nuance (e.g. TTN truncating-only, HFE C282Y-homozygotes, AR 2-variant rule) is
**out of scope** for this seed and belongs to the Phase-6 ACMG detector.

---

## Implementation plan (8 sections)

### 1. Reading list confirmed
CLAUDE.md, ROADMAP.md, `docs/schemas/schema_group_2_reference_annotations.md`,
`docs/schemas/schema_group_3_derived_analyses.md`, `ddl/group_2_annotations.sql`,
`ddl/group_3_derived.sql`; findings 005, 017, 019, 020; `backend/src/genome/annotate/`
(`cli.py`, `source_versions.py`, `supersession.py`, `registry.py`, `__init__.py`,
`loaders/variant_aliases.py`, `loaders/clinvar.py`, `loaders/pharmgkb.py`);
`backend/tests/test_annotate_cli.py`, `test_init_schema.py`, `test_loaders_variant_aliases.py`.
Live DB re-verified read-only (2026-06-22/23): `genes=0`, `MAX(source_version_id)=10`,
no `hgnc` row in `annotation_sources` (total=7), `cpic_guidelines` distinct gene_symbol=19,
`pharmgkb_annotations` distinct gene_symbol (NOT NULL)=1086, `variants_master=3,160,364`.

### 2. Problem statement
See Summary. The naive "just INSERT a curated list" has two latent failure modes this
plan closes: (a) the gate appears open in PR-6 but slams shut at Phase-6 **runtime** if a
curated symbol is mis-cased/absent relative to what the PGx/carrier pipelines consume —
closed by deriving the PGx half from the consumed tables + a raising coverage gate;
(b) a partial-INSERT can leave an orphan `annotation_source_versions` row (finding-015) —
closed by the clinvar-exact rollback-then-cleanup ordering.

### 3. Constraints
- **#7 supersession:** `genes` is not in `_SUPERSESSION_TABLES` and is not a registered
  loader; this is a one-time static FK-satisfying seed, so it correctly bypasses the
  version-pointer flip. **No** `annotation_sources` pointer for `hgnc` is created; the seed
  writes the version row + rows and STOPS (must NOT call `flip_to_new_version`). Atomicity
  is still honored via a single transaction.
- **#8 provenance:** each row carries `retrieval_date` + `source_version_id` = the new
  `hgnc` svid (decision 2).
- **Immutable schema** (Things never to do #1): NO edit to `docs/schemas/` or `ddl/`.
  `rebuild_required=false`. The seed adds rows only.
- **No-refactor zones:** `insert_source_version` (source_versions.py — bare `conn.execute`,
  no internal begin/commit) and `commit_and_checkpoint` reused verbatim; the
  clinvar/pharmgkb transaction shape and CLI command registry reused unchanged.
- **Bulk-load:** PyArrow Table registration + `INSERT ... SELECT` (mirror
  `variant_aliases._insert_batch`), not `executemany` (trivial at ~1.1k rows, but locked).
- `variant_aliases` is **not** the precedent for version-row allocation (it reuses the
  dbsnp svid). The correct model is `clinvar.py:736` (allocate a NEW svid).
- Privacy: curated public reference data, no user genome data; structlog, no `print()`.

### 4. Implementation tasks
1. **(STOP gate — RESOLVED)** Seed content. ACMG SF v3.3 panel supplied + verified (see
   Dataset). PGx/carrier symbols derive in-code from `cpic_guidelines ∪ pharmgkb_annotations`.
2. **New module** `backend/src/genome/annotate/seed_genes.py` — a backfill beside
   `variant_aliases.py`, NOT a registered loader (lazy-imported from the CLI, absent from
   `loaders/__init__.py` eager imports). Module constants: `SOURCE_DB='hgnc'`, a
   `SEED_VERSION` label, and the static ACMG SF v3.3 list (from the CSV) as a typed/frozen
   structure carrying `(gene_symbol, is_acmg_sf=TRUE, acmg_sf_disease, acmg_sf_inheritance,
   acmg_sf_version)`. Expose `seed_genes(conn=None, *, force=False) -> GeneSeedResult`. Use
   the `duckdb_connection()/nullcontext(conn)` ctx pattern so tests can pass an in-memory conn.
   Non-ACMG columns (`ensembl_gene_id`, `chrom`, `start/end_grch38`, …) stay NULL.
3. **Build the row set** before any write: query `cpic_guidelines + pharmgkb_annotations`
   for DISTINCT gene_symbol (current-version-scoped); merge with the static ACMG list
   (set-union on gene_symbol; for symbols in both, `is_acmg_sf=TRUE` **and**
   `is_pgx_relevant=TRUE` + carry ACMG metadata); mark PGx-derived symbols
   `is_pgx_relevant=TRUE`. Idempotency: if `COUNT(genes)>0` and not `force`, return early
   `already_populated=True`. `retrieval_date = datetime.now(UTC)` captured once.
4. **Write** (clinvar-exact, decision 3): `insert_source_version(conn, source_db='hgnc',
   version=SEED_VERSION, source_url=None, source_file_hash=<sha256 of the SORTED canonical
   seed payload>, source_file_size=<len bytes>, record_count=None, notes=...)` in AUTOCOMMIT
   → capture `new_svid` (will be 11) → `conn.begin()` → (if `force`: assert `genes` is a
   leaf across **all five** dependents — the four derived_* tables **and** `pathway_genes`
   (ddl/group_2:451) — raise if any reference exists; then `DELETE FROM genes`) → PyArrow
   `INSERT ... SELECT` with `source_version_id=new_svid` + `retrieval_date` → `UPDATE
   annotation_source_versions SET record_count=<n>` → `commit_and_checkpoint`. On
   `except`: `conn.rollback()` THEN `_cleanup_orphan_version_row(new_svid)` (best-effort
   DELETE, swallow+log); re-raise. Do NOT `flip_to_new_version`.
5. **CLI** `seed-genes` in `cli.py`, mirroring `annotate_refresh_aliases` /
   `annotate_refresh_index` (lazy import in the body, `--force` flag). Echo a one-line
   summary of `GeneSeedResult`: `source_version_id, already_populated, genes_rows,
   acmg_sf_genes, pgx_genes, cpic_covered, pharmgkb_covered`. Ensure `annotate --help`
   lists it.
6. **Coverage gate INSIDE `seed_genes`** (raising, not prose): after the INSERT, compute
   the two `EXCEPT` probes — (DISTINCT cpic gene_symbol, current-version-scoped) `EXCEPT`
   (genes), and the pharmgkb equivalent — carry cardinalities as `cpic_uncovered /
   pharmgkb_uncovered`. Both must be 0 by construction (the PGx half is derived from those
   tables); if either is non-zero, raise `GeneSeedCoverageError` (a wiring bug).
7. **Export** `seed_genes / GeneSeedResult` from `annotate/__init__.py`. **CHANGELOG.md**
   `[Unreleased]` entry. **Amend finding-020** (line ~554) noting the genes seed was pulled
   forward to PR 6 from Phase 7 (clears the stale-scope freshness flag).

### 5. Tests (`backend/tests/`)
New (plan-blind test-author writes from this spec + §6, not the implementation):
- `test_seed_genes_keystone_fk_satisfied` — fully-specified fixture (throwaway
  `analysis_runs` + one `variants_master`); seed incl. gene G; a `derived_acmg_sf_findings`
  insert using seeded `gene_symbol=G` with ALL co-required NOT NULLs **succeeds**. Only
  `gene_symbol` is the free FK variable.
- `test_seed_genes_fk_rejects_unseeded_symbol` — negative control; same fixture, insert with
  `gene_symbol='ZZZ_NOT_A_GENE'` **raises**, and the error names `genes`/`gene_symbol`
  (fail loudly if it instead names `variants_master`/`analysis_runs`).
- `test_seed_genes_atomic_no_orphan_version_row` — force failure mid-INSERT; assert
  `genes`=0, `annotation_source_versions WHERE source_db='hgnc'`=0, and cleanup raised no
  secondary exception.
- `test_seed_genes_provenance_columns` — every row non-NULL `retrieval_date` +
  `source_version_id`==new hgnc svid; exactly one `hgnc` version row, `record_count`==COUNT;
  **+ across-run hash stability** (two seed runs over the same input → byte-identical
  `source_file_hash`; closes pre-mortem #3).
- `test_seed_genes_no_pointer_flip` — no `annotation_sources` row for `hgnc`;
  `annotation_sources` total unchanged; an unrelated pointer (gnomad) unchanged.
- `test_seed_genes_coverage_gate_against_consumed_tables` — seed cpic+pharmgkb rows under a
  flipped pointer; assert both `EXCEPT` probes return 0 and `cpic_uncovered==pharmgkb_uncovered==0`.
- `test_seed_genes_idempotent` — second run without force → `already_populated=True`, counts unchanged.
- `test_seed_genes_force_reseeds` — `force=True` DELETE+re-INSERT under a FRESH svid; the
  leaf-check **raises** (not silently DELETEs) when any of the **five** dependents reference `genes`.
- `test_seed_genes_appears_in_help` — mirror `test_annotate_refresh_index_appears_in_help`
  (test_annotate_cli.py:318).
- `test_seed_genes_populates_and_echoes_summary` — CLI on a fresh `init_databases()` DB.

Must still pass: `test_annotate_cli.py`, `test_init_schema.py`,
`test_loaders_variant_aliases.py`, the clinvar/pharmgkb/variant_aliases loader suites; full
`pytest`, `ruff check`, `ruff format --check`, `mypy --strict backend/src`.

### 6. Verification (real-data gate)
- `seed-genes` summary: `already_populated=False`, `genes_rows=N` (N = |84 ACMG ∪ cpic19 ∪
  pharmgkb1086|, deduped → ~1.1k-order), `cpic_covered=True`, `pharmgkb_covered=True`,
  `source_version_id=11`.
- Provenance: `COUNT(annotation_source_versions WHERE source_db='hgnc')==1`, its svid==11
  (= locked MAX 10 + 1); `COUNT(genes WHERE source_version_id IS NULL)==0`;
  `COUNT(genes WHERE retrieval_date IS NULL)==0`.
- **Gate-clear coverage:** `(DISTINCT cpic gene_symbol, current-version-scoped) EXCEPT
  (genes) == 0` AND the pharmgkb equivalent `== 0`.
- **Gate-clear probe-INSERT:** scratch txn — one `analysis_runs` + one `variants_master` +
  one `derived_acmg_sf_findings` (seeded ACMG gene) succeeds; same with `gene_symbol=
  '__no_such_gene__'` raises an FK error naming `genes`; ROLLBACK.
- **Negative control (must be byte-unchanged):** `variants_master==3,160,364`;
  `annotation_sources` gnomad pointer `source_version_id==10` (no stray flip);
  `annotation_sources` total `==7` (no `hgnc`/`genes` pointer); obs#4 index match counts
  (gnomad_matches 3,054,426 / clinvar_matches 61,926 / gwas_matches 66,742 /
  pharmgkb_matches 1,737 / row_count 3,077,001) unchanged — `refresh-index` is NOT run here.
- Idempotence: second `seed-genes` → `already_populated=True`, counts unchanged.
- Dev-loop green; ruff/format clean; mypy --strict clean.

### 7. Out of scope
Full `genes`/`traits`/`pathways` dictionaries + HGNC bulk loader (Phase 7);
`variants_master.is_acmg_sf` (Phase-6 ACMG detection, finding-005 #5; distinct from the
gene-level `genes.is_acmg_sf` flag); `traits` / `pathways` / `pathway_genes` seeds;
ACMG SF severity escalation; re-running `refresh-index`/`merge`/`canonicalize`; registering
`hgnc` as a `refresh --source` loader or adding `_SUPERSESSION_TABLES`/`annotation_sources`
pointer; sourcing PGx/carrier symbols from anywhere but the loaded cpic/pharmgkb tables;
ACMG "variants to report" reporting rules.

### 8. Handoff
`/handoff` at session end.

---

## Pre-mortem watch-items (predicted surprises)

1. **(med) Gate reopens-then-recloses at Phase-6 runtime** — a carrier/ACMG insert carries a
   `gene_symbol` (or casing) sourced OUTSIDE `cpic ∪ pharmgkb ∪ ACMG` (e.g. a ClinVar gene
   column or PharmCAT/HIBAG tool-internal symbol). The §6 `EXCEPT`-zero gate is zero *by
   construction* and cannot catch this. **Backstop:** keystone probe-INSERT; named as the
   plan's riskiest_assumption. **Carry into Phase-6's first task:** before the first derived_*
   insert, run `SELECT DISTINCT gene_symbol FROM <phase6_emitted_set> EXCEPT SELECT
   gene_symbol FROM genes` and top up any gap.
2. **(low) `--force` reseed after Phase-6 rows exist** — `genes` has **5** FK dependents
   (4 derived_* + `pathway_genes`), not 4. The force-path leaf-assert must enumerate all
   five and RAISE (handled in §4/§5).
3. **(low) Non-deterministic seed hash** — `insert_source_version` requires a
   `source_file_hash`; hashing the union in set-iteration order makes it unstable. **Fix:**
   sort the union before hashing; assert across-run stability (test in §5).
4. **(low) `gene_variant_summary_v` returns 0 per gene** — the view LEFT JOINs
   `variants_master` on `chrom/start/end_grch38`, which the FK-subset seed leaves NULL, so
   every gene shows `user_variants_in_gene=0` until the Phase-7 dictionary backfills
   coordinates. Not a gate blocker; note in handoff so it is not mistaken for data loss.

## Auditor notes (verdict `ready`, no blockers)
- Add the across-run `source_file_hash` stability assertion (folded into §5).
- The manifest/finding-020 "genes is a leaf with no FK dependents" is **stale** — it has
  five dependents; the plan's force-path already handles all five.
- The §6 coverage gate is intrinsically circular (covers the cpic/pharmgkb leg only); the
  residual is bounded to Escalation A + the Phase-6 entry check above.

## Implementation orchestration
Run via the **implement-review** structure (finding-034 Stage 2 + Stage 3) — see the
session-start prompt (`pr-6-session-start-prompt.md`). Note: `.claude/workflows/implement-review.js`
targets an abstract subagent primitive and lacks `export const meta`; like `plan-phase.js`
it must be run via a Workflow-dialect port (or the `scope-run` skill), not verbatim. Tier 1 →
Stage 2 = implementer + green-keeper + test-author + plan-adherence-sentinel +
silent-failure-hunter; Stage 3 = convention-compliance + phi-pii-guardian agents +
`/code-review` (+ `/security-review`, data surface) → finding-verifier → review-synthesizer.
Given PR 6 adds tests, also run **test-integrity** and **pr-test-analyzer** in Stage 3.
Ends at Human Gate 2 (VSC-User runs `docs/runbooks/verification.md` + merges); then `close.js`.

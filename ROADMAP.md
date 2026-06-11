# Build Roadmap

Phases are sequential. Do not start phase N+1 until phase N's verification passes.

**Current phase:** Phase 5 closed; executing the pre-Phase-6 cleanup sequence (PRs 1–4 landed, PR 5 next) before Phase 6 begins.

## Phase 1 — Foundation (this is the bootstrap)

**Status:** complete.

Project layout, DDL extraction, DB initialization, config, CLI, basic tests. **Verification:** `genome init` works on a clean checkout; `pytest` green; `mypy --strict` clean.

## Phase 2 — Ingestion

**Status:** complete (see findings 001, 003, 004).
- Parse 23andMe and Ancestry raw exports
- Normalize to GRCh38 (lift-over via `pyliftover` or chain files)
- Strand resolution (with palindrome flagging)
- Multi-allelic split
- Populate `variants_master`, `genotype_calls`, `ingestion_runs`
- Compute `sample_qc`
- CLI: `genome ingest --source 23andme path/to/file.txt`

**Verification:** ingest both fixture files; `variants_master` populated; `sample_qc` row produced; tests cover format edge cases.

## Phase 3 — Merge & discrepancy detection

**Status:** complete (see findings 002, 005).

- Variant matching via three-tier strategy (chr:pos:ref:alt → rsid → fuzzy with strand)
- Compute `consensus_genotypes` via `consensus_v1` rule
- Detect and catalog discrepancies (six types, four severity levels)
- CLI: `genome merge`

**Verification:** known mismatches in fixture data are correctly flagged; concordance rate computed; per-source counts match the Venn-diagram view.

## Phase 4 — Local imputation (Beagle 5.5)

**Status:** complete (see findings 006, 007).

- Export merged consensus calls to per-chromosome VCFs (autosomes + X + Y)
- Run Beagle 5.5 locally against the 1000 Genomes Phase 3 reference
  panel on GRCh38, with the corresponding PLINK genetic map
- Parse imputed VCFs; integrate with imputation_dr2 (Beagle's INFO/DR2)
  per call
- Reference panel management: standard on-disk location under
  ~/.cache/genome/imputation/, validation, optional one-time download
- CLI: `genome imputation prepare | run | import | list` plus
  `genome imputation panel install | status` for one-time setup

**Verification:** end-to-end roundtrip works on chr22 alone first;
`is_imputed` flags correct; DR² distribution sane; full-genome run
completes against real 23andMe + Ancestry corpus.

## Phase 5 — Reference annotation loaders

**Status:** complete — 5.0–5.7 shipped; the phase is closed (5.7 PR #62).

- Per-source downloaders (ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog metadata, gnomAD filtered, dbSNP filtered)
- Each writes to `annotation_source_versions` and the per-source table; supersession is via the version-pointer pattern (see CLAUDE.md #7 and [`finding-010`](docs/findings/finding-010-version-pointer-supersession-pattern.md))
- Refresh `variant_annotations_index` rollup across all loaded sources
- CLI: `genome annotate refresh [--source ...]`

Sub-phase status:
- [x] 5.0 — Loader scaffold (PR #33)
- [x] 5.1a — PharmGKB loader (PR #34)
- [x] 5.1b — CPIC loader (PR #35)
- [x] 5.2 — ClinVar loader (PR #36)
- [x] 5.3 — GWAS Catalog loader (PR #38)
- [x] 5.4 — PGS Catalog metadata loader (PR #39)
- [x] 5.5 — gnomAD filtered (PR #49)
- [x] 5.6 — dbSNP filtered (surrogate BIGINT PKs PR #57; filtered loader PR #59)
- [x] 5.7 — `variant_annotations_index` refresh (closes Phase 5; PR #62). Joins ClinVar / GWAS / gnomAD / PharmGKB into one sparse row per variant via `genome annotate refresh-index`. Ships with the VEP columns + `is_acmg_sf` NULL (Phase 6's VEP runner / ACMG SF detection backfill them via a later rollup refresh) and `is_curated` from ClinVar/PharmGKB only (CPIC excluded at variant level — no gene→variant mapping yet).

Follow-ups (not phase-bound): the version-pointer / truncation follow-ups formerly
listed here are now numbered PRs in the pre-Phase-6 sequence — PharmGKB/CPIC cosmetic
cleanup + `MAPPED_TRAIT_URI` (finding-010 #12) → PR 8, orphan-row cleanup procedure
(finding-010 #14) → PR 9, HEAD-failure version-label policy (finding-010 #13) → PR 10.
The one remaining non-actionable item, cross-source generalization of the version-pointer
pattern (finding-010 #15), is tracked under "Deliberately deferred" in that sequence.

Deferred to later phases:
- Genes / traits / pathways dictionary tables — primarily serve insight generation and rendering; defer to Phase 7. The loaders we ship in Phase 5 carry gene symbols and trait IDs inline, so the index does not need the dictionaries to do its joins. (The minimal FK-satisfying genes seed — gene symbols only, enough to unblock the four NOT NULL genes FKs — lands earlier as PR 6 in the pre-Phase-6 sequence; only the full genes / traits / pathways dictionaries with descriptions and rendering metadata defer to Phase 7.)

**Verification:** all seven annotation source loaders complete (ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog metadata, gnomAD, dbSNP); `variant_annotations_index` populated with the expected per-variant join across them (VEP columns NULL pending Phase 6's VEP runner); queries against `variant_full_v` view return joined annotations.

## Pre-Phase-6 sequence

**Status:** in progress — PRs 1–4 landed (#63, #64, #65, #70); PR 5 is next.

A 13-PR run that clears every dbSNP-dependent backfill, deferred-cleanup item,
and FK blocker before the Phase 6 analyses begin, so Phase 6 starts with no open
deferred items. Replaces the former "Post-5.7 backfills" slot and absorbs the
non-phase-bound follow-ups previously tracked under Phase 5. Sequence positions
("PR N") are stable references and are distinct from GitHub PR numbers.

**Backfills cluster** — data re-derivation of `variants_master` / `consensus_genotypes`
content, gated on the loaded dbSNP build (5.6) and on `variant_aliases` being populated
(the 5.6 loader shipped `dbsnp_annotations` only and left `variant_aliases` empty —
finding-016 #8):

- [x] **PR 1** — Pre-Phase-6 cleanup (docs + operational): off-by-one phase-number
  docstrings, the `annotations.md` "after a schema rebuild" reload sequence (gnomAD/
  dbSNP/refresh-index steps were missing), a hard-fail BGZF-EOF ingest guard
  (finding-008), and a `verify.sh` TMPDIR prelude. Docs/ops only. (#63)
- [x] **PR 2** — `variant_aliases` population from dbSNP `RsMergeArch` via
  `genome annotate refresh-aliases` (finding-019). Fills the table the 5.6 loader left
  empty (finding-016 #8); attaches to the current dbSNP `source_version_id` (no pointer
  flip). The data dependency for PR 4. (#64)
- [x] **PR 3** — Canonical REF/ALT backfill + hom-only recovery + tier-3 consensus
  align (finding-020). `genome annotate canonicalize-variants` re-orients the
  alphabetical-ordering swap victims, recovers hom-only `ref==alt` rows from dbSNP,
  collapses same-canonical-key siblings, and repoints `genotype_calls` FKs; companion
  `genome annotate align-tier3-consensus` runs after `merge`. Closes finding-005 #1
  (ordering aspect) and #6. Deliberate concordance re-lock to 0.999776 (finding-018
  anticipated this; not a regression). The strand-flip `variants_master` collapse is
  deferred to PR 5. (#65)
- [x] **PR 4** — Tier-2 rsID matching in `refresh-index`, consuming the `variant_aliases`
  map from PR 2 (finding-005 #4). Both user-side and source-side rsIDs canonicalize
  through the dbSNP alias map; real-data lift `gwas_matches` 66,701→66,764 /
  `pharmgkb_matches` 1,737→1,738, coord-keyed counts unchanged (finding-025). (#70)

**Remaining cleanup** — clears the deferred backlog so Phase 6 opens clean:

- [ ] **PR 5** — chrX resolution, Option B (sex-aware non-PAR/PAR regions; finding-008)  **← next**
  **+** the deferred strand-flip `variants_master` collapse: the tier-3 strand-flipped
  pairs that Scope-A canonicalize (PR 3) leaves as two rows, requiring `genotype_calls`
  allele complementing via supersession (finding-005 #1, deferred sub-item).
- [ ] **PR 6** — Minimal `genes` seed, Option A: the gene-symbol union of the
  ACMG SF v3.x, PGx, and carrier gene lists. Enough rows to satisfy the
  `NOT NULL REFERENCES genes(gene_symbol)` FKs on `derived_pgx_phenotypes`,
  `derived_carrier_findings`, `derived_acmg_sf_findings`, and `derived_compound_het`,
  which otherwise block every Phase 6 insert into those tables. This is the
  FK-satisfying subset only — the full `genes` / `traits` / `pathways` dictionaries
  (descriptions, rendering metadata) remain deferred to Phase 7.
- [ ] **PR 7** — finding-015 orphan gnomAD cleanup, **Option C**: one-off
  `DELETE` of the pre-existing orphan `annotation_source_versions` rows (gnomAD
  v6/v7/v8/v10, zero `gnomad_frequencies` references each). Distinct from PR #53, which
  shipped finding-015 Option B (loader hardening to prevent *future* orphans) but
  deliberately left these rows in place.
- [ ] **PR 8** — Deferred docs/cosmetic batch: the `MAPPED_TRAIT_URI` truncation finding
  entry (finding-005, deferred from 5.3), the imputation docstring filename fix, and the
  PharmGKB/CPIC `already_current=True` cosmetic cleanup (finding-010 #12).
- [ ] **PR 9** — finding-010 #14: orphan-row cleanup *procedure* for rows under
  superseded `source_version_id`s, plus a runbook entry (covers `variant_aliases`
  orphans too). General/ongoing, vs. PR 7's one-off gnomAD-specific delete.
- [ ] **PR 10** — finding-010 #13: HEAD-request-failure version-label policy — write
  its own finding, decide refuse-vs-fallback, implement.
- [ ] **PR 11** — finding-008: `register-existing-result` CLI command, collapsing
  the full-archive rebuild workflow.
- [ ] **PR 12** — Top-level CLI test module for `init` / `status` / `config get|set` /
  `version` (audit item 3.2; currently uncovered).
- [ ] **PR 13** — gnomAD total-reopen drift sentinel on the `gnomad.refresh.complete`
  event (finding-012 #12).

**Out-of-sequence fix that landed mid-run** (not a numbered slot):

- [x] **#66** — Imputation rsID hygiene (finding-021): a strict `^rs[0-9]+$` ingest
  predicate plus a standalone `genome imputation normalize-rsids` sweep, NULLing the
  ~2.26M synthetic Beagle `chr:pos:ref:alt` rsIDs that were the root cause of PR 3's
  rsID-loss. Merged between #64 and #65; PR 3 was rebased onto it before landing.

**Deliberately deferred** — NOT in the sequence; each is gated on a future signal that
hasn't arrived, tracked in findings for when it does:

- Cross-source generalization of the version-pointer pattern (finding-010 #15)
- Generalize the hash-match fallback into a shared helper
- Hash-as-canonical-identity refactor
- `annotate inspect --source URL` schema-inspection helper

**Phase 6 entry is gated on:** PRs 4–6 in particular (tier-2 rsID matching, chrX
Option B, minimal `genes` seed), plus the locked conventions — supersession-over-update,
operation-level provenance without schema changes, and the PyArrow / INSERT-SELECT
bulk-load pattern.

## Phase 6 — Analysis pipelines
- Load `pgs_score_weights` (per-variant PGS weights, overlapping-only per locked decision #5) → PRS computation against PGS Catalog
- PharmCAT integration → `derived_pgx_phenotypes`
- Carrier detection rules
- ACMG SF detection — first task: populate `variants_master.is_acmg_sf` from the curated ACMG SF v3.x gene list intersected with ClinVar rows (finding-005 #5), which unblocks Phase 3's deferred ACMG SF severity escalation
- HIBAG → `derived_hla_typing`
- VEP local runner against user variants → populates VEP columns in `variant_annotations_index` via the rollup refresh.
- ROH via plink2
- Y/mtDNA haplogroup assignment
- Global ancestry (RFMix or admixture)
- ROH summary, genome QC — including a profile-level QC rollup that combines per-source `sample_qc` rows into a single per-profile answer, resolving CLAUDE.md "Real-data observations" #1 (finding-005 #2)
- Each writes an `analysis_runs` row capturing source versions used
- CLI: `genome analyze [pgs|pgx|carrier|acmg|hla|roh|haplogroup|ancestry|qc|all]`

Follow-ups (gated on `pgs_score_weights` landing):
- gnomAD PGS coverage extension — append PGS-component variants to the active gnomAD source-version (append, not refresh; no version bump). See [`finding-011`](docs/findings/finding-011-gnomad-three-way-intersection.md).
- dbSNP PGS leg — extend the `user_only` dbSNP filter to PGS-component positions, mirroring the gnomAD extension. See [`finding-016`](docs/findings/finding-016-dbsnp-user-only-filter.md).

**Verification:** each pipeline produces non-zero output on the merged+imputed dataset; supersession works on re-run.

## Phase 7 — Insight generation
- Per-analysis-type insight generators in `genome.insights.*`
- Versioned tier mapping functions
- Confidence rollup
- Materialized `summary_dashboard` refresh job
- Audience rendering (eli5/layperson/clinical) lazily generated
- CLI: `genome insights regenerate [--type ...]`

**Verification:** an end-to-end run produces insights for every analysis type; every insight has at least one evidence row; tier rollup is consistent.

## Phase 8 — Backend API
- FastAPI app under `genome.api`
- Endpoints: summary dashboard, drill-downs (gene / pathway / trait / variant), discrepancy view, PGx medication checker, ACMG SF dashboard, snapshot list, audit dashboard
- Natural-language query endpoint (Claude tool-use loop over the schemas)
- Job worker process (`genome jobs run-worker`)
- Audit log middleware on every request

**Verification:** OpenAPI spec covers all groups; integration tests exercise the worker; NL query produces correct DuckDB queries on fixture questions.

## Phase 9 — Frontend
- Next.js scaffold
- Home dashboard (the rollup)
- Gene drill-down
- Trait drill-down with Manhattan plot
- Variant detail page
- Discrepancy view
- Karyogram (D3) with notable variants
- Chronotype/nutrition/PGx pages
- Chat/query interface
- Doctor-ready PDF export

**Verification:** clickable end-to-end demo from dashboard to SNP detail to evidence citations.

## Phase 10 — Privacy hardening, polish, snapshots
- External call audit dashboard
- Sanitized export modes
- Snapshot create / restore / diff (the "what changed" feed)
- ClinVar-update notifications
- Performance pass on `variant_annotations_index` refresh
- Optional: `age`-encrypted backup script

**Verification:** privacy dashboard accurate; snapshot restore reproduces a prior state; backup script roundtrips.

## Out of scope for v1
- Multi-profile UI (schema is ready; UI deferred)
- Whole-genome sequencing input
- Drug-drug interaction modeling (DrugBank)
- Cloud sync / sharing
- Mobile native app

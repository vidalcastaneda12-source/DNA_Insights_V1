# Build Roadmap

Phases are sequential. Do not start phase N+1 until phase N's verification passes.

**Current phase:** Phase 5 closed; executing the pre-Phase-6 cleanup sequence (PRs 1–6 landed; PR 7 next) before Phase 6 begins. PR 6 (minimal `genes` seed, #88) cleared the Phase-6 FK gate.

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

**Status:** in progress — PRs 1–6 landed (#63, #64, #65, #70, #74, #88); PR 7 is next
(but re-scope first — see its ⚠️ note; it may be moot against the live DB).

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
  deferred as its own separately-tracked item (finding-005 #1 / finding-020 "Out of
  scope"), distinct from PR 5's two halves. (#65)
- [x] **PR 4** — Tier-2 rsID matching in `refresh-index`, consuming the `variant_aliases`
  map from PR 2 (finding-005 #4). Both user-side and source-side rsIDs canonicalize
  through the dbSNP alias map; real-data lift `gwas_matches` 66,701→66,764 /
  `pharmgkb_matches` 1,737→1,738, coord-keyed counts unchanged (finding-025). (#70)

**Remaining cleanup** — clears the deferred backlog so Phase 6 opens clean:

- [x] **PR 5** — chrX resolution + same-SNP duplicate collapse (two independent halves)
    - [x] **5b-pre** + **5b** — `consensus_v1` chip-no-call fix + `collapse-duplicate-variants`
      (≈684 duplicates across five mechanisms reconciled; finding-005 #1 closed;
      findings 026/027/028). Merged; new anchor variants_master/consensus 3,088,233.
    - [x] **5a** — chrX resolution via M3-physical region split (PR #74; sex-aware
      PAR1/non-PAR/PAR2 physical panel subsets; findings 029/031/033; closes
      finding-008). Supersedes the original Option-B framing.
- [x] **PR 6** — Minimal `genes` seed, Option A: the gene-symbol union of the
  ACMG SF v3.3 panel and the in-DB CPIC/PharmGKB symbols. Enough rows to satisfy the
  `NOT NULL REFERENCES genes(gene_symbol)` FKs on `derived_pgx_phenotypes`,
  `derived_carrier_findings`, `derived_acmg_sf_findings`, `derived_compound_het`,
  and `pathway_genes` (five dependents, not four — `genes` was never a leaf),
  which otherwise block every Phase 6 insert into those tables. This is the
  FK-satisfying subset only — the full `genes` / `traits` / `pathways` dictionaries
  (descriptions, rendering metadata) + HGNC bulk loader remain deferred to Phase 7.
  `genome annotate seed-genes`; one-time static backfill under a fresh `hgnc`
  `annotation_source_versions` row (no `annotation_sources` pointer flip). Gate-confirmed
  on the live corpus (Human Gate 2, 2026-06-23): `genes`=1153 (|84 ACMG ∪ 1086 PGx|,
  overlap 17), `is_acmg_sf`=84 / `is_pgx_relevant`=1086, `source_version_id`=11,
  `cpic_covered`/`pharmgkb_covered`=True; negative control byte-unchanged. See
  CLAUDE.md "Real-data observations" #7, [`finding-020`](docs/findings/finding-020-canonical-refalt-backfill.md)
  "Out of scope" amendment, and verification.md "PR 6 genes seed gate". (#88)
- [ ] **PR 7** — finding-015 orphan gnomAD cleanup, **Option C**: one-off
  `DELETE` of the pre-existing orphan `annotation_source_versions` rows (gnomAD
  v6/v7/v8/v10, zero `gnomad_frequencies` references each). Distinct from PR #53, which
  shipped finding-015 Option B (loader hardening to prevent *future* orphans) but
  deliberately left these rows in place.
  - **⚠️ Re-scope before running (post-PR-C, 2026-06-22):** the `v6/v7/v8/v10` list is
    **stale**. The live (rebuilt) DB has only `source_version_id=8` (superseded,
    4,467,370 rows) + `source_version_id=10` (**active**, 4,568,802 rows) — the
    `IN (6,7,8,10)` DELETE would erase the **active** gnomAD version + the superseded
    build (data loss). Re-derive the real zero-row orphan set against the live DB first;
    by current evidence PR 7 may be **moot** (no zero-row orphans by those ids). See
    finding-015 "Amendment — post-PR-C reload" + CLAUDE.md obs #4.
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

**Phase 6 entry is gated on:** the minimal `genes` seed (PR 6) — **now landed (#88)**,
gate-confirmed `genes`=1153 unblocking the five `derived_*` / `pathway_genes` FKs (see
CLAUDE.md "Real-data observations" #7); PRs 4 (tier-2 rsID matching, #70) and 5 (chrX
M3-physical, #74) had already landed. The FK gate is therefore **cleared** — Phase 6's
remaining entry conditions are the locked conventions: supersession-over-update,
operation-level provenance without schema changes, and the PyArrow / INSERT-SELECT
bulk-load pattern. (The remaining open pre-Phase-6 slots — PRs 7–13 — are cleanup that
does not block Phase-6 entry.)

## Sub Project B2 — scope-split (Phase 1)

The smart-cut detector (`genome scope-split`, finding-039): read a Stage-0 dispatcher
scope manifest and propose whether a scope is **separable** into independently-shippable
sub-scopes, or is one indivisible unit (atomic). Manifest-primary cut policy with the
git-grep import graph as a veto signal; fail-closed (a false split is the costliest mode,
so the detector under-proposes by construction). Phase 1 is the detector only — no
campaign runner, no auto-running of sub-scopes.

- [x] **B2-Phase1** — `genome.scope_split` smart-cut detector + `scope-split` sub-app
  (check / dry-run / write-roadmap), the Stage-0.5 split-check micro-gate hook, and the
  managed ROADMAP block below. DB-free core; placeholder sub-scope ids only.

The block between the sentinels is **managed by `genome scope-split write-roadmap`** — do
not hand-edit it; the writer replaces only the inter-sentinel region (append-only).

<!-- B2-SUBSCOPES:BEGIN -->
<!-- B2-SUBSCOPES:END -->

## Sub Project C2+D — Workflow-Engine Migration

Port the per-scope agent-team orchestrators from the model-driven `runAgent()` probe-shim to
the real dynamic-workflows **engine dialect** and make the deterministic JS workflows the
**engine-primary** path, while retaining the model-driven `/scope-run` conductor as the
by-name segment launcher and the headless/cron fallback. Both human gates (plan approval;
merge verification) are unchanged. The reversal is recorded **pure-append**
([`finding-034`](docs/findings/finding-034-agent-team-plan-phase.md) Amendment / `DEC-0099`;
the finding-034 design `DEC-0020` is left active and unflipped, per the `DEC-0086`/`DEC-0087`
precedent). The Stage-1 gate package flagged that no ROADMAP slot existed for this work; this
is it, added at Stage-5 close.

- [x] **Phase 1** (PR #109 / `866d255`) — port
  `.claude/workflows/{plan-phase,implement-review,close}.js` to the engine dialect
  (pure-literal `export const meta`, self-contained body, injected
  `agent · parallel · pipeline · log · phase · budget` hooks, schema-validated `agent()` calls
  replacing the hand-rolled coercion, top-level `return`); close six fidelity gaps (Tier-0
  minimal-diff planner; Tier-2 architect-reviewer folded into one severity→verdict ladder;
  Stage-4 `handoff-assembler` wired on the `go` path; the four trigger-gated Stage-2 writers on
  real triggers, `fan-out-implementer` replacing the single implementer; severity-scaled
  refute-by-default verification; budget-guarded escalation); record the
  model-driven→engine-primary reversal (`DEC-0099`, pure-append); add the `node:test` harness
  (87 tests · 86 pass · 1 intentional Phase-2 skip · 0 fail); and fail-closed-harden the
  `parallel`/`pipeline` fan-out seams. The engine load model was empirically confirmed by a
  committed live-engine probe
  ([`c2d-load-probe-wf_a37802b2-c92.js`](docs/findings/c2d-load-probe-wf_a37802b2-c92.js), run
  `wf_a37802b2-c92`; see finding-034's probe appendix). JS-orchestration + docs only — no
  Python / schema / DB change (the dev-loop stayed byte-unchanged; `manifest.applicable_anchors`
  was `[]`, no real-data anchors). Gate recipe: verification.md "C2+D Phase 1 gate
  (engine-dialect workflow port)".
- [ ] **Phase 2** — the engine-primary CLI + the Python-CLI **reversal-gate** (the one
  intentionally-skipped `drift.test.mjs` test — EC3's single skip). **Residual carried from
  Phase 1:** the four trigger-gated Stage-2 writers are **deferred-unverified (D7)** (exercised
  only on synthetic manifests; live-engine RUN semantics not yet validated), and the `arch-1`
  drift-guard seam-coverage gap is latent/backlogged. See
  [`finding-034`](docs/findings/finding-034-agent-team-plan-phase.md) "C2D-Phase1 residual risk".

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
- gnomAD PGS coverage extension — append PGS-component variants to the active gnomAD source-version (append, not refresh; no version bump). See [`finding-011`](docs/findings/finding-011-gnomad-three-way-intersection.md). **Moot while the gnomAD filter is `user_only`** (adopted [`finding-035`](docs/findings/finding-035-gnomad-filter-set-consumer-audit.md), 2026-06-21): the extension would load gnomAD AF at PGS-component positions the user doesn't carry, which — like the ClinVar/GWAS legs finding-035 audited — nothing reads. Revival requires restoring `three_way`.
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

# Build Roadmap

Phases are sequential. Do not start phase N+1 until phase N's verification passes.

Each phase is organized into **Prerequisites** (entry-gating work that clears before the
deliverables begin), **Deliverables** (the phase's core scope), and **Follow-ups** (residual
cleanup, hardening, and process/tooling work attributable to the phase). Only non-empty buckets
are shown. Every top-level checklist item carries a frozen `RM-<7 hex>` id enforced by
`genome roadmap check` (finding-042 / `DEC-0125`); indented sub-bullets are detail and carry no
id. Existing `PR N` sequence labels are retained as a secondary alias.

**Current phase:** Phase 5 closed (5.0–5.7, #62). The pre-Phase-6 prerequisites have landed —
the minimal `genes` seed (#88) cleared the Phase-6 FK gate — so Phase 6 is unblocked but not yet
started. Residual cleanup from the completed phases is tracked under each phase's **Follow-ups**;
the remaining open items gate nothing.

## Phase 1 — Foundation (this is the bootstrap)

**Status:** complete.

### Deliverables

- [x] RM-a129447 — Project layout
- [x] RM-6088c68 — DDL extraction
- [x] RM-821211b — DB initialization
- [x] RM-c9b732b — config
- [x] RM-d1e2d22 — CLI
- [x] RM-4f9cb26 — basic tests

### Follow-ups

- [x] RM-c5bcb2d (PR 12) — Top-level CLI test module for `init` / `status` / `config get|set` /
  `version` (audit item 3.2; currently uncovered). (#144)
- [x] RM-e95c4a0 — Wire the dead `genome --version` eager flag into the `_main` callback (or remove
  it): `cli.py:1131-1139` defines `_VersionFlag` / `_print_version_and_exit`, but neither is
  referenced by the parameter-less `_main` callback, so `genome --version` is a no-op today (only the
  `genome version` subcommand prints the version). Surfaced by PR 12 / RM-c5bcb2d intake. (#153)

**Verification:** `genome init` works on a clean checkout; `pytest` green; `mypy --strict` clean.

## Phase 2 — Ingestion

**Status:** complete (see findings 001, 003, 004).

### Deliverables

- [x] RM-50b0db2 — Parse 23andMe and Ancestry raw exports
- [x] RM-ef1b89c — Normalize to GRCh38 (lift-over via `pyliftover` or chain files)
- [x] RM-b40b650 — Strand resolution (with palindrome flagging)
- [x] RM-9f62128 — Multi-allelic split
- [x] RM-7d16e12 — Populate `variants_master`, `genotype_calls`, `ingestion_runs`
- [x] RM-c7b1ad1 — Compute `sample_qc`
- [x] RM-01e86de — CLI: `genome ingest --source 23andme path/to/file.txt`

**Verification:** ingest both fixture files; `variants_master` populated; `sample_qc` row produced; tests cover format edge cases.

## Phase 3 — Merge & discrepancy detection

**Status:** complete (see findings 002, 005).

### Deliverables

- [x] RM-a13374c — Variant matching via three-tier strategy (chr:pos:ref:alt → rsid → fuzzy with strand)
- [x] RM-279f791 — Compute `consensus_genotypes` via `consensus_v1` rule
- [x] RM-80ae329 — Detect and catalog discrepancies (six types, four severity levels)
- [x] RM-442400a — CLI: `genome merge`

### Follow-ups

- [ ] RM-2aa5333 — **Build merge Tier-2 (cross-position rsID matching)** in `merge/pipeline.py` — distinct from the annotation-index tier-2 (PR 4, done); dependency `variant_aliases` is now loaded. (merge/pipeline.py:8-10; U7)

**Verification:** known mismatches in fixture data are correctly flagged; concordance rate computed; per-source counts match the Venn-diagram view.

## Phase 4 — Local imputation (Beagle 5.5)

**Status:** complete (see findings 006, 007).

### Deliverables

- [x] RM-8a97e54 — Export merged consensus calls to per-chromosome VCFs (autosomes + X + Y)
- [x] RM-a5a0426 — Run Beagle 5.5 locally against the 1000 Genomes Phase 3 reference
  panel on GRCh38, with the corresponding PLINK genetic map
- [x] RM-bfe122e — Parse imputed VCFs; integrate with imputation_dr2 (Beagle's INFO/DR2)
  per call
- [x] RM-edd2af0 — Reference panel management: standard on-disk location under
  ~/.cache/genome/imputation/, validation, optional one-time download
- [x] RM-4bf2cb5 — CLI: `genome imputation prepare | run | import | list` plus
  `genome imputation panel install | status` for one-time setup

### Follow-ups

- [x] RM-7fba363 (PR 11) — finding-008: `register-existing-result` CLI command, collapsing
  the full-archive rebuild workflow.
- [x] RM-1f18fcc (#66) — Imputation rsID hygiene (finding-021): a strict `^rs[0-9]+$` ingest
  predicate plus a standalone `genome imputation normalize-rsids` sweep, NULLing the
  ~2.26M synthetic Beagle `chr:pos:ref:alt` rsIDs that were the root cause of PR 3's
  rsID-loss. Merged between #64 and #65; PR 3 was rebased onto it before landing.
- [ ] RM-42bb7df — PR 11 register-existing-result review residuals: (silent-1) make the manifest count-coercion fail-closed so a chrom with a malformed variants_per_chrom count AND an absent result VCF is refused, not silently dropped — scope the strictness to the register consumer to avoid regressing import's shared use of _load_manifest_variants_per_chrom; (ptest-2) add a test for the expected_count==0 branch in _result_vcf_incomplete_reason. (Surfaced by the PR 11 Stage-3 review; captured via /fast-follow.)
- [ ] RM-b9043cd (PR 14) — Deferred pipeline / imputation residuals (surfaced by the 2026-06-26 repo
  sweep — each was a deferral whose original fold target landed without absorbing it, so it had
  no slot):
  - finding-005 #9: `pos_grch37` not re-coalesced across the `canonicalize-variants` collapse
    (the survivor INSERT inherits only the `MIN(old_variant_id)` representative's GRCh37 coord;
    divergent/NULL movers are dropped, not coalesced). Needs a re-liftover / GRCh37-recoalesce
    pass. **Low severity** — GRCh38 (the project's primary) and the GRCh38-keyed consensus / index
    are unaffected; only the alongside-stored GRCh37 value is at issue.
  - finding-027: the upstream `vcf_export.py` panel-strand reconciliation that stops *new*
    duplicate `variants_master` rows from being created (PR 5b collapsed only the *existing* ones).
    Fold into a future `imputation prepare` / re-impute PR.
  - finding-021: recover chip-probe IDs to canonical rsIDs (`kgp`→`rs`, unwrap `acom_rs…`) —
    alias-format normalization that PR 4's merged-rsID resolution (finding-025) did not cover.
- [ ] RM-1fa3abc — **`prepare-chrx` haploid-count short-circuit perf fix** (finding-030): replace the O(variants×samples) exact `count_haploid_gts` with a first-haploid-GT short-circuit at the three existence-only assertion sites; cuts ~55 CPU-min. Recommended-not-applied. (imputation.md:294; CHANGELOG [Unreleased]; U4)
- [ ] RM-ec3d69e — **Persist `--sex` to `sample_qc.sex_expected` + COALESCE in `consensus_chrx_dosage_v`** for the all-ambiguous-profile chrX edge (today raises / passes uncorrected). (finding-029 / finding-031; U13)
- [ ] RM-ba44f41 — **Autosomal re-impute to capture PR-3-recovered hom-only positions** (chrX was re-imputed in 5a; autosomes not). Operator-gated 30-min op. (finding-020; U14)
- [ ] RM-ca2c96e — **HRC reference-panel support + bref3 conversion** for the imputation pipeline (optional enhancements; live only in the Phase-4 plan). (finding-007; U15)

**Verification:** end-to-end roundtrip works on chr22 alone first; `is_imputed` flags correct; DR² distribution sane; full-genome run completes against real 23andMe + Ancestry corpus.

## Phase 5 — Reference annotation loaders

**Status:** complete — 5.0–5.7 shipped; the phase is closed (5.7 PR #62).

### Deliverables

- [x] RM-241de10 — Per-source downloaders (ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog metadata, gnomAD filtered, dbSNP filtered)
- [x] RM-c8195b3 — Each writes to `annotation_source_versions` and the per-source table; supersession is via the version-pointer pattern (see CLAUDE.md #7 and [`finding-010`](docs/findings/finding-010-version-pointer-supersession-pattern.md))
- [x] RM-7a48cb7 — Refresh `variant_annotations_index` rollup across all loaded sources
- [x] RM-de35483 — CLI: `genome annotate refresh [--source ...]`

#### Sub-phase status

- [x] RM-371e8a3 5.0 — Loader scaffold (PR #33)
- [x] RM-2670da4 5.1a — PharmGKB loader (PR #34)
- [x] RM-b4ce224 5.1b — CPIC loader (PR #35)
- [x] RM-850a0b1 5.2 — ClinVar loader (PR #36)
- [x] RM-b31b89c 5.3 — GWAS Catalog loader (PR #38)
- [x] RM-e912822 5.4 — PGS Catalog metadata loader (PR #39)
- [x] RM-ffeda59 5.5 — gnomAD filtered (PR #49)
- [x] RM-5e7f390 5.6 — dbSNP filtered (surrogate BIGINT PKs PR #57; filtered loader PR #59)
- [x] RM-d345575 5.7 — `variant_annotations_index` refresh (closes Phase 5; PR #62). Joins ClinVar / GWAS / gnomAD / PharmGKB into one sparse row per variant via `genome annotate refresh-index`. Ships with the VEP columns + `is_acmg_sf` NULL (Phase 6's VEP runner / ACMG SF detection backfill them via a later rollup refresh) and `is_curated` from ClinVar/PharmGKB only (CPIC excluded at variant level — no gene→variant mapping yet).

**Verification:** all seven annotation source loaders complete (ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog metadata, gnomAD, dbSNP); `variant_annotations_index` populated with the expected per-variant join across them (VEP columns NULL pending Phase 6's VEP runner); queries against `variant_full_v` view return joined annotations.

### Follow-ups

The annotation-loader phase spawned the largest cleanup tail. It is grouped below into the
data/loader cleanup, the deferred loader items (each gated on a future signal), the development
process & tooling built during the post-5.7 window, and documentation hygiene.

#### Annotation-loader cleanup

- [x] RM-0f1a04d (PR 7) — finding-015 orphan gnomAD cleanup (**Option C**) — **closed as moot
  (2026-06-26).** The original one-off `DELETE` of zero-`gnomad_frequencies`-reference
  gnomAD `annotation_source_versions` rows (framed `IN (6,7,8,10)`) is empty against the
  live (rebuilt) DB. **Read-only PR-7 probe (2026-06-26):** the zero-row-orphan set is
  `[]`; the live gnomad inventory is `{8 (4,467,370 rows, superseded-with-data), 10
  (4,568,802 rows, active)}`, the `annotation_sources` pointer = `10`, and both ids carry
  matching `gnomad_frequencies` data — **no FK-safe orphan exists**. The stale
  `IN (6,7,8,10)` DELETE would have erased the **active** (id=10) + superseded (id=8)
  builds, so **no DELETE was executed**. Future-orphan *prevention* already shipped
  (finding-015 Option B, PR #53). The general superseded-row cleanup procedure (covering
  the data-bearing id=8 and `variant_aliases` orphans) remains **PR 9** (finding-010 #14) —
  not folded here. See finding-015 §12 (now inline-marked) + its Amendment closing note,
  CLAUDE.md obs #4, and `docs/runbooks/annotations.md` (gnomAD §5.5 "Orphan version rows").
- [x] RM-76ec5db (PR 8) — Deferred docs/cosmetic batch: the `MAPPED_TRAIT_URI` truncation finding
  entry (finding-005 #11, deferred from 5.3), the imputation docstring filename fix, and the
  PharmGKB/CPIC `already_current=True` cosmetic cleanup (finding-010 #12). Merged #131
  (2026-06-30); verify-gate GREEN (change_class=core; negative-control held — no DB anchor moved).
  Spun off RM-85121ee (the deferred `mapped_trait_uri VARCHAR[]` schema fix) + RM-035c394 (the
  implement-review pytest-poll wedge).
- [x] RM-12873bf (PR 9) — finding-010 #14: orphan-row cleanup *procedure* for rows under
  superseded `source_version_id`s, plus a runbook entry (covers `variant_aliases`
  orphans too). General/ongoing, vs. PR 7's one-off gnomAD-specific delete.
  **Landed #133 / `d4a07d6` (2026-06-30); verify-gate GREEN (change_class=core; 6 dev-loop
  steps PASS; integrity clean; tests 1713→1752).** `genome annotate purge-superseded`:
  retention **keep-1** (active + immediate prior kept per source; finding-010 #14),
  **dry-run default + mandatory read-only pre-execute probe + `--execute` opt-in** (the two
  VSC gate decisions). Gate-confirmed a **pure no-op** on the live corpus (`orphan_candidates=0`,
  every source `deletable=[]`) — the no-op is **corpus-conditional, not structural** (the orphan
  sweep would snapshot + delete a zero-data registry orphan if one existed). Two fail-closed
  guards: a **14-FK-child** per-column guard on `annotation_source_versions` (not the 8 in
  `_SUPERSESSION_TABLES`) + a `source_db` dangling-pointer check. See CLAUDE.md obs #8,
  [`verification.md`](docs/runbooks/verification.md) "PR 9 purge gate", finding-010 #14,
  `MEMORY.md` DEC-0126/DEC-0127.
- [x] RM-9f3c52c (PR 10) — Version-label correctness policy (two related defects):
  - finding-010 #13: HEAD-request-failure version-label policy — write its own finding,
    decide refuse-vs-fallback, implement. **Shipped: refuse (finding-043 / DEC-0148).**
  - finding-022 / finding-005 #10: the loader version label decouples from the cached bytes on a
    `rm -rf data/` rebuild reload — ClinVar/GWAS resolve the *current upstream* label (e.g. June)
    while loading *older cached* bytes (e.g. May), mislabeling `annotation_source_versions.version`
    (data correct, label wrong). Bind the persisted label to the loaded bytes — a sidecar
    `<file>.version` written on fresh download and read back on a cache-hit, or generalize
    finding-014's `maybe_skip_on_hash_match` to adopt the label of any prior row whose hash matches
    the cached file. (Folded here by the 2026-06-26 repo sweep; the named "next annotation-loader
    PR" fix point had no slot.) **Shipped: the sidecar shape (finding-043 / DEC-0149).**
  - Shipped 2026-07-01: OQ-1=A refuse (ClinVar HEAD failure propagates / raises, GWAS-symmetric)
    + the `<dest>.version` sidecar bind + inline version+hash steady-state guard (OQ-4=4a-i,
    `supersession.py` untouched). See [`finding-043`](docs/findings/finding-043-head-failure-version-label-policy.md),
    [`verification.md`](docs/runbooks/verification.md) "PR 10 version-label correctness gate",
    `MEMORY.md` DEC-0148/DEC-0149. The `maybe_skip_on_hash_match` generalization stays separate as
    RM-25072d2 (still open, below).
- [x] RM-3973250 (PR 13) — gnomAD **+ dbSNP** total-reopen drift sentinel on the
  `gnomad.refresh.complete` **and `dbsnp.refresh.complete`** events (finding-012 #12).
  **Landed #147 / `11da3f6` (2026-07-02); evidence-gated verify-and-merge GREEN — `change_class=core`,
  N/A anchors: this PR runs no refresh / no `refresh-index`, so the negative-control data anchors of
  CLAUDE.md obs #3/#4 held unchanged by construction; tests 1796 → 1818 (+22).** Surfaces the
  run-total htslib HTTP/2 reopen count as `reopens_total` on both `GnomadLoadResult` /
  `gnomad.refresh.complete` (parallel + sequential) and `DbsnpLoadResult` / `dbsnp.refresh.complete`
  (sequential-only), via a shared `remote_tabix.RemoteReadStats` out-param accumulator — the
  finding-012 #11 shared-machinery extraction now serves both loaders. `reopens_total` is
  **tolerance-banded network telemetry, not a re-lockable anchor** (fenced out of the byte-exact
  table; finding-012 #5; `0` healthy, record-the-value, never byte-match); on a failed run the gnomAD
  parallel path under-reports a dead spawn worker's unrecoverable partial — a documented
  accepted-limitation asymmetry (dbSNP is sequential-only). Folding the dbSNP mirror **in-scope**
  (rather than deferring it) meant **no separate dbSNP-mirror `RM-` slot was minted**. See CLAUDE.md obs #4,
  [`verification.md`](docs/runbooks/verification.md) "PR 13 gnomAD + dbSNP reopen-sentinel gate",
  finding-012 #10–#12, `MEMORY.md` DEC-0161.
- [ ] RM-fdbeb64 (ptest-1) — Optional loader-grain regression guard asserting the documented
  `reopens_total` failure-path asymmetry: a dead gnomAD parallel (`spawn`) worker contributes 0
  (its partial reopens are unrecoverable) while the sequential path — and all of dbSNP — surfaces a
  failed chromosome's partial. (Surfaced by the RM-3973250 / PR 13 Stage-3 review; finding-012 #12.)
- [ ] RM-a3b5d24 — **Pre-existing / optional** (NOT introduced by PR 13): tighten
  `_ChromResult.status` in `backend/src/genome/annotate/loaders/gnomad.py` from `status: str`
  (values `{"ok","failed"}`) to `Literal["ok","failed"]`. (Surfaced by the RM-3973250 / PR 13
  Stage-3 review.)
- [ ] RM-8b79899 — **GWAS loader populates `effect_size_unit` + `ancestry`** (both hardcoded NULL today, `annotate/loaders/gwas_catalog.py:639-648`): parse the OR/BETA unit hint from the `95% CI (TEXT)` free text + consume the GWAS Catalog ancestry TSV. No schema change. (audit U1; runbooks/annotations.md:774)
- [ ] RM-bfe6ffb — **Load dbSNP withdrawals (`SNPHistory.bcp.gz`, `alias_type='withdrawn'`) + splits into `variant_aliases`** (only `merged` rows loaded today; schema already has `alias_type`). (finding-019; U8)
- [ ] RM-54d396f — **Add `--jobs` per-chromosome parallelism to the dbSNP refresh** (gnomAD-only today; dbSNP rejects `--jobs`, `annotate/cli.py:134-138`). (U20)

#### Deferred loader items (gated on a future signal)

- [ ] RM-4f5df57 — Cross-source generalization of the version-pointer pattern (finding-010 #15)
- [ ] RM-25072d2 — Generalize the hash-match fallback into a shared helper
- [ ] RM-fd3f213 — Sidecar write/read atomicity hardening (finding-043 follow-up): temp-file+atomic-rename (or unlink-before-write) for the version sidecar so a swallowed write failure degrades to ABSENT not STALE; narrow `_read_version_sidecar` to FileNotFoundError + warn on other OSError. Adversarial-only today (single-user 0700/0600 cache); on-theme label-correctness hardening.
- [ ] RM-b2a34d9 — Hash-as-canonical-identity refactor
- [ ] RM-597e9fc — `annotate inspect --source URL` schema-inspection helper

#### Development process & tooling

Process/tooling sub-projects built during the post-5.7 cleanup window to ship the cleanup safely
(orthogonal to Phase-6 entry — none gates the analyses).

- [x] RM-d93d904 **Sub Project C1 — Phase 2 (calibration enablement, [`finding-040`](docs/findings/finding-040-cross-run-learning-calibration.md)).**
  C1 shipped report-only (`auto_tuning_enabled=false`, ratchet dark). The enablement flip
  (`auto_tuning_enabled=true`) is gated on the loop-closure test, a VSC-User `tier_in_hindsight`
  decision, and three **pre-enablement must-fixes** in the dark `apply-parked` / `ratchet --apply`
  write path (finding-040 "Pre-enablement residuals"): (1) the stale full-snapshot apply can
  silently lose a concurrent auto-commit's knob move; (2) an approved parked row is never retired,
  so it stays re-appliable (duplicate `CommitPlan` → empty commit); (3) `apply-parked` does not
  read the kill switch — an open design decision (is one-click human approval exempt from
  `auto_tuning_enabled=false`?) — plus the deferred test coverage for all three. Also deferred
  here: the dispatcher/splitter `est_risk_tier` convergence PR (the splitter stays advisory until
  then) and the unattended every-N-merges close-hook auto-commit (on-demand `/calibrate` is first).
  **Done (2026-06-28):** PR 1 (#124, `DEC-0123`, still dark) landed the three must-fixes + the
  deterministic loop-closure test + the HONOR kill-switch policy; PR 2 (#125, `DEC-0124`) flipped the live
  `risk_weights.json` to `auto_tuning_enabled=true` / `rw-2` (live-file-only insert-then-flip
  supersession — `SEED_RISK_WEIGHTS` stays the immutable `rw-1`/dark reconciliation + back-test +
  kill-switch baseline) with a reversibility falsifier. The two further-deferred follow-ons (the
  dispatcher/splitter `est_risk_tier` convergence PR; the unattended every-N-merges close-hook
  auto-commit) **remain open**.
- [ ] RM-776b1b7 — **finding-040 lower-severity Stage-3 calibration nits**: `per_knob_tally` all-zero-breakdown drop, reference-table doc-drift, test-adequacy smoke tests (NO_OP/tie-break/`_bump_version`/empty-tally). (finding-040; U23)
- [x] RM-7df853f **Sub Project B2 — Phase 2 (`genome.campaign`, [`finding-041`](docs/findings/finding-041-campaign-orchestrator.md)).**
  The campaign runner that auto-runs split sub-scopes through the per-scope team (each transition
  an insert-then-flip supersession). B2 Phase 1 ([`finding-039`](docs/findings/finding-039-scope-split-smart-cut.md))
  shipped the smart-cut detector only.
  **PR 1** shipped the DB-free core + advisory CLI (`DEC-0120`: the `CampaignStatus` state machine,
  the supersession ledger, adaptive re-validation, append-only persistence, ROADMAP reflection, and
  the `genome campaign` CLI — no live launch); **PR 2** shipped the live launch (`DEC-0121`): the
  human-gate-event-recording `revalidate` / `approve-plan` / `record-merge` / `show` commands plus
  the new `/campaign-run` model-driven conductor (`DEC-0099`-aligned; engine-primary deferred to
  C2+D Phase 2), recording each human-gate event onto the ledger.
- [x] RM-88bafb3 (B2-Phase1) — `genome.scope_split` smart-cut detector + `scope-split` sub-app
  (check / dry-run / write-roadmap), the Stage-0.5 split-check micro-gate hook, and the
  managed ROADMAP block below. DB-free core; placeholder sub-scope ids only.

The block between the sentinels is **managed by `genome scope-split write-roadmap`** — do
not hand-edit it; the writer replaces only the inter-sentinel region (append-only).

<!-- B2-SUBSCOPES:BEGIN -->
<!-- B2-SUBSCOPES:END -->

- [ ] RM-1f8e235 — **scope_split cut-policy upgrades**: LSP coupling adapter / `weakly_connected_components` fusion-wiring / recursive re-split (deferred-supersession options; wcc primitive built-but-unused). (finding-039; sub-project-B2-phase1-deferred-followups.md; U22)
- [ ] RM-77c3fd4 — **`genome.campaign` type-tightening nits**: `apply_revalidation` overloads/discriminated-union, `from_json resplit_depth` validation, `SubScopeStateJSON` Literal/bounds (cosmetic on the frozen core). (finding-041; U24)
- [x] RM-acf6880 (Phase 1) (PR #109 / `866d255`) — port
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
- [x] RM-82a7546 (Phase 2) (PR #121 + #122 + #123) — the engine-primary CLI (`genome workflows`) + the
  DB-free, fail-closed **reversal-gate** (`genome workflows check`: seam-drift + schema-validity),
  **closing Sub Project C2+D** (finding-034 Phase-2 amendments / `DEC-0122`). **PR 1 (#121)** fixed
  the latent StructuredOutput 400 — all 21 `SCHEMAS` entries are now valid JSON Schema
  (`type:'object'`), restoring the team workflows on the real engine. **PR 2 (#122)** added the
  `genome workflows` CLI + the reversal-gate (mirrors `genome docs check`; own
  `model`/`seam`/`schemas`/`validator`/`cli`, DB-free + config-free), the `// agent-seam:start`/
  `:end` sentinels, the un-skipped `drift.test.mjs` (harness 0-skip), and the `workflows-gate` CI
  workflow. **PR 3 (#123)** closed the two Phase-1 residuals: **D7** — a live-engine probe
  ([`c2d-d7-probe-wf_f3e8d649-a1a.js`](docs/findings/c2d-d7-probe-wf_f3e8d649-a1a.js), run
  `wf_f3e8d649-a1a`) ran all four trigger-gated Stage-2 writers through `parallel()` on the real
  engine (all resolved, schema-validated) — and **arch-1** — exhaustive harness
  `parallel`/`pipeline` fan-out coverage (`harness-fanout-semantics.test.mjs`; harness 95 pass ·
  0 skip). The superseded maximalist migration plan
  (`docs/plans/sub-project-C2-D-workflow-engine-migration.md`, §4 "migrate A/B/B2/C1" descoped,
  **not** re-proposed) is pruned. JS-orchestration + a new DB-free Python gate + docs only — no
  Python schema / `ddl` / DB change; `manifest.applicable_anchors` `[]`. Gate recipe:
  verification.md "C2+D Phase 2 gate (reversal-gate + engine-primary CLI)".
- [ ] RM-877424d — finding-034 deferred residuals (agent-team follow-ups; neither built):
  - Candidate cross-examination mode — escalation-only Stage-1 pattern for hard-diverging
    Tier-2. (finding-034; U25; ex-RM-96b0a6d)
  - Verify `isolation:'worktree'` live-writer semantics (close.js:93 et al.) — confirm vs the
    accepted residual risk; possibly unverified post-D7. (finding-034; U26; ex-RM-914c4db)
- [ ] RM-74c3386 — Gate-1 fail-closed **token core** for `genome.campaign` `approve-plan` — a typed-token authorization mirroring Sub Project A's `verify_gate` `merge` token; the shipped `--approved` flag already suffices (the reducer refuses any GATE_CROSSING absent `external_event`), so this is future hardening, not a correctness gap. Deferred as optional hardening: the gating Sub Project C2+D Phase 2 has since fired (closed 2026-06-28, `RM-82a7546`), so this is no longer gated on an unfired signal — the shipped `--approved` mechanism suffices. ([`finding-041`](docs/findings/finding-041-campaign-orchestrator.md) "Gate-1 authorization — as taken" / `DEC-0121`)
- [ ] RM-2e4acd3 — Engine-primary `/campaign-run` conductor — the shipped conductor is **model-driven** (`DEC-0099`); the engine-primary launch path is deferred as optional enhancement — the gating Sub Project C2+D Phase 2 has since fired (closed 2026-06-28, `RM-82a7546`), so no longer gated on an unfired signal; the shipped model-driven conductor suffices. ([`finding-041`](docs/findings/finding-041-campaign-orchestrator.md) D6 / `DEC-0121`)
- [x] RM-9dc7915 — **`genome roadmap check` fail-closed gate** (PR B of this effort): validate RM-id format + uniqueness + findings↔ROADMAP referential integrity; DB-free; + `roadmap-gate` CI. (finding-042 / DEC-0125)
- [ ] RM-1552e6a — **verify-gate `change_class` re-derivation hardening**: compare the declared `change_class` against `git diff --name-only` so a mis-declaration isn't trusted (caught only by human token review today). (finding-037; sub-project-A-deferred-followups.md lone unchecked item; U2)
- [ ] RM-a26ae82 — [operator] Enable both CI gate workflows as required status checks on `main`
  (branch protection; advisory until toggled — repo-admin action, not code):
  - `docs-check` Action. (verification.md:88-90; docs-gate-enforcement.md; U5; ex-RM-eb81b6b)
  - `workflows-gate` workflow (the C2+D reversal-gate). (verification.md:505-507; U6; ex-RM-e150116)
- [ ] RM-eda68be — **Markdown↔DDL parity CI check / re-extraction tool**: verify each schema-doc fenced SQL block matches `ddl/*.sql` (the `docs check` gate validates the ledger/frontmatter, not schema↔DDL parity). (finding-010 #16; U21)
- [ ] RM-a128da3 — **Teach `scope_split` `roadmap_writer`/`formatter` to mint `RM-` ids for auto-written sub-scope slots** so the managed `<!-- B2-SUBSCOPES -->` region can later drop its gate exemption. (dogfood)
- [ ] RM-035c394 — **`implement-review.js` implementer must run the dev-loop `pytest` in the FOREGROUND (a `Bash` call with a timeout), not as a background task + `sleep`-poll on its output file** — the Stage-2 wedge surfaced on RM-76ec5db / PR 8: the implementer backgrounded `pytest`, then polled a never-filled (0-byte) output file and hung ~6.5 h; the segment never advanced past the implementer (≈85k tokens, no green-keeper / no review fan-out). Fix the dev-loop invocation in the implementer agent / workflow so a slow suite cannot wedge the run — run pytest foreground with a generous timeout, or rely on the harness's background-completion notification, never a manual file poll. (finding-034; surfaced by `/scope-run RM-76ec5db`)

#### Documentation hygiene

- [ ] RM-f53aa75 — **Prune 3 implemented-but-unpruned plan docs** (`decision-tracking-followups.md`, `docs-gate-enforcement.md`, `PP6-PR7-gnomad-orphan-version-cleanup.md`) whose status banners are stale (shipped/closed). (audit NOTES)
- [x] RM-a63d67a — **Refresh `verification.md` L462-466** — stale Phase-1 D7/arch-1 "open residual" narration closed by PR #123. (audit NOTES)
- [x] RM-c994ce4 — **Roll up `CHANGELOG.md [Unreleased]`** (~2076 lines) into a versioned release section per the CLAUDE.md convention (Phases 1-5 complete). (audit NOTES)
- [x] RM-66f4c75 — **Refresh README "Status"** (says "PR 7 next"; PR 7 closed-as-moot). (audit NOTES)
- [x] RM-4484526 — **Fix CLAUDE.md obs #6 stale line** ("strand-flip collapse deferred to PR 5" — shipped #73). (audit NOTES)
- [x] RM-80af453 — **Refresh ROADMAP header status lines** — ROADMAP.md L5 + L93-94 still read "PR 8 is next" (PR 8 merged #131; PR 9 then merged #133). Update to "PRs 1–9 landed; PR 10 next"; batch with the README L235 fix (RM-66f4c75). (fast-follow / repo-sweep 2026-06-30)
- [x] RM-b8470f2 — **MEMORY.md per-PR DEC-row backfill, PRs #114–#133** — the missing tactical per-PR DEC rows (the ledger footer still reads "complete: PRs #19…#113"). Append the next free contiguous range — **DEC-0128…** onward (DEC-0126/DEC-0127 are taken by RM-12873bf / PR 9's design-decision rows; #133's per-PR row backfills alongside #114–#131) — with the squash subject as the decision text + bump the footer. (fast-follow / repo-sweep 2026-06-30)
- [x] RM-96830ba — **PR A — ROADMAP restructure**: frozen `RM-` ids on every line item (PR-N kept as alias) + the 22 audit items + `finding-042` + `DEC-0125`. (#126)
- [x] RM-1a55a3a — **PR B — `genome roadmap check` gate** (see Tooling item `roadmap-check-gate`). (#127)
- [x] RM-527258f — **PR C — capture-forward convention**: CLAUDE.md SoT rule + skill/agent updates routing new work into ROADMAP. (#128)

## Phase 6 — Analysis pipelines

**Status:** not started — entry gate cleared. The minimal `genes` seed (#88) unblocked the five
`derived_*` / `pathway_genes` FKs (CLAUDE.md "Real-data observations" #7); PRs 4 (tier-2 rsID, #70)
and 5 (chrX M3-physical, #74) had already landed. Remaining entry conditions are the locked
conventions: supersession-over-update, operation-level provenance without schema changes, and the
PyArrow / INSERT-SELECT bulk-load pattern.

### Prerequisites

The pre-Phase-6 sequence — a run that cleared every dbSNP-dependent backfill and FK blocker so
the analyses start with no open deferred items. PRs 1–6 landed (#63, #64, #65, #70, #74, #88);
PR 7 closed-as-moot (2026-06-26 — the live DB has no FK-safe gnomAD orphan).

- [x] RM-13cd016 (PR 1) — Pre-Phase-6 cleanup (docs + operational): off-by-one phase-number
  docstrings, the `annotations.md` "after a schema rebuild" reload sequence (gnomAD/
  dbSNP/refresh-index steps were missing), a hard-fail BGZF-EOF ingest guard
  (finding-008), and a `verify.sh` TMPDIR prelude. Docs/ops only. (#63)
- [x] RM-5a32d13 (PR 2) — `variant_aliases` population from dbSNP `RsMergeArch` via
  `genome annotate refresh-aliases` (finding-019). Fills the table the 5.6 loader left
  empty (finding-016 #8); attaches to the current dbSNP `source_version_id` (no pointer
  flip). The data dependency for PR 4. (#64)
- [x] RM-8efb0b3 (PR 3) — Canonical REF/ALT backfill + hom-only recovery + tier-3 consensus
  align (finding-020). `genome annotate canonicalize-variants` re-orients the
  alphabetical-ordering swap victims, recovers hom-only `ref==alt` rows from dbSNP,
  collapses same-canonical-key siblings, and repoints `genotype_calls` FKs; companion
  `genome annotate align-tier3-consensus` runs after `merge`. Closes finding-005 #1
  (ordering aspect) and #6. Deliberate concordance re-lock to 0.999776 (finding-018
  anticipated this; not a regression). The strand-flip `variants_master` collapse is
  deferred as its own separately-tracked item (finding-005 #1 / finding-020 "Out of
  scope"), distinct from PR 5's two halves. (#65)
- [x] RM-34cb101 (PR 4) — Tier-2 rsID matching in `refresh-index`, consuming the `variant_aliases`
  map from PR 2 (finding-005 #4). Both user-side and source-side rsIDs canonicalize
  through the dbSNP alias map; real-data lift `gwas_matches` 66,701→66,764 /
  `pharmgkb_matches` 1,737→1,738, coord-keyed counts unchanged (finding-025). (#70)
- [x] RM-7e5dccf (PR 5) — chrX resolution + same-SNP duplicate collapse (two independent halves)
  - [x] **5b-pre** + **5b** — `consensus_v1` chip-no-call fix + `collapse-duplicate-variants`
    (≈684 duplicates across five mechanisms reconciled; finding-005 #1 closed;
    findings 026/027/028). Merged; new anchor variants_master/consensus 3,088,233.
  - [x] **5a** — chrX resolution via M3-physical region split (PR #74; sex-aware
    PAR1/non-PAR/PAR2 physical panel subsets; findings 029/031/033; closes
    finding-008). Supersedes the original Option-B framing.
- [x] RM-8094752 (PR 6) — Minimal `genes` seed, Option A: the gene-symbol union of the
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
- [ ] RM-f098bb5 — Deferred schema changes, applied together in one DB rebuild (each needs a
  schema-doc edit + `ddl` re-extract + `rm -rf data/ && genome init`; do NOT execute
  opportunistically):
  - Add a real `genotype_calls.dosage_confidence` column (NOT NULL-defaulted; DR² and
    dosage-confidence cleanly separated) to replace the `imputation_r2` + `quality_flags`
    overload for male non-PAR chrX. (finding-031; CHANGELOG [Unreleased]; U16; ex-RM-ea6c510)
  - Drop `variant_id_seq` in favor of a `MAX`-based allocator (as the annotation tables use),
    removing the fragile `_resync_variant_id_sequence` dance that caused the finding-029
    off-by-one. (finding-020 §2; U17; ex-RM-0bb9b37)
  - Expand `ingestion_status_enum` so imputation roundtrip sub-stages get first-class status
    values instead of being squeezed into 4 values + metadata. (imputation/runs.py:6-10; U18;
    ex-RM-7b3123e)
- [ ] RM-85121ee — **Multi-valued `mapped_trait_uri` (`VARCHAR[]`)** so GWAS rows with multiple comma-separated EFO URIs stop truncating to URI#1 (today single-valued VARCHAR; loader keeps URI#1 + counts `truncated_mapped_trait_uri`). Schema-doc edit + `ddl` re-extract + `rm -rf data/ && genome init`; do NOT execute opportunistically. (finding-005 #11)

### Deliverables

- [ ] RM-2ec2f39 — Load `pgs_score_weights` (per-variant PGS weights, overlapping-only per locked decision #5) → PRS computation against PGS Catalog
- [ ] RM-9dc6228 — PharmCAT integration → `derived_pgx_phenotypes`
- [ ] RM-1ba7d2b — Carrier detection rules
- [ ] RM-d55477a — ACMG SF detection — first task: populate `variants_master.is_acmg_sf` from the curated ACMG SF v3.x gene list intersected with ClinVar rows (finding-005 #5), which unblocks Phase 3's deferred ACMG SF severity escalation
- [ ] RM-5a86ff0 — HIBAG → `derived_hla_typing`
- [ ] RM-de8897c — VEP local runner against user variants → populates VEP columns in `variant_annotations_index` via the rollup refresh.
- [ ] RM-6551070 — ROH via plink2
- [ ] RM-972cd4f — Y/mtDNA haplogroup assignment
- [ ] RM-b53bac2 — Global ancestry (RFMix or admixture)
- [ ] RM-c7b30fd — ROH summary, genome QC — including a profile-level QC rollup that combines per-source `sample_qc` rows into a single per-profile answer, resolving CLAUDE.md "Real-data observations" #1 (finding-005 #2)
- [ ] RM-424ebf3 — Each writes an `analysis_runs` row capturing source versions used
- [ ] RM-6a3c47c — CLI: `genome analyze [pgs|pgx|carrier|acmg|hla|roh|haplogroup|ancestry|qc|all]`

### Follow-ups

- [ ] RM-d461a63 — **`het_outlier` QC threshold calibration across sources** (source-aware / wide-tolerance: 23andMe ~0.17, Ancestry ~0.34, post-imputation different again). (finding-005 #3; U3)
- [ ] RM-f8797e6 — **`is_curated` CPIC coverage via a gene→variant mapping** (conditional; gated on the mapping Phase 6/7 builds). (finding-018; U10)
- [ ] RM-479b818 — **Per-alt hom-ref surfacing in `variant_annotations_index`** (conditional on a UI consumer). (finding-020; U11)

Gated on `pgs_score_weights` landing:

- [ ] RM-e1ccb4a — gnomAD PGS coverage extension — append PGS-component variants to the active gnomAD source-version (append, not refresh; no version bump). See [`finding-011`](docs/findings/finding-011-gnomad-three-way-intersection.md). **Moot while the gnomAD filter is `user_only`** (adopted [`finding-035`](docs/findings/finding-035-gnomad-filter-set-consumer-audit.md), 2026-06-21): the extension would load gnomAD AF at PGS-component positions the user doesn't carry, which — like the ClinVar/GWAS legs finding-035 audited — nothing reads. Revival requires restoring `three_way`.
- [ ] RM-58a194d — dbSNP PGS leg — extend the `user_only` dbSNP filter to PGS-component positions, mirroring the gnomAD extension. See [`finding-016`](docs/findings/finding-016-dbsnp-user-only-filter.md).

**Verification:** each pipeline produces non-zero output on the merged+imputed dataset; supersession works on re-run.

## Phase 7 — Insight generation

### Deliverables

- [ ] RM-d86d4fc — Genes / traits / pathways dictionary tables (full) — primarily serve insight generation and rendering. The loaders we ship in Phase 5 carry gene symbols and trait IDs inline, so the index does not need the dictionaries to do its joins. (The minimal FK-satisfying genes seed — gene symbols only, enough to unblock the five NOT NULL genes FKs — landed earlier as PR 6 in the pre-Phase-6 sequence; only the full genes / traits / pathways dictionaries with descriptions and rendering metadata remain, here in their home phase.)
- [ ] RM-9c15e0f — Per-analysis-type insight generators in `genome.insights.*`
- [ ] RM-3d8bfd1 — Versioned tier mapping functions
- [ ] RM-16e06ae — Confidence rollup
- [ ] RM-0184d08 — Materialized `summary_dashboard` refresh job
- [ ] RM-cc5f624 — Audience rendering (eli5/layperson/clinical) lazily generated
- [ ] RM-24ec28e — CLI: `genome insights regenerate [--type ...]`

### Follow-ups

- [ ] RM-dcd024c — **Wire MyVariant.info / PubMed external enrichment** through the audited client (the `pubmed_enrichment_enabled` config knob has no consumer today; `external_client.py:16`). (U9)

**Verification:** an end-to-end run produces insights for every analysis type; every insight has at least one evidence row; tier rollup is consistent.

## Phase 8 — Backend API

### Deliverables

- [ ] RM-ebc9ec2 — FastAPI app under `genome.api`
- [ ] RM-b8a0652 — Endpoints: summary dashboard, drill-downs (gene / pathway / trait / variant), discrepancy view, PGx medication checker, ACMG SF dashboard, snapshot list, audit dashboard
- [ ] RM-5754c2a — Natural-language query endpoint (Claude tool-use loop over the schemas)
- [ ] RM-843cc66 — Job worker process (`genome jobs run-worker`)
- [ ] RM-b625045 — Audit log middleware on every request

**Verification:** OpenAPI spec covers all groups; integration tests exercise the worker; NL query produces correct DuckDB queries on fixture questions.

## Phase 9 — Frontend

### Deliverables

- [ ] RM-9fac94e — Next.js scaffold
- [ ] RM-3ff08e1 — Home dashboard (the rollup)
- [ ] RM-05401ec — Gene drill-down
- [ ] RM-bfe147a — Trait drill-down with Manhattan plot
- [ ] RM-2362b9d — Variant detail page
- [ ] RM-ff8f424 — Discrepancy view
- [ ] RM-9006c10 — Karyogram (D3) with notable variants
- [ ] RM-25cc4a2 — Chronotype/nutrition/PGx pages
- [ ] RM-85cc899 — Chat/query interface
- [ ] RM-1265f38 — Doctor-ready PDF export

**Verification:** clickable end-to-end demo from dashboard to SNP detail to evidence citations.

## Phase 10 — Privacy hardening, polish, snapshots

### Deliverables

- [ ] RM-6725336 — External call audit dashboard
- [ ] RM-4472a63 — Sanitized export modes
- [ ] RM-c99f219 — Snapshot create / restore / diff (the "what changed" feed)
- [ ] RM-b21db32 — ClinVar-update notifications
- [ ] RM-15b1d77 — Performance pass on `variant_annotations_index` refresh
- [ ] RM-6bc1295 — Optional: `age`-encrypted backup script

### Follow-ups

- [ ] RM-0a34b89 — **External-calls Option C: drop the now-decorative `Settings.external_calls_enabled` `.env` field** (Option A only stopped the misreport; root-cause cleanup so the two-store divergence can't recur). (finding-024; U19)

**Verification:** privacy dashboard accurate; snapshot restore reproduces a prior state; backup script roundtrips.

## Out of scope for v1

- [ ] RM-905df66 — Multi-profile UI (schema is ready; UI deferred)
- [ ] RM-66faca4 — Whole-genome sequencing input
- [ ] RM-231e336 — Drug-drug interaction modeling (DrugBank)
- [ ] RM-dc1dc81 — Cloud sync / sharing
- [ ] RM-fa34137 — Mobile native app

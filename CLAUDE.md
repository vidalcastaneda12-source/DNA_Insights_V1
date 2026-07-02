# Project Context — DNA Insights App

## What this is
A local-first personal DNA insights application that ingests 23andMe + Ancestry raw exports, merges + imputes them via TopMed, joins against curated reference annotations, runs analytical pipelines, and surfaces a unified insights model.

## Read this before any work
- The five schema documents in `docs/schemas/` are the source of truth for data design. Read the relevant one(s) before touching any DB-adjacent code.
- `ROADMAP.md` defines build phases **and is the single source of truth for all scope**: every trackable line item carries a frozen `RM-<7 hex>` id, enforced by the `genome roadmap check` gate (finding-042 / `DEC-0125`). Stay within the current phase unless explicitly directed otherwise.
- `MEMORY.md` (repo root) is the decision ledger — every architectural/tactical decision as a `DEC-NNNN` row, and findings carry machine-readable frontmatter. The `genome docs check` gate enforces it (finding-036). Not to be confused with Claude Code's session auto-memory.
- This file (`CLAUDE.md`) is the persistent context for every session.

## Working with this codebase

This codebase is built collaboratively across five actors. The boundaries between them are part of the convention.

- **ClaudeCodeVerification** — Claude Code. the pre and post implementation chat. Plan review, handoff review. Does not touch the codebase or run commands.
- **ClaudeCodeTestingBugs** — Claude Code. the post implementation chat. Test-results review and test assistance. Does not touch the codebase or run commands.
- **ClaudeCodePlanning** — Claude Code in plan mode — toggled locally via Shift+Tab, or invoked in the cloud via /ultraplan for inline-comment review. Reads the repo, produces a technical plan, surfaces questions. Does not write code or run commands.
- **ClaudeCodeDevelopment** — Claude Code. Writes code, runs the dev-loop tests, commits, pushes, opens PRs, produces an end-of-session handoff via `/handoff`.
- **VSC-User** — the human operator. Approves plans, runs the formal verification protocol (`docs/runbooks/verification.md`), merges (or gives the typed approval that drives the owner-approved evidence-gated merge — Sub Project A, `finding-037`).

Older docs and CHANGELOG entries use **VSC-Claude** as a single name. That maps to VSC-ClaudeCodeDevelopment (implementation mode) by default.

### Plan mode first

For any non-trivial change — anything that touches multiple files, modifies behavior, alters the schema, or adds a dependency — Claude Code starts in plan mode. Implementation does not begin until VSC-User has approved the plan.

When in plan mode, read the listed inputs first — `CLAUDE.md`, `ROADMAP.md`, relevant `docs/findings/`, and the relevant `backend/src/genome/` subdirectory — then produce a plan containing:

1. **Reading list confirmation** — the docs and code files that were read.
2. **Problem statement** — what's wrong or missing. Specific numbers, error messages, or symptoms.
3. **Constraints** — locked decisions respected, schema files that won't change without re-extraction, code that won't be refactored opportunistically.
4. **Implementation plan** — numbered tasks.
5. **Tests** — new tests to add, existing tests that must still pass.
6. **Verification** — how to confirm success. Test counts, lint/type clean, expected real-data outputs.
7. **Out-of-scope** — explicit list. Phase boundaries, optional features, things to defer.
8. **End-of-session handoff** — `/handoff` at session end.

If plan mode surfaces a question that needs judgment outside the code (roadmap-level trade-offs, architectural fit, alignment with locked decisions), pause and ask VSC-User. VSC-User routes the question to ClaudeCodePlanning and returns with an answer.

### Implementation contract

Once VSC-User approves the plan, ClaudeCodeDevelopment executes it. The expectation is mechanical execution — surprises at this stage usually mean the plan missed something, and the right move is to pause and escalate rather than improvise.

Every implementation session produces:
- A new branch from `main`.
- A clean dev-loop (`pytest`, `ruff check`, `ruff format --check`, `mypy --strict backend/src`).
- A commit + push.
- An open PR, or a new commit to an existing PR where appropriate.
- An end-of-session handoff via `/handoff` (`.claude/commands/handoff.md`).

VSC-User runs the canonical verification independently against the pushed branch — see `docs/runbooks/verification.md`. That independence is the gate that catches selective test runs, test mutation, and number-interpretation slippage. The independent human run is always available and is the standing fallback. As of Sub Project A (`finding-037`) there is also an owner-approved **evidence-gated** path: Claude runs the same protocol through the fail-closed `genome.verify_gate` core, presents the raw evidence, takes a typed approval, then squash-merges (`.claude/commands/verify-and-merge.md`). The fail-closed core is what preserves the guarantee there — an undecidable or stale signal reduces to `UNKNOWN`/`BLOCKED`, never a fabricated pass.

## Architecture — locked decisions

1. Two databases: `genome.duckdb` (DuckDB analytical) and `app.db` (SQLite + SQLCipher, encrypted).
2. Coordinates: GRCh38 primary, GRCh37 stored alongside. `variant_id` is `BIGINT` from a sequence.
3. Multi-allelic variants split into biallelic rows.
4. Imputed variants share `variants_master`; imputation status is on `genotype_calls`.
5. PGS weights are overlapping-only.
6. Encryption: OS FDE + SQLCipher on `app.db`. The DuckDB file is not encrypted; rely on filesystem perms (0600) and FDE.
7. Supersession over update. Readers never see a torn state — at any moment the user-visible "current" set for a given source is entirely the old release or entirely the new release, never a mix. Two mechanisms, chosen by supersession grain. **Source-grain** (an entire dataset replaces the prior dataset — the Phase-5 annotation tables: ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog) uses the **version-pointer pattern**: a single-row pointer in `annotation_sources` names the current `source_version_id`; a refresh INSERTs the new set under a fresh `source_version_id`, then UPSERTs the pointer. The atomicity guarantee is a one-row write, not a mass UPDATE. **Row-grain** (an individual row gets re-derived — `genotype_calls`, aspirational `insights` / `evidence` / `derived_*`) keeps per-row `is_active` + `superseded_by`: re-runs INSERT-then-deactivate, wrapped in one transaction with the producing INSERT. Either way, never UPDATE active content. See finding-010 and `schema_group_2_reference_annotations.md` for the version-pointer rationale.
8. Provenance everywhere. Every annotation, derived row, and insight names its source/method version.
9. Local-first privacy. External calls require `external_calls_enabled = true`. Every external call is audit-logged with endpoint + payload hash (not payload).

## Tech stack — locked

- Python 3.12+, DuckDB, SQLite via pysqlcipher3, FastAPI, Typer, Anthropic SDK
- Variant tooling: bcftools, plink2, cyvcf2; PharmCAT (PGx); HIBAG (HLA)
- Frontend: Next.js + React + TypeScript + Tailwind + shadcn/ui; Recharts + D3
- Tests: pytest; lint: ruff; types: mypy strict

## Conventions

- All cross-DB references are application-validated, not enforced by FK.
- Every insight points to one or more evidence rows. Never write an insight with zero evidence.
- The unified evidence-tier scale is `1A | 1B | 2A | 2B | 3 | 4`. Source-specific grades are mapped via versioned functions in `genome.insights.tier_mapping`.
- Insight `confidence_score` is computed from evidence; never set by hand.
- All long-running tasks go through the jobs table — never run them inline in API handlers.
- All external calls go through a single audited HTTP client (`genome.privacy.external_client`).
- Logging: structlog with JSON output. No `print()`.
- Style: ruff defaults plus `--select=ALL --ignore=D,ANN101,ANN102`. Type-annotate everything.
- 23andMe and Ancestry exports may include variants on non-canonical GRCh38 contigs (alt, random, unplaced, decoy). These are filtered at parse time and counted in `ingestion_runs.variants_dropped_non_canonical`. Only canonical chromosomes (1-22, X, Y, MT) are stored. This matches standard clinical bioinformatics practice.
- Lift-over uses the `liftover` Python package (CFFI-backed, fast) by default. The `Liftover` Protocol abstracts engine selection; alternatives include `IdentityLiftover` for native GRCh38, `BcftoolsLiftover` for setups with a working bcftools `+liftover` plugin, and `PyLiftoverWrapper` as a pure-Python fallback. Engine selection happens through `make_liftover(..., engine='auto'|'liftover'|'pyliftover')`; `auto` prefers the `liftover` package and logs a loud INFO when it falls back to `pyliftover`.
- Lift-over can produce non-canonical contigs — a canonical GRCh37 coordinate may map to e.g. `4_GL000008v2_random` on GRCh38. The normalize step re-runs `normalize_chrom` on the post-lift chromosome, drops the row when the result is `None`, and counts it in `ingestion_runs.variants_dropped_lift_to_non_canonical`. The same positive-rule filter is applied at both parse time and normalize time, so the writer's `chromosome_enum` cast never sees a non-canonical label regardless of which engine produced it.
- Every PR that changes behavior, schema, dependencies, or build steps should add an entry to `CHANGELOG.md` under the `[Unreleased]` section. The entry should be one or two sentences describing what changed and why, with a PR reference. Roll up `[Unreleased]` into a versioned release section when phase milestones land.
- **ROADMAP.md is the single source of truth for scope — capture-forward.** When new deferred or incomplete work is identified — in a finding, a code review, an audit, a `/handoff`, or a code comment — capture it in `ROADMAP.md` as a checklist line item with a fresh `RM-<7 hex>` id (`RM-` + first 7 of `sha1(<stable-kebab-slug>)`) in the **same change** that records it. A finding / `MEMORY.md` / `CHANGELOG.md` / runbook may *describe* the work, but must back-reference the `RM-` id — never be its sole record. The `genome roadmap check` gate (finding-042 / `DEC-0125`) fails on a missing id, a duplicate id, or an `RM-` reference that doesn't resolve to a ROADMAP line item. Existing `PR N` sequence labels are retained as a secondary alias. Use the non-hex placeholder `RM-xxxxxxx` in illustrative examples so they aren't mistaken for real ids.
- For bulk loads into DuckDB, use PyArrow Table registration plus `INSERT ... SELECT`, not `executemany`. The latter does not batch-bind and is catastrophically slow at scale.
- Performance target: routine refresh, ingest, and CLI operations should complete in well under one minute (~30 seconds is the target). Long-running operations are explicitly gated behind named subcommands (e.g. Beagle full-genome imputation at ~30 minutes via `genome imputation run`) and must emit per-step structlog progress so the wall-clock window is observable. Routine refresh commands that exceed the target without progress output are out of contract and need either optimization or progress instrumentation.
- **Schema changes require rebuilding local databases.** After pulling any PR that modifies files under `docs/schemas/` or `ddl/`, run:
  ```
  rm -rf data/
  uv run genome init
  ```
  DuckDB enums and table structures don't auto-migrate; existing files stay on the old schema. For workflows that need to preserve ingested data across schema changes, this implies a re-ingest after recreation. With the post-Phase-2 optimized pipeline taking ~16 seconds per file, this is acceptable friction for a personal-use app. A proper migration system would be appropriate if the project ever shifted toward multi-user or production deployment.

## Real-data observations

**23andMe v5 and Ancestry v2 chips have meaningfully different SNP compositions.** Real-data verification exposed two findings worth keeping in mind:

1. **Ancestry v2 does not include Y-chromosome SNPs.** Sex inference from Ancestry data alone returns `ambiguous` for males (correctly, since with no Y data the inference is genuinely undetermined). A profile-level QC rollup that combines per-run inferences across sources should be implemented in Phase 6 (consolidated with the genome-QC analysis pipeline) — until then, the per-source `sex_inferred` field is correct on its own terms but may not be a useful single answer at the profile level.

2. **Heterozygosity rate is chip-dependent.** 23andMe v5 typically lands ~0.17, Ancestry v2 ~0.34 — for the same sample. The two chips target different SNP populations: 23andMe's broader panel includes many common variants where most individuals are homozygous-reference, while Ancestry's panel is curated for ancestry-informative markers with higher MAF and consequently higher heterozygosity. The QC `het_outlier` threshold (if/when introduced) should be calibrated per source or use a wide tolerance that accommodates both ranges. Cross-platform het differences are chip-design signal, not biological signal.

3. **Phase 4 Beagle imputation produces ~2.37M variants at DR² > 0.3 from ~204K polymorphic chip inputs.** Real-data verification (see finding-007) established these durable numbers for the user's 23andMe v5 + Ancestry v2 merged corpus:
   - Input to Beagle: 204,153 polymorphic SNVs across chromosomes 1-22 + X. Hom-only positions are filtered at prepare per finding-005 #6.
   - Imputed output at DR² > 0.3: 2,369,171 variants.
   - Mean DR²: 0.8242. High-quality (DR² > 0.8): 1,592,735 (~67% of imported).
   - chrX imputed variants (M3-physical region split, PR #74 / 5a — historically 0 for males): **92,832** total kept (non-PAR **90,999**, PAR **1,833**), recovered by PR-3 hom-only recovery + the M3-physical region-split imputation ([`finding-029`](docs/findings/finding-029-chrx-imputation-m1.md)). On male non-PAR, Beagle's `INFO/DR2` is structurally dead (single-sample hemizygous → 0 for every marker), so quality is **dosage-confidence** `max(DS,1−DS)` — **87,578** calls ≥0.99 / **3,421** in [0.9,0.99) — plus a 5-fold leave-one-out concordance of **~0.9856** ([`finding-031`](docs/findings/finding-031-chrx-nonpar-dosage-confidence-qc.md)/[`-033`](docs/findings/finding-033-chrx-loo-allele-aware-matching.md)). `male_nonpar_het_anomaly = 1` (one residual chip miscall; ≈0 by construction under M3+R1). These imputation-derived numbers are **tolerance-banded, not exact** — Beagle is multi-threaded / not bit-reproducible (LOO ≈0.985–0.986 across runs: run_0002 0.985550, run_0003 0.985971; non-PAR yield run-to-run ±~100; the PASS bar is the finding-031 ≥95% criterion). Source: live `genome.duckdb` (run_0002) + `archive/imputation/run_0002/loo/REPORT.json`.
   - Full-genome runtime: ~30 min on Linux, 16 threads, 8 GB heap.
   - Post-merge `consensus_genotypes`: 3,210,371 rows (942,620 chip-derived; 2,267,751 imputed-only under the `consensus_v1` Phase 4 extension; the 101,420 chip+imputed overlap variants stay chip-derived with the imputed call appended to `contributing_calls` as confirming evidence).
   - Phase 3 numbers preserved exactly through Phase 4: `both_concordant=120,516`, `disagreement_resolved=106`, `single_source=821,998`, shared-call concordance=1.0000, `strand_flip_resolutions=106`, palindromic shared variants=31.

   These numbers are stable identifiers through Phase 4. Drift in any of them on a re-run against the same input corpus is a regression signal **at the Phase-4 boundary**. They re-lock at the PR-3 canonicalize step (post-5.7 backfill — see observation #6 and finding-020) as the deliberate consequence of exposing previously-hidden cross-chip disagreements; the shared-call concordance specifically **drops from 1.0000** under PR 3 and that drop is a correction, not a regression. See finding-020 "Concordance re-lock" + bedrock anchor table for the post-PR-3 numbers. Post-PR-3 headline re-lock (gate-captured): shared-call concordance **0.999776** (down from 1.0000, driven *entirely* by 27 palindromic `strand_ambiguous` no-calls with `genotype_mismatch`=0; recon A confirmed **correct unification** — 27 distinct palindromic sites — see finding-020 recon A); total chip-derived consensus rows **942,592** (↓ by 28 net = −27 population-A collapse − 1 `align-tier3` deletion; final post-align `disagreement_resolved` 106 → **1**, `consensus_total` 3,210,371 → **3,088,916**).

   Post-chrX re-lock (PR #74 M3-physical chrX, gate-captured run_0002; **exact/deterministic** — these supersede the post-PR-3 figures above as the current boundary, which stay as the pre-chrX negative-control reference): `consensus_total` = `variants_master` total **3,160,364**; `imputed_only` **2,218,539**; `single_source` **821,285**; `both_concordant` **120,513**; `disagreement_resolved` **0**; `unresolvable` **27**. Shared-call concordance is **UNCHANGED** at **0.9997760079641613** (= 120,513/120,540; chrX added no shared-call disagreement — `genotype_mismatch`=0, 27 `strand_ambiguous`). Autosomal negative control byte-identical to pre-chrX: `both_concordant` **115,509** / `single_source` **793,917** / `imputed_only` **2,146,302** / `unresolvable` **26**. Index match counts (`gnomad_matches`, `row_count`, `clinvar_matches`, …) are **locked in PR C** (the post-chrX `user_only` gnomAD reload + `refresh-index`, gate-run 2026-06-22) — see obs #4's post-chrX re-lock.

4. **Phase 5.7 `variant_annotations_index` first build is allele-match-gated, not position-match-gated (see finding-018).** Real-data verification of `genome annotate refresh-index` against the user's loaded corpus (ClinVar `2026_05_17`, gnomAD `4.1.1`, GWAS `2026_05_16`, PharmGKB `2025_07_05`) established these durable numbers:
   - `row_count` = 159,658; wall-clock ~2.2 s (well under the 30 s target).
   - `gnomad_matches` = 101,501, `clinvar_matches` = 2,559, `gwas_matches` = 66,726, `pharmgkb_matches` = 1,737, `curated_count` = 4,198.
   - `is_rare` TRUE = 848, `is_ultrarare` TRUE = 421 (the matched variants are overwhelmingly common chip SNPs).
   - The coord-keyed sources (ClinVar, gnomAD) match far below the position-level overlap because **78.3% of `variants_master` is hom-ref (`ref==alt`, finding-005 #6)** and **~50% of the genuine `ref≠alt` variants match gnomAD only with REF/ALT swapped** — `variants_master` REF/ALT is not yet canonicalized (finding-005 #1). The rsid-keyed sources (GWAS, PharmGKB) are unaffected. This is expected, not a regression; the match rate rises when the post-5.7 canonical-REF/ALT backfill re-runs the index.

   These numbers are stable identifiers for the **pre-PR-3** (pre-canonicalization) `variants_master`. Drift on a re-run against the same corpus + same source versions is a regression signal at that boundary. The PR-3 canonicalize step is the deliberate event that re-locks `gnomad_matches` / `clinvar_matches` upward (hundreds of thousands); `gwas_matches` / `pharmgkb_matches` (rsid-keyed) stay unchanged. See observation #6 and finding-020 bedrock anchor table for the post-PR-3 numbers. Post-PR-3 headline re-lock (gate-captured): `gnomad_matches` **2,796,952** and `clinvar_matches` **61,458**, both up dramatically from 101,501 / 2,559 (`row_count` 159,658 → 2,824,229; `is_rare` 848 → 163,160 / `is_ultrarare` 421 → 103,261 — the imputed corpus carries the rare tail; `is_ultrarare` ⊂ `is_rare` ⊂ `gnomad_matches` holds). `pharmgkb_matches` 1,737 holds, but `gwas_matches` is **66,701** — **not** unchanged: −23 vs the pre-canon swept count (66,724) from collapse-dedup, not the loader cache-skew (finding-020 recon C). NB: the gate's ClinVar/GWAS data is the May `2026_05_17` / `2026_05_19` cache, while the in-DB version row carries a **June** label — the loader label↔data decoupling, [`finding-022`](docs/findings/finding-022-loader-version-label-decoupling.md). **Post-PR-4 re-lock (gate-captured; tier-2 rsID matching, finding-025):** the rsid-keyed legs now resolve merged-away rsIDs through `variant_aliases`, lifting `gwas_matches` **66,701 → 66,764** (+63: 28 direction-1 + 35 direction-2 — GWAS rows carrying stale rsIDs that map to the user's current rsIDs) and `pharmgkb_matches` **1,737 → 1,738** (+1); `row_count` **2,824,229 → 2,824,236** (+7, the only recovered variants not already indexed via another source). The coord-keyed anchors are **unchanged**: `gnomad_matches` 2,796,952 / `clinvar_matches` 61,458 / `is_rare` 163,160 / `is_ultrarare` 103,261 (rsID merges don't touch a coordinate join). Map-integrity gate, both 0 on dbSNP 157: single-hop terminal-survivor + one-survivor-per-alias. The `tier2_rsid_lifts` result field (48 here) is a direction-1 path-fired **sentinel**, not the 64-variant recovered count. `refresh-index` wall-clock ~120 s at this corpus is commit-dominated (2.8M-row rebuild), pre-existing, not a PR-4 regression.

   **Post-chrX `user_only` re-lock (PR C, gate-captured 2026-06-22; exact/deterministic — supersedes the post-PR-4 three-way figures above as the current boundary, which stay as the pre-chrX / three-way reference).** The post-chrX gnomAD reload (`genome annotate refresh --source gnomad --force --jobs 8` at the finding-035 `user_only` filter, now chrX-inclusive: `rows_loaded` **4,568,802**, `match_rate` **0.9957**, `filter_set_composition` `user`=`union_total` **3,144,800**, 23/23 chroms succeeded / 0 failed, 2 htslib recovers [this reopen count is now emitted as the `reopens_total` field per RM-3973250 / finding-012 #12 — a **tolerance-banded** network-weather signal, **not** a byte-exact anchor: finding-012 #5, expected 0–~30 with `0` the healthy floor, record-the-value / never byte-match], `mean_af_user_overlap` **0.2288**, wall-clock ~7 h 14 m at `--jobs 8`) followed by `refresh-index` re-lock the index anchors to `gnomad_matches` **3,054,426** / `clinvar_matches` **61,926** / `gwas_matches` **66,742** / `pharmgkb_matches` **1,737** / `row_count` **3,077,001** / `curated_count` **63,198** / `is_rare` **173,689** / `is_ultrarare` **109,013** (`is_ultrarare` ⊂ `is_rare` ⊂ `gnomad_matches` holds). The entire `gnomad_matches` rise vs the pre-reload `user_only` build (+71,995 over 2,982,431) is **chrX and only chrX**: chrX index `gnomad_matches` **22,640 → 94,635**, chrX `gnomad_frequencies` rows **36,867 → 138,299** — the autosomal legs reloaded byte-identically, so `clinvar_matches` / `gwas_matches` / `pharmgkb_matches` (coord/rsid-keyed) are **unchanged** by the reload (their values here are the first authoritative post-chrX `user_only` capture, superseding the PR-4 three-way figures). Negative control byte-identical (`variants_master` 3,160,364; consensus anchors of obs #3 unchanged) — the reload touched only `gnomad_frequencies` (new `source_version_id`=10, pointer flipped 8→10) + `variant_annotations_index`. Active versions `{clinvar 2026_06_15, gwas_catalog 2026_06_01, gnomad 4.1.1, pharmgkb 2025_07_05, dbsnp 157}`. NB the `user_only` filter is now **3,144,800** distinct positions (not the pre-imputation ~0.94 M the runbook's superseded three-way `user` leg shows — the Phase-4 imputed corpus grew `variants_master`), so the reload is ~61% of the old three-way set, not the ~18% the finding-035 estimate assumed; `--jobs 8` wall-clock ran ~7 h, well past the optimistic 1.5–3 h. Source: live `genome.duckdb` (gnomad `source_version_id`=10) + the gate's `gnomad.refresh.complete` / `index_refresh.complete`.

5. **`variant_aliases` is populated by `genome annotate refresh-aliases`, the first post-5.7 backfill (see finding-019).** It loads NCBI's dbSNP `RsMergeArch.bcp.gz` (legacy rs-merge archive, ~146 MB, frozen 2018/build ~151 — the VCF carries no merge history) filtered to merges touching the user's own rsIDs on either side, mapping `alias_rsid (old) → current_rsid (survivor)` with `alias_type='merged'`. It is **not** a registered `--source` loader; it attaches alias rows under the **current dbSNP `source_version_id`** (the dbsnp source group's two tables share one `annotation_sources` pointer per decision #7 / PR #57) — no new version, no pointer flip, no VCF re-stream. Consequence: **re-run `refresh-aliases` after any future `refresh --source dbsnp`** that flips the dbsnp pointer, or the new epoch carries no aliases. Locked first-run drift identifiers against the user's corpus (dbSNP `157`, `variants_master` 927,964 distinct rsIDs; RsMergeArch 11,963,907 source rows, ~54 s wall-clock): `rows_loaded` = 839,413, `distinct_alias_rsid` = 839,413, `distinct_current_rsid` = 513,573, **`user_old_rsid_hits` = 1,190** (the tier-2-lift proxy — user variants carrying a now-mappable stale rsID), `user_current_rsid_hits` = 512,408. Drift on a re-run against the same corpus + frozen RsMergeArch is a regression signal. Tier-2 rsID matching (finding-005 #4) is the consumer, a later PR.

6. **Canonical REF/ALT backfill + hom-only recovery — the second post-5.7 backfill (see finding-020).** New `genome annotate canonicalize-variants` rewrites `variants_master.(ref_allele, alt_allele)` against the currently-active dbSNP source-version: re-orients the alphabetical-ordering swap victims dominant in finding-018 (~101,918 genuine `ref≠alt` rows whose `(ref,alt)` matched dbSNP only when swapped), recovers hom-only `ref==alt` rows by assigning a real ALT (closing finding-005 #1 ordering aspect and #6 — the imputation-input hom-only drop), and collapses rows whose new canonical key collides with a sibling at the same position (re-pointing `genotype_calls.variant_id` FKs to the survivor; `variant_id` is **not preserved** for movers, see finding-020 §2). The companion `genome annotate align-tier3-consensus` runs after `merge` to delete the non-canonical-side consensus rows for the strand-flipped duplicates that Scope-A canonicalize leaves as two rows. Auto pre-mutation snapshot of `genome.duckdb` to `archive/canonicalize/` with `--no-backup` opt-out. Two-transaction split sidesteps DuckDB's FK-on-DELETE enforcement that doesn't see in-transaction FK re-points. **Triggers the re-lock of observation #3 (merge counts) and #4 (index match counts)** — the shared-call concordance drops from 1.0000 by design, exposing previously-hidden cross-chip disagreements (see finding-020 "Concordance re-lock — correction, not regression"). Runbook sequence: `canonicalize-variants` → `merge` → `align-tier3-consensus` → `refresh-index`. Strand-flip `variants_master` collapse shipped in PR 5b (#73): `collapse-duplicate-variants` reconciled the residual same-SNP duplicates via supersession; finding-005 #1 closed (the original ~106-tier-3 framing was superseded by the post-canon residual measurement, findings 026/027). The first-authoritative-run locked numbers (capture on first real-data run; the bedrock anchor table in finding-020 lists every shifted value with explicit correction-not-regression framing) are the regression signal going forward.

7. **Minimal `genes` seed — the FK-satisfying gene-symbol subset (PR 6 / RM-8094752, ROADMAP Phase 6 → Prerequisites; see [`finding-020`](docs/findings/finding-020-canonical-refalt-backfill.md) "Out of scope" amendment).** New `genome annotate seed-genes` populates the previously-empty `genes` table with the set-union (on `gene_symbol`) of the verified ACMG SF v3.3 panel (84 genes) and the currently-active CPIC + PharmGKB gene symbols, enough to satisfy the five `NOT NULL REFERENCES genes(gene_symbol)` FKs that otherwise block every Phase-6 insert into `derived_pgx_phenotypes`, `derived_carrier_findings`, `derived_acmg_sf_findings`, `derived_compound_het`, and `pathway_genes` (`genes` was never a leaf — five dependents, not zero). One-time static backfill under a fresh `hgnc` `annotation_source_versions` row for provenance; it **deliberately does not flip an `annotation_sources` pointer** (the full `genes`/`traits`/`pathways` dictionaries + HGNC bulk loader remain Phase 7, so there is no "current `genes` version" to point at yet — the version row is provenance-only). Gate-captured durable identifiers against the user's live corpus (Human Gate 2, 2026-06-23; CPIC/PharmGKB symbols read from the active versions `pharmgkb 2025_07_05`, `cpic`-distinct-current 19):
   - New `hgnc` `annotation_source_versions` row: `source_version_id` = **11** (live `MAX(source_version_id)` was 10 pre-seed → now 11), version label `acmg_sf_v3.3+pgx_derived`, `record_count` = 1153.
   - `genes` total rows = **1153** = |84 ACMG ∪ 1086 PGx| (cpic-distinct-current 19, pharmgkb-distinct-NOT-NULL-current 1086, pgx-union 1086, ACMG 84, **ACMG ∩ pgx overlap 17** → 84 + 1086 − 17 = 1153).
   - `is_acmg_sf` count = **84**; `is_pgx_relevant` count = **1086**. Provenance complete: `genes WHERE source_version_id IS NULL` = 0, `genes WHERE retrieval_date IS NULL` = 0, distinct `source_version_id` = {11}. Coverage gate clean: `cpic_covered=True`, `pharmgkb_covered=True` (cpic / pharmgkb EXCEPT-probe both 0 — every active CPIC/PharmGKB symbol is present in the seed).
   - CLI summary (first run): `genes seeded: source_version_id=11 already_populated=False genes_rows=1153 acmg_sf_genes=84 pgx_genes=1086 cpic_covered=True pharmgkb_covered=True`. Idempotent re-run: `already_populated=True`, counts unchanged, still exactly one `hgnc` version row.

   Unlike observations #3–#6, these are **exact / deterministic, not tolerance-banded** — a static curated seed, byte-identical on every run against the same active CPIC/PharmGKB versions (any drift is a regression, never run-to-run noise). **Negative control — byte-unchanged by the seed** (it touches only `genes` + the one new `annotation_source_versions` row, and does **not** run `refresh-index`): `variants_master` = **3,160,364** (obs #3); `annotation_sources` total = **7** (no `hgnc` pointer); gnomad pointer `source_version_id` = **10** (obs #4); obs #4 index counts unchanged (`row_count` 3,077,001 / `gnomad_matches` 3,054,426 / `clinvar_matches` 61,926 / `gwas_matches` 66,742 / `pharmgkb_matches` 1,737 / `is_rare` 173,689 / `is_ultrarare` 109,013). Active source versions at the seed boundary: `clinvar 2026_06_15, gwas_catalog 2026_06_01, gnomad 4.1.1, pharmgkb 2025_07_05, dbsnp 157`, **+ hgnc `acmg_sf_v3.3+pgx_derived` (svid 11)**. This clears the Phase-6 entry FK gate (ROADMAP "Phase 6 entry is gated on"). Verification block: [`verification.md`](docs/runbooks/verification.md) "PR 6 genes seed gate".

8. **General superseded-row purge — `genome annotate purge-superseded` (PR 9 / RM-12873bf, ROADMAP Phase 5 → Follow-ups; [`finding-010`](docs/findings/finding-010-version-pointer-supersession-pattern.md) #14).** The ongoing orphan-row cleanup *procedure* for rows stranded under superseded `source_version_id`s (covers `variant_aliases` orphans too), generalizing PR 7's one-off gnomAD-specific delete. Retention is **keep-1** (active + immediate prior kept per source; finding-010 #14); the command **defaults to dry-run** and mutates only under an explicit `--execute` gated behind a **mandatory read-only pre-execute probe** (the two VSC gate decisions). Gate-captured against the live corpus (PR #133 / `d4a07d6`, 2026-06-30) as a **pure no-op today** — every source's `deletable` set is empty and the dry-run reports `purge.complete executed=false deletable_total=0 orphan_candidates=0`, so a keep-1 `--execute` changes nothing. The no-op is **corpus-conditional, not structural**: the orphan sweep would snapshot-then-delete a zero-data registry orphan if one existed; today `orphan_candidates=0`. Per-source **active** `source_version_id` at the purge boundary — the stable inventory a later run compares against (a changed id means a refresh ran in between): clinvar=**3**, gwas_catalog=**4**, pharmgkb=**1**, cpic=**2**, **gnomad active=10 / prior=8** (the lone source carrying a retained prior — obs #4's chrX reload), dbsnp=**9**, pgs_catalog=**5**. Two fail-closed guards: a **14-FK-child per-column guard** on `annotation_source_versions` — it has **14** FK children, not the **8** in `_SUPERSESSION_TABLES` (`annotation_sources` references it via `current_source_version_id`, the other **13** via `source_version_id`), each counted on its real FK column via `duckdb_constraints()` (kills a post-TX1 BinderException) — plus a **`source_db` dangling-pointer** check (a cross-source `current_source_version_id` is FK-valid yet dangling → `DanglingPointerError`, closing a real active-build hole). **Negative control — unmoved by the purge** (keep-1 deletes nothing today; a dual-polarity keep-0 probe on a *disposable copy* drops the protected prior gnomad `svid8` rows → 0 while the active `svid10` set is unchanged, and `gnomad_matches` is still byte-identical after a `refresh-index` rebuild — so the prior-version rows are genuinely index-unreferenced and keep-1 is pure history margin): the bedrock anchors of obs #3 (`variants_master` **3,160,364**), #4 (`gnomad_matches` **3,054,426**; gnomad `svid8` **4,467,370** / `svid10` **4,568,802**) and #7 (`annotation_sources` total **7**; `genes` svid11 **1153**) all HOLD. These purge-boundary identifiers are **exact / deterministic** (a registry-state inventory, not a tolerance-banded pipeline number) — a non-zero `orphan_candidates` or non-empty `deletable` on a re-run against the same corpus + active source versions is the regression signal. Verification block: [`verification.md`](docs/runbooks/verification.md) "PR 9 purge gate".

## Environment requirements

- **SQLCipher must be built with FTS5.** `app.db` includes a `notes_fts` virtual
  table that uses FTS5. Most distro packages of SQLCipher (e.g. Ubuntu 24.04's
  `libsqlcipher-dev` 4.5.6) ship without FTS5, so `pysqlcipher3` linked against
  them will fail at `genome init` with `no such module: fts5`. Rebuild SQLCipher
  4.5.6 from source with `--enable-fts5` and reinstall `pysqlcipher3` against
  it; the exact build commands live in `README.md` under "Prerequisites".
- **Never "fix" an FTS5 install failure by removing the `notes_fts` virtual table
  (or its triggers) from `docs/schemas/schema_group_5_app_state.md` /
  `ddl/group_5_app_state.sql`.** Note search is a product requirement; if you
  hit `no such module: fts5` the answer is to rebuild SQLCipher with FTS5, not
  to mutilate the schema. Future sessions: heed this. Also relevant: see
  "Things never to do" — schema files are immutable except via deliberate,
  documented schema corrections.

## Common file locations

- DDL: `ddl/*.sql`
- Schema docs: `docs/schemas/`
- Backend code: `backend/src/genome/`
- Tests: `backend/tests/`
- Frontend: `frontend/`
- Runtime data (gitignored): `data/`
- Raw uploads, snapshots, source dumps (gitignored): `archive/`

## How to run

- Setup: `uv sync && cp .env.example .env && $EDITOR .env`
- Initialize: `genome init`
- Tests: `pytest`
- Lint: `ruff check && ruff format --check`
- Types: `mypy --strict backend/src`
- Decision gate: `genome docs check` — validates the `MEMORY.md` ledger + finding frontmatter (CAPTURE / RETRIEVAL / LIFECYCLE). DB-free: runs on a fresh checkout with no SQLCipher built. See finding-036.
- Reversal-gate: `genome workflows check` — validates the three per-scope-team dynamic workflows (`.claude/workflows/{plan-phase,implement-review,close}.js`): seam-drift (the duplicated `agent()`/retry seam stays logically identical under GT-1) + schema-validity (every `SCHEMAS` entry declares `type:'object'`), fail-closed. DB-free. See finding-034 / `DEC-0122`.
- Source-of-truth gate: `genome roadmap check` — validates that every `ROADMAP.md` line item carries a unique `RM-<7 hex>` id and that every `RM-` id cited in findings / `MEMORY.md` / `CHANGELOG.md` resolves to one defined in ROADMAP, fail-closed. DB-free. See finding-042 / `DEC-0125`.
- Dev API (later phases): `uvicorn genome.api.main:app --reload`
- Frontend (later phases): `cd frontend && pnpm dev`

The merge-gate verification protocol — what VSC-User runs independently
of the dev-loop commands above before merging a branch, or what the
owner-approved evidence-gated path runs through `genome.verify_gate`
(`finding-037`, `.claude/commands/verify-and-merge.md`) — lives in
[`docs/runbooks/verification.md`](docs/runbooks/verification.md). The
commands above remain the quick reference during implementation; the
runbook is the canonical gate.

## Things never to do

- Never modify the schema markdown files in `docs/schemas/` or the DDL files extracted from them, except via a deliberate, documented schema change followed by a re-extraction.
- Never UPDATE an active insight or evidence row to change its content. Use the supersession workflow.
- Never bulk-load gnomAD without filtering to the (user ∪ ClinVar ∪ GWAS ∪ PGS) intersection — full gnomAD is too large. As of finding-035 (adopted 2026-06-21), the active gnomAD filter is narrowed further to `user_only` (the user's own variants — the consumed subset, since every `gnomad_frequencies` reader inner-joins `variants_master`); `user_only` is a strict subset of this upper bound, which remains the documented ceiling and the one-argument revert path (`strategy="three_way"`).
- Never call an external API outside the audited client.
- Never store the body of an external request — only the hash.
- Never embed an API key, passphrase, or other secret in code or tests.
- Never bypass the unified evidence-tier scale by writing a source-specific grade into `insights.evidence_tier`.

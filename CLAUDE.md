# Project Context — DNA Insights App

## What this is
A local-first personal DNA insights application that ingests 23andMe + Ancestry raw exports, merges + imputes them via TopMed, joins against curated reference annotations, runs analytical pipelines, and surfaces a unified insights model.

## Read this before any work
- The five schema documents in `docs/schemas/` are the source of truth for data design. Read the relevant one(s) before touching any DB-adjacent code.
- `ROADMAP.md` defines build phases. Stay within the current phase unless explicitly directed otherwise.
- This file (`CLAUDE.md`) is the persistent context for every session.

## Working with this codebase

This codebase is built collaboratively across four actors. The boundaries between them are part of the convention.

- **AI-Claude** — the planning chat (claude.ai / Claude desktop). Roadmap planning, plan review, handoff review, test-results review. Does not touch the codebase or run commands.
- **VSC-ClaudeCodePlanning** — Claude Code in plan mode — toggled locally via Shift+Tab, or invoked in the cloud via /ultraplan for inline-comment review. Reads the repo, produces a technical plan, surfaces questions. Does not write code or run commands.
- **VSC-ClaudeCode** — Claude Code in normal mode. Writes code, runs the dev-loop tests, commits, pushes, opens PRs, produces an end-of-session handoff via `/handoff`.
- **VSC-User** — the human operator. Approves plans, runs the formal verification protocol (`docs/runbooks/verification.md`), merges.

Older docs and CHANGELOG entries use **VSC-Claude** as a single name. That maps to VSC-ClaudeCode (implementation mode) by default — VSC-ClaudeCodePlanning is the newer split.

### Plan mode first

For any non-trivial change — anything that touches multiple files, modifies behavior, alters the schema, or adds a dependency — Claude Code starts in plan mode. Implementation does not begin until VSC-User has approved the plan. Trivial work (typo fixes, single-line docstring edits, comment clarifications) can proceed without plan mode.

When in plan mode, read the listed inputs first — `CLAUDE.md`, `ROADMAP.md`, relevant `docs/findings/`, and the relevant `backend/src/genome/` subdirectory — then produce a plan containing:

1. **Reading list confirmation** — the docs and code files that were read.
2. **Problem statement** — what's wrong or missing. Specific numbers, error messages, or symptoms.
3. **Constraints** — locked decisions respected, schema files that won't change without re-extraction, code that won't be refactored opportunistically.
4. **Implementation plan** — numbered tasks.
5. **Tests** — new tests to add, existing tests that must still pass.
6. **Verification** — how to confirm success. Test counts, lint/type clean, expected real-data outputs.
7. **Out-of-scope** — explicit list. Phase boundaries, optional features, things to defer.
8. **End-of-session handoff** — `/handoff` at session end.

If plan mode surfaces a question that needs judgment outside the code (roadmap-level trade-offs, architectural fit, alignment with locked decisions), pause and ask VSC-User. VSC-User routes the question to AI-Claude and returns with an answer.

### Implementation contract

Once VSC-User approves the plan, VSC-ClaudeCode executes it. The expectation is mechanical execution — surprises at this stage usually mean the plan missed something, and the right move is to pause and escalate rather than improvise.

Every implementation session produces:
- A new branch from `main`.
- A clean dev-loop (`pytest`, `ruff check`, `ruff format --check`, `mypy --strict backend/src`).
- A commit + push.
- An end-of-session handoff via `/handoff` (`.claude/commands/handoff.md`).

VSC-User runs the canonical verification independently against the pushed branch — see `docs/runbooks/verification.md`. That independence is the gate that catches selective test runs, test mutation, and number-interpretation slippage. It is not optional and is not negotiable.

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
   - chrX imputed variants: 0 for males, because hemizygous positions land as `ref==alt` at the prepare layer (finding-005 #6) and so are dropped before Beagle ever sees them.
   - Full-genome runtime: ~30 min on Linux, 16 threads, 8 GB heap.
   - Post-merge `consensus_genotypes`: 3,210,371 rows (942,620 chip-derived; 2,267,751 imputed-only under the `consensus_v1` Phase 4 extension; the 101,420 chip+imputed overlap variants stay chip-derived with the imputed call appended to `contributing_calls` as confirming evidence).
   - Phase 3 numbers preserved exactly through Phase 4: `both_concordant=120,516`, `disagreement_resolved=106`, `single_source=821,998`, shared-call concordance=1.0000, `strand_flip_resolutions=106`, palindromic shared variants=31.

   These numbers are stable identifiers through Phase 4. Drift in any of them on a re-run against the same input corpus is a regression signal **at the Phase-4 boundary**. They re-lock at the PR-3 canonicalize step (post-5.7 backfill — see observation #6 and finding-020) as the deliberate consequence of exposing previously-hidden cross-chip disagreements; the shared-call concordance specifically **drops from 1.0000** under PR 3 and that drop is a correction, not a regression. See finding-020 "Concordance re-lock" + bedrock anchor table for the post-PR-3 numbers.

4. **Phase 5.7 `variant_annotations_index` first build is allele-match-gated, not position-match-gated (see finding-018).** Real-data verification of `genome annotate refresh-index` against the user's loaded corpus (ClinVar `2026_05_17`, gnomAD `4.1.1`, GWAS `2026_05_16`, PharmGKB `2025_07_05`) established these durable numbers:
   - `row_count` = 159,658; wall-clock ~2.2 s (well under the 30 s target).
   - `gnomad_matches` = 101,501, `clinvar_matches` = 2,559, `gwas_matches` = 66,726, `pharmgkb_matches` = 1,737, `curated_count` = 4,198.
   - `is_rare` TRUE = 848, `is_ultrarare` TRUE = 421 (the matched variants are overwhelmingly common chip SNPs).
   - The coord-keyed sources (ClinVar, gnomAD) match far below the position-level overlap because **78.3% of `variants_master` is hom-ref (`ref==alt`, finding-005 #6)** and **~50% of the genuine `ref≠alt` variants match gnomAD only with REF/ALT swapped** — `variants_master` REF/ALT is not yet canonicalized (finding-005 #1). The rsid-keyed sources (GWAS, PharmGKB) are unaffected. This is expected, not a regression; the match rate rises when the post-5.7 canonical-REF/ALT backfill re-runs the index.

   These numbers are stable identifiers for the **pre-PR-3** (pre-canonicalization) `variants_master`. Drift on a re-run against the same corpus + same source versions is a regression signal at that boundary. The PR-3 canonicalize step is the deliberate event that re-locks `gnomad_matches` / `clinvar_matches` upward (hundreds of thousands); `gwas_matches` / `pharmgkb_matches` (rsid-keyed) stay unchanged. See observation #6 and finding-020 bedrock anchor table for the post-PR-3 numbers.

5. **`variant_aliases` is populated by `genome annotate refresh-aliases`, the first post-5.7 backfill (see finding-019).** It loads NCBI's dbSNP `RsMergeArch.bcp.gz` (legacy rs-merge archive, ~146 MB, frozen 2018/build ~151 — the VCF carries no merge history) filtered to merges touching the user's own rsIDs on either side, mapping `alias_rsid (old) → current_rsid (survivor)` with `alias_type='merged'`. It is **not** a registered `--source` loader; it attaches alias rows under the **current dbSNP `source_version_id`** (the dbsnp source group's two tables share one `annotation_sources` pointer per decision #7 / PR #57) — no new version, no pointer flip, no VCF re-stream. Consequence: **re-run `refresh-aliases` after any future `refresh --source dbsnp`** that flips the dbsnp pointer, or the new epoch carries no aliases. Locked first-run drift identifiers against the user's corpus (dbSNP `157`, `variants_master` 927,964 distinct rsIDs; RsMergeArch 11,963,907 source rows, ~54 s wall-clock): `rows_loaded` = 839,413, `distinct_alias_rsid` = 839,413, `distinct_current_rsid` = 513,573, **`user_old_rsid_hits` = 1,190** (the tier-2-lift proxy — user variants carrying a now-mappable stale rsID), `user_current_rsid_hits` = 512,408. Drift on a re-run against the same corpus + frozen RsMergeArch is a regression signal. Tier-2 rsID matching (finding-005 #4) is the consumer, a later PR.

6. **Canonical REF/ALT backfill + hom-only recovery — the second post-5.7 backfill (see finding-020).** New `genome annotate canonicalize-variants` rewrites `variants_master.(ref_allele, alt_allele)` against the currently-active dbSNP source-version: re-orients the alphabetical-ordering swap victims dominant in finding-018 (~101,918 genuine `ref≠alt` rows whose `(ref,alt)` matched dbSNP only when swapped), recovers hom-only `ref==alt` rows by assigning a real ALT (closing finding-005 #1 ordering aspect and #6 — the imputation-input hom-only drop), and collapses rows whose new canonical key collides with a sibling at the same position (re-pointing `genotype_calls.variant_id` FKs to the survivor; `variant_id` is **not preserved** for movers, see finding-020 §2). The companion `genome annotate align-tier3-consensus` runs after `merge` to delete the non-canonical-side consensus rows for the strand-flipped duplicates that Scope-A canonicalize leaves as two rows. Auto pre-mutation snapshot of `genome.duckdb` to `archive/canonicalize/` with `--no-backup` opt-out. Two-transaction split sidesteps DuckDB's FK-on-DELETE enforcement that doesn't see in-transaction FK re-points. **Triggers the re-lock of observation #3 (merge counts) and #4 (index match counts)** — the shared-call concordance drops from 1.0000 by design, exposing previously-hidden cross-chip disagreements (see finding-020 "Concordance re-lock — correction, not regression"). Runbook sequence: `canonicalize-variants` → `merge` → `align-tier3-consensus` → `refresh-index`. Strand-flip `variants_master` collapse (the ~106 tier-3 pairs requiring `genotype_calls` allele complementing via supersession) is deferred to PR 5; tracked in finding-005 #1 as a deferred sub-item. The first-authoritative-run locked numbers (capture on first real-data run; the bedrock anchor table in finding-020 lists every shifted value with explicit correction-not-regression framing) are the regression signal going forward.

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
- Dev API (later phases): `uvicorn genome.api.main:app --reload`
- Frontend (later phases): `cd frontend && pnpm dev`

The merge-gate verification protocol — what VSC-User runs independently
of the dev-loop commands above before merging a branch — lives in
[`docs/runbooks/verification.md`](docs/runbooks/verification.md). The
commands above remain the quick reference during implementation; the
runbook is the canonical gate.

## Things never to do

- Never modify the schema markdown files in `docs/schemas/` or the DDL files extracted from them, except via a deliberate, documented schema change followed by a re-extraction.
- Never UPDATE an active insight or evidence row to change its content. Use the supersession workflow.
- Never bulk-load gnomAD without filtering to the (user ∪ ClinVar ∪ GWAS ∪ PGS) intersection — full gnomAD is too large.
- Never call an external API outside the audited client.
- Never store the body of an external request — only the hash.
- Never embed an API key, passphrase, or other secret in code or tests.
- Never bypass the unified evidence-tier scale by writing a source-specific grade into `insights.evidence_tier`.

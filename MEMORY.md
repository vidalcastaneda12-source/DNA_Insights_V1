# MEMORY — decision ledger

The project's single, git-tracked **decision log**: every architectural and tactical
decision lands one parseable row here, with a lifecycle status and provenance. It closes
the leak where tactical choices lived only in PR bodies and reversed decisions were
discoverable only by prose archaeology (see `docs/findings/finding-036-decision-tracking-ledger.md`).

> **Not** Claude Code's session auto-memory. A separate, untracked `MEMORY.md` lives under
> `~/.claude/projects/<session>/memory/` and is the assistant's per-session scratch index.
> *This* file is the durable, version-controlled project decision ledger. Same filename,
> different system — do not conflate them.

## How to read a row

Columns: `DEC | kind | date | status | superseded_by | actors | provenance | decision | detail-link`.

- **DEC** — `DEC-NNNN`, unique and monotonic. The stable id you cite from PRs, findings, and handoffs.
- **kind** — the granularity axis: `architectural` (a locked design decision) or `tactical`
  (an implementation-level choice). This is the former *grain*; it is **orthogonal** to status.
- **status** — the lifecycle axis: `active` · `superseded` · `reversed` · `deferred`.
- **superseded_by** — the `DEC-NNNN` that replaces this row (required when status is
  `superseded`/`reversed`; `—` otherwise).
- **actors** — one or more of the five canonical names (`ClaudeCodeVerification`,
  `ClaudeCodeTestingBugs`, `ClaudeCodePlanning`, `ClaudeCodeDevelopment`, `VSC-User`).
  Legacy spellings in history are mapped on read.
- **provenance** — verbatim source (`finding-035`, `PR #93`, `git:<sha>`) or the literal
  `unknown` for unrecoverable backfill rows. Never guessed.
- **decision** — one-sentence statement. Free text; a literal `|` is escaped `\|`. Real-data
  anchors (imputation/index/consensus digits) are **referenced** (`see CLAUDE.md obs #4`),
  never transcribed — `genome docs check` fails on a copied anchor number.
- **detail-link** — the canonical home of the full rationale (a finding, a CLAUDE.md obs #N).

## Lifecycle — insert-then-flip (advisory analogue of locked decision #7)

A markdown table has no transaction, so this ledger is an **advisory, validator-enforced**
analogue of decision #7, not a transactional one. The no-torn-state / never-UPDATE-active-content
invariant is relocated to the `genome docs check` gate. The only sanctioned way to change a
decision is **insert-then-flip**:

1. **Insert** a new row for the new decision (a fresh `DEC-NNNN`, `status: active`).
2. **Flip** the old row's `status` → `superseded`/`reversed` and set its `superseded_by` to the
   new `DEC-NNNN`.

A row's **content columns** (`kind`, `date`, `actors`, `provenance`, `decision`, `detail-link`)
are immutable once written; only `status`/`superseded_by` transition. The supersession edge is
authored authoritatively in the **finding frontmatter**; this ledger's cross-links are *derived*
by `genome docs build-index`. Run `genome docs check` before committing.

## Worked example (also a real, active pair — dogfoods the parser)

The gnomAD frequency-filter narrowing: `three_way` (finding-011) was superseded by `user_only`
(finding-035) when VSC-User ruled on it. Note `DEC-0001` flipped to `superseded` with a
back-pointer; `DEC-0002` is the live decision.

<!-- BEGIN decision-ledger -->

| DEC | kind | date | status | superseded_by | actors | provenance | decision | detail-link |
|---|---|---|---|---|---|---|---|---|
| DEC-0001 | architectural | 2026-05-22 | superseded | DEC-0002 | VSC-User, ClaudeCodeDevelopment | finding-011 | gnomAD frequency filter scoped three-way (user ∪ ClinVar ∪ GWAS ∪ PGS); retained as the documented revert / PGS-extension baseline | docs/findings/finding-011-gnomad-three-way-intersection.md |
| DEC-0002 | architectural | 2026-06-21 | active | — | VSC-User | finding-035 | gnomAD filter narrowed to `user_only` — the consumed subset, since every `gnomad_frequencies` reader inner-joins `variants_master`; `three_way` kept as the one-argument revert path | docs/findings/finding-035-gnomad-filter-set-consumer-audit.md |
| DEC-0003 | tactical | 2026-05-12 | active | — | ClaudeCodeDevelopment | finding-001 | Lift-over uses the `liftover` CFFI package by default behind a `Liftover` Protocol that abstracts engine selection (pyliftover fallback, loud INFO on fallback) | docs/findings/finding-001-liftover-engine-selection.md |
| DEC-0004 | tactical | 2026-05-12 | active | — | ClaudeCodeDevelopment | finding-004 | Bulk DuckDB loads use PyArrow Table registration + INSERT…SELECT, never executemany (no batch-bind, catastrophically slow at scale) | docs/findings/finding-004-duckdb-bulk-load-pattern.md |
| DEC-0005 | tactical | 2026-05-12 | active | — | ClaudeCodeDevelopment | finding-005 | A set of Phase-2/3 improvements were explicitly deferred rather than block the current phase; see finding-005 for the tracked list | docs/findings/finding-005-deferred-improvements.md |
| DEC-0006 | architectural | 2026-05-12 | active | — | ClaudeCodeDevelopment | finding-006 | TopMed imputation rejected for personal genomics; Beagle full-genome imputation is the chosen path | docs/findings/finding-006-topmed-not-viable-for-personal-genomics.md |
| DEC-0007 | architectural | 2026-05-19 | active | — | ClaudeCodeDevelopment | finding-010 | Source-grain supersession uses the single-row version-pointer pattern; the atomicity guarantee lives in the transaction, not per-row tags (decision #7) | docs/findings/finding-010-version-pointer-supersession-pattern.md |
| DEC-0008 | tactical | 2026-05-22 | active | — | ClaudeCodeDevelopment | finding-013 | Synthetic test fixtures are built from real data shapes, not assumptions | docs/findings/finding-013-synthetic-fixture-realism.md |
| DEC-0009 | tactical | 2026-05-26 | active | — | ClaudeCodeDevelopment | finding-016 | The dbSNP annotation filters to the user's own variants only | docs/findings/finding-016-dbsnp-user-only-filter.md |
| DEC-0010 | architectural | 2026-05-26 | active | — | ClaudeCodeDevelopment | finding-017 | Phase 5 restructured around a loader/runner split with a per-source loader registry | docs/findings/finding-017-phase-5-restructure.md |
| DEC-0011 | tactical | 2026-05-27 | active | — | ClaudeCodeDevelopment | finding-019 | `variant_aliases` backfilled from dbSNP RsMergeArch, filtered to merges touching the user's rsIDs, attached under the current dbSNP version pointer | docs/findings/finding-019-variant-aliases-backfill.md |
| DEC-0012 | tactical | 2026-06-10 | active | — | ClaudeCodeDevelopment | finding-020 | Canonical REF/ALT backfill re-orients alphabetical-swap victims and recovers hom-only rows, collapsing colliding siblings to a survivor | docs/findings/finding-020-canonical-refalt-backfill.md |
| DEC-0013 | tactical | 2026-06-09 | active | — | ClaudeCodeDevelopment | finding-021 | Imputation rsID hygiene replaces synthetic chr:pos:ref:alt IDs in `variants_master` with real rsIDs | docs/findings/finding-021-imputation-rsid-hygiene.md |
| DEC-0014 | tactical | 2026-06-11 | active | — | ClaudeCodeDevelopment | finding-025 | Tier-2 rsID matching resolves merged-away rsIDs through `variant_aliases` during refresh-index | docs/findings/finding-025-tier2-rsid-matching.md |
| DEC-0015 | tactical | 2026-06-13 | active | — | ClaudeCodeDevelopment | finding-026 | Same-SNP duplicate `variants_master` rows from strand flips are collapsed via `genotype_calls` allele complementing under supersession | docs/findings/finding-026-strand-flip-variants-master-collapse.md |
| DEC-0016 | tactical | 2026-06-13 | active | — | ClaudeCodeDevelopment | finding-028 | `consensus_v1` corrected so a chip no-call does not clobber a confident imputed call | docs/findings/finding-028-consensus-nocall-imputed-clobber.md |
| DEC-0017 | architectural | 2026-06-19 | active | — | ClaudeCodeDevelopment | finding-029 | chrX imputation uses the M3-physical region split (superseding the M1 diploidization approach) to recover non-PAR calls | docs/findings/finding-029-chrx-imputation-m1.md |
| DEC-0018 | tactical | 2026-06-19 | active | — | ClaudeCodeDevelopment | finding-031 | chrX non-PAR QC uses dosage-confidence max(DS,1−DS) + a 5-fold LOO concordance, since Beagle's `INFO/DR2` is structurally dead on male hemizygous markers | docs/findings/finding-031-chrx-nonpar-dosage-confidence-qc.md |
| DEC-0019 | tactical | 2026-06-19 | active | — | ClaudeCodeDevelopment | finding-033 | The chrX LOO harness uses allele-aware matching; position-only matching manufactures concordance | docs/findings/finding-033-chrx-loo-allele-aware-matching.md |
| DEC-0020 | architectural | 2026-06-19 | active | — | VSC-User, ClaudeCodeDevelopment | finding-034 | The per-scope agent team (plan → implement → review → close) was designed with two human gates | docs/findings/finding-034-agent-team-plan-phase.md |
| DEC-0021 | architectural | 2026-06-23 | active | — | VSC-User, ClaudeCodeDevelopment | finding-036 | Adopt a git-tracked `MEMORY.md` decision ledger + per-finding frontmatter + a `genome docs` validator gate relocating decision #7's invariant onto markdown | docs/findings/finding-036-decision-tracking-ledger.md |
| DEC-0022 | tactical | 2026-05-12 | active | — | VSC-User, ClaudeCodeDevelopment | PR #19 | Fix privacy switch default + audit blocked external calls | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/19 |
| DEC-0023 | tactical | 2026-05-14 | active | — | VSC-User, ClaudeCodeDevelopment | PR #28 | merge: extend consensus_v1 in place to handle imputed-only variants | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/28 |
| DEC-0024 | tactical | 2026-05-14 | active | — | VSC-User, ClaudeCodeDevelopment | PR #30 | imputation: scrub TopMed text from `prepare` CLI output | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/30 |
| DEC-0025 | architectural | 2026-05-14 | active | — | VSC-User, ClaudeCodeDevelopment | PR #31 | schema: fix imputed view columns to filter on beagle_imputed | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/31 |
| DEC-0026 | tactical | 2026-05-14 | active | — | VSC-User, ClaudeCodeDevelopment | PR #32 | docs: capture Phase 4 rebuild workflow and chrX Beagle failure | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/32 |
| DEC-0027 | tactical | 2026-05-15 | active | — | VSC-User, ClaudeCodeDevelopment | PR #33 | Phase 5.0 — annotation loader scaffold | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/33 |
| DEC-0028 | tactical | 2026-05-15 | active | — | VSC-User, ClaudeCodeDevelopment | PR #34 | Phase 5.1a — PharmGKB clinical annotations loader | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/34 |
| DEC-0029 | tactical | 2026-05-15 | active | — | VSC-User, ClaudeCodeDevelopment | PR #35 | Phase 5.1b — CPIC clinical guidelines loader | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/35 |
| DEC-0030 | tactical | 2026-05-15 | active | — | VSC-User, ClaudeCodeDevelopment | PR #36 | Phase 5.2 — ClinVar clinical-significance annotations loader | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/36 |
| DEC-0031 | tactical | 2026-05-17 | active | — | VSC-User, ClaudeCodeDevelopment | PR #37 | docs: pre-5.3 cleanup — PR refs, Phase 5 status, perf target, finding-009 | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/37 |
| DEC-0032 | tactical | 2026-05-17 | active | — | VSC-User, ClaudeCodeDevelopment | PR #38 | Phase 5.3 — GWAS Catalog associations loader | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/38 |
| DEC-0033 | tactical | 2026-05-17 | active | — | VSC-User, ClaudeCodeDevelopment | PR #39 | Phase 5.4 — PGS Catalog metadata loader | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/39 |
| DEC-0034 | tactical | 2026-05-17 | active | — | VSC-User, ClaudeCodeDevelopment | PR #40 | docs: reconcile ROADMAP Phase 5 sub-phase status with merged PRs | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/40 |
| DEC-0035 | architectural | 2026-05-18 | active | — | VSC-User, ClaudeCodeDevelopment | PR #41 | Pre-5.5 — supersession observability + --skip-if-same-version | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/41 |
| DEC-0036 | architectural | 2026-05-18 | active | — | VSC-User, ClaudeCodeDevelopment | PR #42 | Pre-5.5 — unify supersession deactivate path + finding-009 correction | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/42 |
| DEC-0037 | architectural | 2026-05-19 | active | — | VSC-User, ClaudeCodeDevelopment | PR #43 | Pre-5.5 — version-pointer supersession refactor (annotation tables) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/43 |
| DEC-0038 | architectural | 2026-05-19 | active | — | VSC-User, ClaudeCodeDevelopment | PR #44 | Pre-5.5 — docs reconcile for version-pointer supersession (PR #43 follow-up) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/44 |
| DEC-0039 | tactical | 2026-05-19 | active | — | VSC-User, ClaudeCodeDevelopment | PR #45 | Pre-5.5 — ROADMAP refresh and remaining-Phase-5 sequencing | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/45 |
| DEC-0040 | tactical | 2026-05-19 | active | — | VSC-User, ClaudeCodeDevelopment | PR #46 | Pre-5.5 — add `gnomad_frequencies.af_mid` (Middle Eastern AF) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/46 |
| DEC-0041 | tactical | 2026-05-19 | active | — | VSC-User, ClaudeCodeDevelopment | PR #47 | Pre-5.5 — lock real-data numbers for GWAS 2026_05_16 and PGS 2026_05_07 | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/47 |
| DEC-0042 | tactical | 2026-05-19 | active | — | VSC-User, ClaudeCodeDevelopment | PR #48 | Pre-5.5 — align annotations runbook prose with locked GWAS/PGS numbers | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/48 |
| DEC-0043 | tactical | 2026-05-22 | active | — | VSC-User, ClaudeCodeDevelopment | PR #49 | Phase 5.5 — gnomAD filtered allele frequencies loader | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/49 |
| DEC-0044 | tactical | 2026-05-22 | active | — | VSC-User, ClaudeCodeDevelopment | PR #50 | gwas_catalog: hash-based fallback for upstream label drift (+ finding-014) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/50 |
| DEC-0045 | tactical | 2026-05-24 | active | — | VSC-User, ClaudeCodeDevelopment | PR #51 | Pre-5.6 — fix cpic test format drift + consolidate verification protocol | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/51 |
| DEC-0046 | tactical | 2026-05-24 | active | — | VSC-User, ClaudeCodeDevelopment | PR #52 | docs: finding-015 — gnomad v10 audit-trail investigation (orphan version rows) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/52 |
| DEC-0047 | tactical | 2026-05-24 | active | — | VSC-User, ClaudeCodeDevelopment | PR #53 | gnomad: cleanup orphan annotation_source_versions rows (finding-015 option B) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/53 |
| DEC-0048 | tactical | 2026-05-24 | active | — | VSC-User, ClaudeCodeDevelopment | PR #54 | Workflow tooling — scripts/verify.sh + /handoff slash command | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/54 |
| DEC-0049 | tactical | 2026-05-24 | active | — | VSC-User, ClaudeCodeDevelopment | PR #55 | docs(changelog): backfill missing Phase 5 [Unreleased] entries | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/55 |
| DEC-0050 | tactical | 2026-05-24 | active | — | VSC-User, ClaudeCodeDevelopment | PR #56 | Changelog backfill phase 5 | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/56 |
| DEC-0051 | tactical | 2026-05-25 | active | — | VSC-User, ClaudeCodeDevelopment | PR #57 | Phase 5.6 PR A — surrogate BIGINT PKs for dbsnp_annotations and variant_aliases | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/57 |
| DEC-0052 | tactical | 2026-05-25 | active | — | VSC-User, ClaudeCodeDevelopment | PR #58 | docs: correct Phase 5.5/5.6 staleness in ROADMAP + README | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/58 |
| DEC-0053 | tactical | 2026-05-26 | active | — | VSC-User, ClaudeCodeDevelopment | PR #59 | Phase 5.6 PR B — dbSNP loader + remote-tabix/filter-set extraction | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/59 |
| DEC-0054 | tactical | 2026-05-26 | active | — | VSC-User, ClaudeCodeDevelopment | PR #60 | docs: update CLAUDE.md to document four-actor collaboration and implementation contract | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/60 |
| DEC-0055 | tactical | 2026-05-26 | active | — | VSC-User, ClaudeCodeDevelopment | PR #61 | docs: reset Phase 5 around the loader/runner cut | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/61 |
| DEC-0056 | tactical | 2026-05-26 | active | — | VSC-User, ClaudeCodeDevelopment | PR #62 | Phase 5.7 — variant_annotations_index rollup builder (closes Phase 5) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/62 |
| DEC-0057 | tactical | 2026-05-27 | active | — | VSC-User, ClaudeCodeDevelopment | PR #63 | Pre-Phase-6 cleanup: phase docstrings, reload sequence, BGZF guard, verify TMPDIR | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/63 |
| DEC-0058 | tactical | 2026-05-27 | active | — | VSC-User, ClaudeCodeDevelopment | PR #64 | Populate variant_aliases from dbSNP RsMergeArch (post-5.7 backfill) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/64 |
| DEC-0059 | tactical | 2026-06-09 | active | — | VSC-User, ClaudeCodeDevelopment | PR #66 | Imputation rsID hygiene: strict predicate + synthetic-ID sweep (finding-021) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/66 |
| DEC-0060 | tactical | 2026-06-10 | active | — | VSC-User, ClaudeCodeDevelopment | PR #65 | PR 3 — Canonical REF/ALT backfill + hom-only recovery + tier-3 align (finding-020) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/65 |
| DEC-0061 | tactical | 2026-06-10 | active | — | VSC-User, ClaudeCodeDevelopment | PR #67 | Pre-Phase-6 docs/comment hygiene: ROADMAP sequence + README status + canonicalize comment | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/67 |
| DEC-0062 | architectural | 2026-06-11 | active | — | VSC-User, ClaudeCodeDevelopment | PR #68 | Fix drifted pgx_phenotype_drugs_v view in schema markdown (finding-010 #16) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/68 |
| DEC-0063 | tactical | 2026-06-11 | active | — | VSC-User, ClaudeCodeDevelopment | PR #69 | Document genome status vs egress-gate divergence (finding-024) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/69 |
| DEC-0064 | tactical | 2026-06-11 | active | — | VSC-User, ClaudeCodeDevelopment | PR #70 | PR 4 — Tier-2 rsID matching in refresh-index (finding-025) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/70 |
| DEC-0065 | tactical | 2026-06-13 | active | — | VSC-User, ClaudeCodeDevelopment | PR #72 | PR 5b-pre — consensus_v1 chip-no-call vs imputed-real fix (finding-028) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/72 |
| DEC-0066 | tactical | 2026-06-13 | active | — | VSC-User, ClaudeCodeDevelopment | PR #73 | PR 5b — same-SNP duplicate variants_master collapse (closes finding-005 #1) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/73 |
| DEC-0067 | tactical | 2026-06-19 | active | — | VSC-User, ClaudeCodeDevelopment | PR #75 | gnomAD parallel import via --jobs (process pool, staged-Parquet merge) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/75 |
| DEC-0068 | tactical | 2026-06-19 | active | — | VSC-User, ClaudeCodeDevelopment | PR #74 | PR 5a — chrX imputation via M3-physical region split + dosage-confidence QC (closes finding-008) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/74 |
| DEC-0069 | tactical | 2026-06-20 | active | — | VSC-User, ClaudeCodeDevelopment | PR #77 | docs(plans): post-merge follow-up plan for PR #74 (chrX M3) + PR #75 (gnomAD --jobs) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/77 |
| DEC-0070 | tactical | 2026-06-21 | active | — | VSC-User, ClaudeCodeDevelopment | PR #79 | feat(agent-team): per-scope agent team Stages 0–5 + guardrail hooks + skills (finding-034) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/79 |
| DEC-0071 | tactical | 2026-06-21 | active | — | VSC-User, ClaudeCodeDevelopment | PR #80 | docs(finding-034): reconcile status prose with the merged build (Stage-5 close) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/80 |
| DEC-0072 | tactical | 2026-06-21 | active | — | VSC-User, ClaudeCodeDevelopment | PR #81 | feat(agent-team): add /scope-run model-driven orchestrator (finding-034) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/81 |
| DEC-0073 | tactical | 2026-06-21 | active | — | VSC-User, ClaudeCodeDevelopment | PR #82 | docs(finding-029): lock run_0002 chrX M3 + post-chrX anchors (PR A doc-lock) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/82 |
| DEC-0074 | tactical | 2026-06-22 | active | — | VSC-User, ClaudeCodeDevelopment | PR #83 | feat(annotate): narrow gnomAD filter to user_only (finding-035, PR B) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/83 |
| DEC-0075 | tactical | 2026-06-22 | active | — | VSC-User, ClaudeCodeDevelopment | PR #84 | docs(close): close PR-B scope (finding-035) + clear PR-A-deferred doc-staleness | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/84 |
| DEC-0076 | tactical | 2026-06-22 | active | — | VSC-User, ClaudeCodeDevelopment | PR #85 | docs(pr-c): chrX gnomAD gap reload + final user_only number-lock | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/85 |
| DEC-0077 | tactical | 2026-06-22 | active | — | VSC-User, ClaudeCodeDevelopment | PR #86 | docs(pr-c-followup): clear stale verification.md ref + guard ROADMAP PR 7 / finding-015 orphan DELETE | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/86 |
| DEC-0078 | tactical | 2026-06-23 | active | — | VSC-User, ClaudeCodeDevelopment | PR #87 | docs(pr-6): approved plan + ACMG SF v3.3 seed dataset + dev-session start prompt | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/87 |
| DEC-0079 | tactical | 2026-06-23 | active | — | VSC-User, ClaudeCodeDevelopment | PR #88 | feat(annotate): seed genes with ACMG SF v3.3 + derived PGx symbols | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/88 |
| DEC-0080 | tactical | 2026-06-23 | active | — | VSC-User, ClaudeCodeDevelopment | PR #90 | docs(pr-6-close): re-lock gate-confirmed genes-seed anchors + flip ROADMAP slot | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/90 |
| DEC-0081 | tactical | 2026-06-23 | active | — | VSC-User, ClaudeCodeDevelopment | PR #89 | Plan: Decision Tracking Leak Fix (Option D) | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/89 |
| DEC-0082 | tactical | 2026-06-23 | active | — | VSC-User, ClaudeCodeDevelopment | PR #91 | docs(verify): scope the pre-squash GATE-FILL grep to durable docs | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/91 |
| DEC-0083 | tactical | 2026-06-23 | active | — | VSC-User, ClaudeCodeDevelopment | PR #92 | docs(verify): make the pre-squash GATE-FILL check a positive allowlist | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/92 |
| DEC-0084 | tactical | 2026-06-23 | active | — | VSC-User, ClaudeCodeDevelopment | PR #93 | docs(plans): prune five implemented plan artifacts; keep the live one | https://github.com/vidalcastaneda12-source/DNA_Insights_V1/pull/93 |
| DEC-0085 | tactical | 2026-06-24 | active | — | VSC-User, ClaudeCodeDevelopment | finding-036 | Made the `genome` CLI pysqlcipher3-lazy: four module-scope SQLCipher imports moved to call time + the `genome.db` re-export dropped (option b), so `genome docs check` runs on a fresh checkout with no SQLCipher built; pysqlcipher3 is lazy, not removed | docs/plans/decision-tracking-followups.md |

<!-- END decision-ledger -->

_**Backfill status:** `DEC-0001 … DEC-0021` are the curated decision-finding rows (every
`type: decision`/`both` finding). `DEC-0022 … DEC-0084` are the **per-PR-history retrospective** —
one row per merged PR in `main`'s history, the squash-merge subject git-verbatim as the decision.
**Declared complete: PRs #19 … #93** (63 PR-referenced commits, the full PR history
in the current `main` lineage; any pre-#19 PRs predate this lineage). New PRs append the next row
at the `/handoff` or Stage-5 checkpoint._

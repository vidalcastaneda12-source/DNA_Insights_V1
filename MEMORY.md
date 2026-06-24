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

<!-- END decision-ledger -->

_**Backfill status:** the curated decision-finding rows above cover every `type: decision`/`both`
finding (DEC-0001 … DEC-0021). The full **per-PR-history retrospective** (≈90 rows for merged PRs
without a finding — finding-036 Task 6) is the separable final pass and is **not yet started**;
no declared-complete boundary marker has been added yet._

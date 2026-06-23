# New-session start prompt — PR 6 implementation

Copy everything in the fenced block below into a fresh Claude Code session opened in this
repo (`/home/vidal_h0df0uq/projects/dna-insights`).

```
You are ClaudeCodeDevelopment for this session. Implement Pre-Phase-6 **PR 6 — the minimal
`genes` seed** for the DNA Insights app. The plan is already approved at Human Gate 1; this
session executes it and ends at Human Gate 2 (VSC-User runs verification.md + merges).

Read first, in order:
1. docs/plans/pr-6-genes-seed.md      — the approved 8-section plan (authoritative).
2. docs/plans/pr-6-acmg-sf-v3.3-genes.csv — the verified 84-gene ACMG SF v3.3 panel
   (gene_symbol, acmg_sf_disease, acmg_sf_inheritance, acmg_sf_version). Bake this into
   seed_genes.py as the static ACMG constant.
3. CLAUDE.md (locked decisions, conventions, "Things never to do") and the genes DDL in
   ddl/group_2_annotations.sql + the four derived_* FKs in ddl/group_3_derived.sql.

The four design decisions are LOCKED (do not re-open): (1) derive the PGx/carrier symbols
in-code from cpic_guidelines ∪ pharmgkb_annotations, current-version-scoped; (2) provenance
via one real annotation_source_versions row under source_db='hgnc' (new svid = 11), NOT
source_version_id=NULL; (3) clinvar-exact transaction ordering — insert_source_version in
autocommit before begin(), bulk INSERT inside begin(), on failure rollback-THEN-cleanup-
orphan; (4) amend finding-020's stale "genes seed → Phase 7" note + add the CHANGELOG entry
in this PR. Escalation A (the ACMG panel) is already supplied and verified against the
official ACMG SF v3.3 supplementary — do not re-fetch it.

Implement using the **implement-review structure** (finding-034 Stage 2 + Stage 3),
mirroring how the Plan phase was run:
- The orchestrator is .claude/workflows/implement-review.js. IMPORTANT: like plan-phase.js,
  it targets an abstract subagent primitive and has no `export const meta`, so it will NOT
  run verbatim under the Workflow tool. Run its logic via a faithful Workflow-dialect port
  (export const meta; each step = agent(prompt, {agentType:'<name>'}) with JSON in/out;
  parallel() for fan-outs; top-level return), OR via the `scope-run` skill if you prefer.
  The agent types (implementer, test-author, green-keeper, plan-adherence-sentinel,
  silent-failure-hunter, convention-compliance, phi-pii-guardian, finding-verifier,
  review-synthesizer, test-integrity, pr-test-analyzer) resolve from the Agent registry.
- Tier 1 → Stage 2 = interface-freeze → plan-blind test-author ∥ implementer → green loop
  (green-keeper + plan-adherence-sentinel + silent-failure-hunter). Keep the test-author
  plan-blind (it sees the plan §5/§6 + frozen interface, NOT the implementation bodies).
- Stage 3 = convention-compliance + phi-pii-guardian agents + /code-review (+ /security-
  review, data surface) → refute-by-default finding-verifier → review-synthesizer. PR 6 adds
  tests, so ALSO run test-integrity and pr-test-analyzer. Bounded fix-first loop ×2.

Implementation contract (CLAUDE.md): new branch from main; clean dev-loop (pytest, ruff
check, ruff format --check, mypy --strict backend/src); commit + push + open a PR; /handoff
at session end. Do NOT touch docs/schemas/ or ddl/ (rebuild_required=false). Do NOT git add
-A — stage by path (raw genome data is untracked; privacy decision #9). If implementation
surprises you in a way the plan did not cover, STOP and escalate rather than improvise.

Watch-items the plan flags (see its "Pre-mortem watch-items"): the §6 EXCEPT-zero coverage
gate is circular (covers only the cpic/pharmgkb leg) — the keystone probe-INSERT is the
backstop; genes has FIVE FK dependents (4 derived_* + pathway_genes), so the --force leaf-
assert must enumerate all five and raise; sort the seed union before hashing source_file_hash
and assert across-run stability; gene_variant_summary_v will show 0 per gene until Phase 7
backfills coordinates (not a regression).

Real-data gate anchors to leave byte-unchanged (negative control): variants_master=3,160,364;
annotation_sources gnomad pointer svid=10; annotation_sources total=7; obs#4 index counts
unchanged (refresh-index is NOT run by this PR). The only new state is one hgnc
annotation_source_versions row at svid=11.

Start in plan mode only if you find the plan underspecified; otherwise proceed to
implementation per the approved plan.
```

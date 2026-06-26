Run the per-scope agent team (finding-034) for one ROADMAP scope item, end to
end, stopping at each of the two human gates. Argument: a scope id (e.g.
`PR-6`), optionally followed by `--from <stage>` to resume after a gate.

This command is the team's **model-driven conductor and headless/cron fallback** —
the orchestration default went **engine-primary** in Sub Project C2-D (see "Two
orchestration paths" below), so the deterministic JS workflows are preferred and this
command's role is to launch each engine segment by name (pausing for the human between
gates) and to walk the whole pipeline through the Task tool when the engine is
unavailable. The team's members live in `.claude/agents/*.md` and are usable
standalone; this command sequences them with **adaptive depth** and threads each
member's structured JSON output to the next. You (the lead session) spawn each member via
the **Task tool** with the matching `subagent_type`, collect its JSON, and route
per the rules below. You never auto-approve a plan and never merge; those are the
two human gates.

## Two orchestration paths (engine-primary since Sub Project C2-D)

Since Sub Project C2-D (`finding-034` / `DEC-0099`) the orchestration default is
**engine-primary**: the deterministic JS workflows are the preferred path, and this
model-driven command is retained as the **conductor** and the **headless/cron fallback**.

- **The JS workflows — deterministic (preferred).** `.claude/workflows/{plan-phase,
  implement-review,close}.js` encode this pipeline as deterministic control flow in the
  empirically-confirmed dynamic-workflows engine dialect, segmented by the two gates
  (`/plan-phase PR-6` → gate → `/implement-review PR-6` → gate → `/close PR-6`). Same
  members, same depth, same gates as below. **When the dynamic-workflows engine is
  available, use these** (the syntax gate is the AsyncFunction construct-check described in
  `.claude/agents/README.md`).
- **This command (`/scope-run`) — model-driven (conductor + fallback).** Rides the Task
  tool; flexible (the session interprets the routing rules each run) but non-deterministic.
  It keeps two roles: (1) the **conductor** that launches each engine segment by name and
  pauses for the human between segments, and (2) the **headless/cron fallback** that walks
  the whole pipeline through the Task tool **whenever the engine is unavailable**. Same
  members, same depth, same gates.

Either way the guardrail **hooks are live** (`.claude/settings.json`): the
`implementer` / `schema-change-executor` cannot edit `docs/schemas/`|`ddl/`
without `GENOME_ALLOW_SCHEMA_CHANGE=1`, no member can `git add -A`, and commits
get GATE-FILL / CHANGELOG nudges.

## Operating rules

- **Spawn members via Task** with `subagent_type` = the agent file name. Pass the
  upstream JSON the member needs as its prompt input. Run independent members
  **in parallel** (multiple Task calls in one message): the N planners, the
  per-axis judges, the review lenses, the verifier skeptics.
- **Thread the JSON.** Each stage consumes the prior stage's structured output.
  Keep the manifest as the shared source of truth throughout.
- **Respect the two human gates.** Stop and present to VSC-User after Stage 1
  (plan approval) and after Stage 4 (merge verification). Do not proceed past a
  gate on your own; resume with `--from stage2` / `--from stage5`.
- **Bounded loops.** Plan revise loop: ×2 then escalate. Review fix-first loop:
  ×2 then escalate. Stage 3 completeness loop: until-dry (K=2).
- **Read vs write.** Only Stage 2 writers (`implementer`, `test-author`,
  `schema-change-executor`, `fan-out-implementer`) and Stage 5
  `knowledge-curator` may edit. Everyone else is read-only. Never hand the
  `test-author` the implementation diff.
- **Over-tier when unsure.** If the dispatcher's tier is borderline, run the
  deeper tier.

## Stage 0 — Intake

1. Spawn `scope-dispatcher` with the scope id. Collect the **scope manifest**
   (including `risk_tier`, `risk_breakdown`, `review_lenses`, `deep_T2`,
   `applicable_anchors`, `precedent`, `freshness_flags`, `open_questions`).
2. If `freshness_flags` or `open_questions` are non-empty, surface them now (they
   warn; they don't block).
3. The `risk_tier` selects depth for every downstream stage (table below).

## Stage 0.5 — Split check

Before planning the scope as a monolith, run the `scope-split` smart-cut split check (Sub
Project B2 Phase 1, `finding-039`) over the Stage-0 manifest via `genome scope-split check
--manifest -` (feeding the manifest on stdin), the detector behind the `/scope-split` skill.
The `scope-split` detector is **manifest-primary + fail-closed** — atomic is the default, and a
split is proposed only when a candidate cut survives every gate.

- **Atomic** (`atomic — no split`) → proceed to Stage 1 unchanged. This is the common case
  (a tight cluster like PR-3 / PR-5a is correctly indivisible).
- **Clean split** → present the 🚦 **pre-plan micro-gate**: "PR-X is really these N ordered
  PRs" — the ordered sub-scopes with each `origin_scope`, change classes, estimated footprint,
  re-scored tier, and the cut-quality line. Ask VSC-User to **approve / edit / run-as-one**.
  **Stop for the human.** Do not proceed to Stage 1 on your own; the split check is advisory
  and never auto-runs a sub-scope or crosses a gate.

This split check is advisory: it never writes ROADMAP (except via the explicit `genome
scope-split write-roadmap` on approval) and never crosses a gate.

## Stage 1 — Plan

By tier:
- **Tier 0:** 1 `planner` (minimal-diff) → `plan-premortem` (1) → `plan-auditor`.
- **Tier 1:** 2 `planner`s (minimal-diff + gate-backward) in parallel →
  `plan-judges` (light, all axes) → `plan-synthesizer` → `plan-premortem` (1) →
  `plan-auditor` panel (contract + architecture-fit).
- **Tier 2:** full panel of `planner`s (minimal-diff, gate-backward, risk-first,
  convention-purist) in parallel → per-axis `plan-judges` (one per axis, in
  parallel) → `plan-synthesizer` → `plan-premortem` (2 skeptics; 3 if `deep_T2`)
  → `plan-auditor` panel + `architect-reviewer`.

The `plan-auditor` verdict routes: `ready` → the human gate; `revise` → back to
the planner(s) with findings (bounded ×2 → escalate); `escalate` → VSC-User.

**→ HUMAN GATE 1.** Present to VSC-User: the synthesized plan; its **merged
riskiest-assumptions** (first); the **divergence** open questions; the
**predicted surprises** (incl. any `probe-first`). Stop. Do not implement until
VSC-User approves.

## Stage 2 — Implement (after plan approval)

1. **interface-freeze:** have `implementer` declare public signatures / CLI /
   columns as skeleton stubs (or confirm the plan pins them).
2. In parallel: `test-author` (PLAN-BLIND — give it the plan §5/§6 + the frozen
   interface + `predicted_surprises`, **never the diff**) writes the red tests;
   `implementer` fills bodies.
3. `plan-adherence-sentinel` watches the diff; drift → PAUSE + escalate.
4. Green loop: `green-keeper` runs `pytest · ruff check · ruff format --check ·
   mypy --strict backend/src` after each change. On real red → `test-triage` →
   (`deep-debugger` if gnarly). A green-fix needing a weakened test / schema
   touch → escalate.
5. Side-channels: if `change_class ⊇ schema` → `schema-change-executor` runs the
   rebuild protocol; if `blast_radius` wide & independent →
   `fan-out-implementer` units in worktrees.
6. Exit when dev-loop green ∧ sentinel clean ∧ coverage-of-plan complete.

Depth: Tier 0 = `implementer` + `green-keeper`; Tier 1 = + `test-author` +
`sentinel` + `silent-failure-hunter` (in-loop); Tier 2 = + `test-triage` +
`deep-debugger` on standby + the side-channels.

## Stage 3 — Review fan-out

1. Spawn the **lenses gated by `manifest.review_lenses`** in parallel, each blind
   to the others, each seeing the **diff** not the implementer's reasoning:
   `/code-review` (skill, always), `convention-compliance`, plus the full set at
   Tier 1+ (`test-integrity`, `silent-failure-hunter`, `type-design-analyzer`,
   `pr-test-analyzer`, `comment-analyzer`, `architect-reviewer`). Factor-gated
   regardless of tier: `phi-pii-guardian` on any data/privacy surface,
   `regression-hunter` whenever anchors ≥ 1. `/security-review` runs alongside
   `phi-pii-guardian` when the diff warrants it. Give `test-integrity` the
   Stage-2 test→spec provenance and `regression-hunter` the `predicted_surprises`.
2. As each lens completes, feed its findings to `finding-verifier`
   (refute-by-default; blocker → 2–3 distinct-angle skeptics in parallel; warn →
   1; nit → logged, not verified). For sweep-shaped scope with heavy cross-lens
   overlap, dedup **before** the verifier.
3. Tier 1+: `completeness-critic` loops-until-dry (K=2).
4. `review-synthesizer` produces the **pre-gate package**: verdict, ranked
   verified blockers/warns, nits appendix, **anchors-to-watch (with expected
   values)**, correctness attestation, residual risk.

Route: `fix-first` → back to Stage 2 (bounded ×2 → escalate); `go` → Stage 4.

## Stage 4 — Handoff

Spawn `handoff-assembler`: it wraps `/handoff` + `/changelog` + (`/new-finding`)
— gathering git/gh facts verbatim — and appends the Agent-team pre-gate appendix
(verdict, anchors-to-watch with expected values, residual risk, surviving
predicted surprises, schema-rebuild steps if `change_class ⊇ schema`). Run
`/pr-ready` as the in-loop dry-run of the merge-gate contract before presenting.

**→ HUMAN GATE 2.** VSC-User runs `docs/runbooks/verification.md` independently,
confirms the anchors-to-watch on real data, and merges (or bounces to Stage 2).
Stop. The team does not merge.

**Evidence-gated merge (Sub Project A — `finding-037`).** Once the
`/verify-and-merge` skill (`.claude/commands/verify-and-merge.md`) is in place, a
**future** scope may take the owner-approved evidence-gated path instead of the
manual run above: Claude runs the same protocol through the fail-closed
`genome.verify_gate` core, presents the raw evidence, takes a typed approval, then
squash-merges and closes. The independent human run stays the standing fallback, so
"the team does not merge" holds until that typed approval is given. **Temporal split:**
the Sub-A PR that introduces the skill lands through **this** existing Gate 2 (the
operator merges it by hand); the skill governs the merge of subsequent scopes, not its
own introduction.

## Stage 5 — Close (after VSC-User merges, or after the evidence-gated merge)

1. `knowledge-curator` re-locks the anchors **VSC-User confirmed at the gate**
   into `CLAUDE.md` / `verification.md` / the finding's bedrock table, flips the
   ROADMAP slot, adds cross-links, and appends/flips the scope's `DEC-NNNN`
   `MEMORY.md` rows (insert-then-flip, `genome docs check` clean) — into a
   **reviewable doc change**, never a direct push, human-confirmed numbers only.
2. `repo-sweep` (whole-repo) files residual staleness to the backlog
   (non-blocking), including the **missing-DEC-row** check (`genome docs check`).

This closes the anchor loop: predict (Stage 1) → flag with expected values
(Stage 3) → confirm on real data (gate) → record (Stage 5).

## Depth quick-reference

| Tier | Plan | Implement | Review |
|---|---|---|---|
| 0 | 1 planner + pre-mortem + auditor | implementer + green-keeper | code-review + convention; single verify |
| 1 | 2 planners + light judge + pre-mortem + auditor panel | + test-author + sentinel + silent-failure | full lens set + 3-skeptic verify + loop-until-dry |
| 2 | full panel + per-axis judges + multi-skeptic pre-mortem + auditor panel + architect-reviewer | + test-triage + deep-debugger + schema-executor / fan-out | all lenses + 3-skeptic verify + completeness-critic |

This table is the recalibrated adaptive-depth table (finding-034); the JS
workflows implement the same depths.

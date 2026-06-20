Run the per-scope agent team (finding-034) for one ROADMAP scope item, end to
end, stopping at each of the two human gates. Argument: a scope id (e.g.
`PR-6`), optionally followed by `--from <stage>` to resume.

This command is the **opt-in orchestrator**. The team's members live in
`.claude/agents/*.md` and are usable standalone; this command sequences them
with **adaptive depth** and threads each member's structured JSON output to the
next. It is **model-driven orchestration** — you (the lead session) spawn each
member via the Task tool with the matching `subagent_type`, collect its JSON,
and route per the rules below. You never auto-approve a plan and never merge;
those are the two human gates.

## Operating rules

- **Spawn members via Task** with `subagent_type` = the agent file name. Pass the
  upstream JSON the member needs as its prompt input. Run independent members
  **in parallel** (multiple Task calls in one message): the N planners, the
  per-axis judges, the review lenses, the verifier skeptics.
- **Thread the JSON.** Each stage consumes the prior stage's structured output.
  Keep the manifest as the shared source of truth throughout.
- **Respect the two human gates.** Stop and present to VSC-User after Stage 1
  (plan approval) and after Stage 4 (merge verification). Do not proceed past a
  gate on your own.
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

## Stage 1 — Plan

By tier:
- **Tier 0:** 1 `planner` (minimal-diff) → `plan-premortem` (1) → `plan-auditor`.
- **Tier 1:** 2 `planner`s (minimal-diff + gate-backward) in parallel →
  `plan-judges` (light, all axes) → `plan-synthesizer` → `plan-premortem` (1) →
  `plan-auditor` panel.
- **Tier 2:** full panel of `planner`s (minimal-diff, gate-backward, risk-first,
  convention-purist) in parallel → per-axis `plan-judges` (one per axis, in
  parallel) → `plan-synthesizer` → `plan-premortem` (2–3 skeptics if `deep_T2`)
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
   `/code-review` (skill, always), `convention-compliance`, `phi-pii-guardian`,
   `test-integrity`, `regression-hunter`, plus at Tier 1+ `silent-failure-hunter`,
   `type-design-analyzer`, `pr-test-analyzer`, `comment-analyzer`, and at Tier 2
   `architect-reviewer`. `/security-review` may run alongside `phi-pii-guardian`.
   Give `test-integrity` the Stage-2 test→spec provenance and `regression-hunter`
   the `predicted_surprises`.
2. As each lens completes, feed its findings to `finding-verifier`
   (refute-by-default; blocker → 2–3 distinct-angle skeptics in parallel; warn →
   1; nit → logged, not verified). For sweep-shaped scope with heavy cross-lens
   overlap, dedup **before** the verifier.
3. Tier 2 / "be comprehensive": `completeness-critic` loops-until-dry.
4. `review-synthesizer` produces the **pre-gate package**: verdict, ranked
   verified blockers/warns, nits appendix, **anchors-to-watch (with expected
   values)**, correctness attestation, residual risk.

Route: `fix-first` → back to Stage 2 (bounded ×2 → escalate); `go` → Stage 4.

## Stage 4 — Handoff

Spawn `handoff-assembler`: it wraps `/handoff` + `/changelog` + (`/new-finding`)
— gathering git/gh facts verbatim — and appends the Agent-team pre-gate appendix
(verdict, anchors-to-watch with expected values, residual risk, surviving
predicted surprises, schema-rebuild steps if `change_class ⊇ schema`).

**→ HUMAN GATE 2.** VSC-User runs `docs/runbooks/verification.md` independently,
confirms the anchors-to-watch on real data, and merges (or bounces to Stage 2).
Stop. The team does not merge.

## Stage 5 — Close (after VSC-User merges)

1. `knowledge-curator` re-locks the anchors **VSC-User confirmed at the gate**
   into `CLAUDE.md` / `verification.md` / the finding's bedrock table, flips the
   ROADMAP slot, adds cross-links — into a **reviewable doc change**, never a
   direct push, human-confirmed numbers only.
2. `repo-sweep` (whole-repo) files residual staleness to the backlog
   (non-blocking).

This closes the anchor loop: predict (Stage 1) → flag with expected values
(Stage 3) → confirm on real data (gate) → record (Stage 5).

## Depth quick-reference

| Tier | Plan | Implement | Review |
|---|---|---|---|
| 0 | 1 planner + pre-mortem + auditor | implementer + green-keeper | code-review + convention; single verify |
| 1 | 2 planners + light judge + pre-mortem + auditor panel | + test-author + sentinel + silent-failure | full lens set + 3-skeptic verify + loop-until-dry |
| 2 | full panel + per-axis judges + multi-skeptic pre-mortem + auditor panel + architect-reviewer | + test-triage + deep-debugger + schema-executor / fan-out | all lenses + 3-skeptic verify + completeness-critic |

# Agent-team workflow — per-scope team (Stages 0–5)

This directory implements the per-scope agent team designed in
[`docs/findings/finding-034-agent-team-plan-phase.md`](../../docs/findings/finding-034-agent-team-plan-phase.md).
Each `*.md` file here is one Claude Code subagent (frontmatter + system-prompt
body). The team automates the routing VSC-User does by hand between the five
actors (`CLAUDE.md` → "Working with this codebase"), **without ever replacing
the two human gates** — plan approval and merge verification.

> **Build status.** The agents are usable standalone today (invoke one with the
> Task tool / `subagent_type`). The opt-in orchestration that runs the full
> pipeline lives in [`.claude/commands/scope-run.md`](../commands/scope-run.md).
> Guardrail hooks and the `/new-finding` · `/changelog` · `/pr-ready` authoring
> skills are deferred follow-ups (see finding-034 → "Out of scope").

## The pipeline (segmented by the two human gates)

| Stage | Member(s) | Actor |
|---|---|---|
| 0 · Intake | `scope-dispatcher` → scope manifest + risk-tier | — |
| 1 · Plan | `planner ×N` → `plan-judges` → `plan-synthesizer` → `plan-premortem` → `plan-auditor` | Planning + Verification |
| → | **HUMAN GATE 1: VSC-User approves the plan** | VSC-User |
| 2 · Implement | `interface-freeze` → `test-author` (plan-blind) ∥ `implementer` + `plan-adherence-sentinel` + `green-keeper` (+ `test-triage`, `deep-debugger`, `schema-change-executor`, `fan-out-implementer`) | Development |
| 3 · Review | parallel lenses → `finding-verifier` (refute-by-default) → `completeness-critic` → `review-synthesizer` | Verification + TestingBugs |
| 4 · Handoff | `handoff-assembler` wraps `/handoff` + `/changelog` + the pre-gate package | Development |
| → | **HUMAN GATE 2: VSC-User runs `verification.md`, then merges** | VSC-User |
| 5 · Close | `knowledge-curator` re-locks confirmed anchors; `repo-sweep` triage | — |

The two human gates are **out-of-loop** by design (`verification.md`: *"not
optional and is not negotiable"*). The team is the loop that produced the
change; the gates exist precisely because they run outside it. So the team
produces the *pre-gate package* — it never auto-approves and never merges.

## Adaptive depth — the organizing principle

The dispatcher computes a **risk tier** and the orchestrator spawns only the
members that tier earns. Right-sizing is the point: a docstring batch and a
canonicalize re-lock get different amounts of planning intelligence.

| Tier | Trigger | Plan | Implement | Review |
|---|---|---|---|---|
| **0 · cosmetic/docs** | docs-only; no anchors; no code touched | 1 planner + pre-mortem + auditor | implementer + green-keeper | `/code-review` (light) + convention-compliance; single-vote verify |
| **1 · contained code** | bounded code/CLI; small blast radius; no schema; no anchors moved | 2 planners + light judge + pre-mortem + auditor panel | + test-author + sentinel + silent-failure-hunter | full lens set + 3-skeptic verify + loop-until-dry |
| **2 · schema/pipeline/anchor** | `docs/schemas/` or `ddl/` touched, OR `applicable_anchors` non-empty, OR large blast radius | full panel + per-axis judges + multi-skeptic pre-mortem + auditor panel + architect-reviewer | + test-triage + deep-debugger + schema-change-executor / fan-out-implementer | all lenses + 3-skeptic verify + completeness-critic |

**Pre-mortem fires at every tier** (cheap failure prediction). **Lens gates are
by factor, not by tier** — `phi-pii-guardian` on any data/external/config
surface, `regression-hunter` whenever `anchors ≥ 1`, `test-integrity` whenever
tests are touched, `/code-review` always — so a lens can fire below its
expected tier.

### Risk-tier scoring (the dispatcher computes this)

Conservative by construction — under-tiering is far costlier than over-tiering,
so trip-wires floor the two irreversible risks, ties round up, escalations only
ever raise the tier. Stored in `manifest.risk_breakdown` for auditability.

```
C — change-class (max over touched concerns; +1 if ≥3 distinct code concerns):
    docs 0 · tests 1 · cli 1 · data-backfill 2 · annotation-loader 2 ·
    analysis/insights 2 · pipeline 3 · schema|ddl 4
B — blast radius (|imports_touched|): isolated ≤1→0 · small 2–5→1 · moderate 6–15→2 · large >15→3
P — precedent-surprise (nearest 2–3 precedents): clean 0 · minor/noted 1 · correction-class 2
A — anchor exposure (|applicable_anchors|): none 0 · 1–2→2 · 3+→3   (a within-Tier-2 depth knob)

S = C + B + P
floor = 2 if (schema|ddl touched) OR (|applicable_anchors| >= 1) else 0
tier_from_S = 0 if S==0 · 1 if 1<=S<=4 · 2 if S>=5
tier  = max(floor, tier_from_S)
tier  = min(2, tier + 1) if pre-mortem=probe-first OR manifest.open_questions OR human-bump
deep_T2 = (S >= 7) OR (A >= 3)    # 3 skeptics + completeness-critic + loop-until-dry; else standard T2
```

Calibration bias is explicit: **when unsure, run the deeper tier.** Re-run the
back-test table in finding-034 after any re-weighting — a flipped row means
calibration broke.

## How to run

- **Standalone:** invoke any single member with the Task tool, e.g. ask for the
  `scope-dispatcher` on a ROADMAP slot, or a `convention-compliance` lens on the
  current diff.
- **Full pipeline (opt-in):** run [`/scope-run <scope-id>`](../commands/scope-run.md);
  it computes the tier, spawns the tier-appropriate members in the right order,
  threads each member's structured output to the next, and stops at each human
  gate.

## Conventions for every member

- **Structured JSON output** matching the schema in each member's body, so the
  orchestrator can route, aggregate, and adversarially verify.
- **Read vs write:** Plan-phase and Review-phase members are **read-only**. The
  Stage-2 writers (`implementer`, `test-author`, `schema-change-executor`,
  `fan-out-implementer`) hold `Edit`/`Write`; `test-author`'s writes are
  confined to `backend/tests/` and it is denied the implementation diff.
  `knowledge-curator` is the lone Stage-5 durable-doc writer — post-merge,
  human-confirmed numbers only, via a reviewable change.
- **Independence:** an auditor/verifier is always a *different instance* from
  the producer it grades, ideally seeing only outputs, not reasoning — the
  in-loop analogue of VSC-User's out-of-loop gate.

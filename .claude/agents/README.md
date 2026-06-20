# Per-scope agent team — Plan phase (Stages 0–1)

This directory holds the **Plan-phase slice** of the per-scope agent team designed in
[`docs/findings/finding-034-agent-team-plan-phase.md`](../../docs/findings/finding-034-agent-team-plan-phase.md).
The team automates routing between the repo's five actors (ClaudeCodeVerification,
ClaudeCodeTestingBugs, ClaudeCodePlanning, ClaudeCodeDevelopment, VSC-User) for one unit
of scope — a numbered ROADMAP slot — **without** ever replacing VSC-User's two
independent human gates (plan approval; merge verification).

## Status

| Stages | State |
|---|---|
| **0 · Intake + 1 · Plan** | **Built** (this directory + `../workflows/plan-phase.js`) |
| 2 · Implement · 3 · Review · 4 · Handoff · 5 · Close | **Designed, not built** (see finding-034) |

The Plan phase ends at a **human decision** (VSC-User approves the plan), never at an
auto-approval. The team produces the *pre-gate package*; the gate stays independent.

## Members (all read-only — they produce a plan, not code)

| File | Stage | Role | Model |
|---|---|---|---|
| `scope-dispatcher.md` | 0 | Reads one ROADMAP slot → the **scope manifest** (change_class, blast_radius, anchors, precedent, **risk_tier**, review_lenses, freshness_flags) | sonnet |
| `planner.md` | 1 | One 8-section plan per the CLAUDE.md contract, from an assigned **angle** (run ×N) | opus |
| `plan-judges.md` | 1 | Per-axis scorecard over all candidates (run one per **axis**) | opus |
| `plan-synthesizer.md` | 1 | New plan = winning skeleton + best-of-breed grafts; divergence + merged riskiest-assumptions | opus |
| `plan-premortem.md` | 1.5 | Predicts the implementation/gate surprise (fires at **all tiers**); `proceed`/`revise`/`probe-first` | opus |
| `plan-auditor.md` | 1 | Adversarial contract-compliance grade; independent instance; `ready`/`revise`/`escalate` | opus |

Variant members are **one file run N times** with the variant (`angle` / `axis` /
`lens`) passed in the prompt — the `×N` pattern from finding-034.

## Adaptive depth (the dispatcher's `risk_tier` is the switch)

| Tier | Trigger | Plan depth |
|---|---|---|
| **0 · cosmetic/docs** | docs-only; no anchors; no code | 1 planner → pre-mortem → auditor |
| **1 · contained code** | bounded code/CLI; small blast radius; no schema; no anchors | 2 planners (minimal-diff + gate-backward) → light judge → synthesize → pre-mortem → auditor |
| **2 · schema/pipeline/anchor** | `docs/schemas/`|`ddl/` touched, **or** anchors exposed, **or** large blast radius | full panel (3–4 angles) → per-axis judges → synthesize → 2–3-skeptic pre-mortem → auditor + architecture-fit → divergence escalation |

The exact tier formula lives in `scope-dispatcher.md` (copied verbatim from finding-034
§"Risk-tier scoring") with the PR-history back-test as its own regression check.

## Usage

**Standalone** — invoke any member directly (e.g. via the Task/Agent tool) with the
inputs named in its body. Each returns the structured JSON in its "Output" section, so a
member is useful on its own before the orchestrator runs.

**Orchestrated** — `../workflows/plan-phase.js` chains the members for one scope item
(deterministic dynamic-workflow orchestration: tier-driven parallel fan-out, per-axis
judges, output-shape validation, a bounded ×2 revise loop). Saved dynamic workflows live
in `.claude/workflows/` and run as a command — `/plan-phase PR-6` — with the scope id
arriving via the runtime global `args`. It is **opt-in** — VSC-User triggers the
per-scope run; the agents remain usable standalone. The workflow is intentionally **not**
wired into `settings.json`, and it never auto-approves: the Plan phase ends at VSC-User's
human gate.

> **One runtime caveat.** The dynamic-workflows JS *authoring* API (the exact
> subagent-invocation primitive) is not part of Claude Code's public docs. `plan-phase.js`
> isolates that single call behind one `runAgent()` helper that probes the known primitive
> names and throws a loud, actionable error if none resolve — so adapting to a given
> runtime is a one-line change in one place. The orchestration logic itself (fan-out,
> judging, synthesis, pre-mortem merge, audit verdict, revise loop) is runtime-agnostic
> and `node --check`-clean; it is **not** end-to-end executed here because that primitive
> is environment-provided.

## Guardrails

The cross-cutting guardrails this team relies on live alongside it: `../hooks/*.sh`
(schema-immutability, `git add -A` block, GATE-FILL + CHANGELOG nudges) wired via
`../settings.json`. The Stage-3/4 authoring skills the full design leans on — `/changelog`
(the `[Unreleased]` entry), `/new-finding`, and `/pr-ready` in `../commands/` — are
**designed, not built** (they belong to the Implement/Review/Handoff stages tracked in
finding-034); only `/handoff` exists in `../commands/` today.

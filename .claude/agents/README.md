# Per-scope agent team — full lifecycle (Stages 0–5)

This directory holds the per-scope agent team designed in
[`docs/findings/finding-034-agent-team-plan-phase.md`](../../docs/findings/finding-034-agent-team-plan-phase.md).
The team automates routing between the repo's five actors (ClaudeCodeVerification,
ClaudeCodeTestingBugs, ClaudeCodePlanning, ClaudeCodeDevelopment, VSC-User) for one unit
of scope — a numbered ROADMAP slot — **without** ever replacing VSC-User's two
independent human gates (plan approval; merge verification). A scope item flows: intake →
adaptive-depth plan panel → **Gate 1** → guarded single-writer implementation with a
plan-blind test oracle → fan-out of adversarially-verified review lenses → enriched
handoff → **Gate 2** → post-merge anchor re-lock.

## Status

| Stage | State |
|---|---|
| **0 · Intake** + **1 · Plan** | **Built** (`../workflows/plan-phase.js`) |
| **2 · Implement** + **3 · Review** | **Built** (`../workflows/implement-review.js`) |
| **4 · Handoff** | **Built** (`handoff-assembler.md`) |
| **5 · Close** | **Built** (`../workflows/close.js`) |

Every stage is in-loop; both gates are out-of-loop and human. The team produces the
*pre-gate package* at each gate; the gate stays independent.

## Members

Stages 0–1 + all read-only review members produce analysis, not code. The **writers**
(`Edit`/`Write`) are confined to Stage 2 + the lone Stage-5 durable-doc writer.

**Stage 0–1 · Plan** (all read-only)

| File | Stage | Role | Model |
|---|---|---|---|
| `scope-dispatcher.md` | 0 | Reads one ROADMAP slot → the **scope manifest** (change_class, blast_radius, anchors, precedent, **risk_tier**, review_lenses, freshness_flags) | sonnet |
| `planner.md` | 1 | One 8-section plan per the CLAUDE.md contract, from an assigned **angle** (run ×N) | opus |
| `plan-judges.md` | 1 | Per-axis scorecard over all candidates (run one per **axis**) | opus |
| `plan-synthesizer.md` | 1 | New plan = winning skeleton + best-of-breed grafts; divergence + merged riskiest-assumptions | opus |
| `plan-premortem.md` | 1.5 | Predicts the implementation/gate surprise (fires at **all tiers**); `proceed`/`revise`/`probe-first` | opus |
| `plan-auditor.md` | 1 | Adversarial contract-compliance grade; independent instance; `ready`/`revise`/`escalate` | opus |

**Stage 2 · Implement** (`implementer`, `test-author`, `schema-change-executor`,
`fan-out-implementer` are **writers**; the rest read-only)

| File | Role | Model |
|---|---|---|
| `implementer.md` | Executes approved §4 mechanically; drives blind tests green; STOP+escalate on any surprise | opus |
| `test-author.md` | **Plan-blind** §5 tests from spec + frozen interface; writes only `backend/tests/`; `test→spec` provenance | opus |
| `plan-adherence-sentinel.md` | Write-phase analogue of plan-auditor; flags diff-vs-plan drift; PAUSE+escalate | opus |
| `green-keeper.md` | Holds the dev-loop green; escalates vs weakening a test / touching schema | sonnet |
| `test-triage.md` | Classifies a red test (real/flaky/env/needs-update) + routes | sonnet |
| `deep-debugger.md` | On-demand root-cause for gnarly domain breakages; never weakens a test | opus |
| `schema-change-executor.md` | Rare writer; drives the documented schema-rebuild protocol; FTS5 rule | opus |
| `fan-out-implementer.md` | Worktree-isolated writer for wide independent mechanical breadth only | opus |

**Stage 3 · Review** (all read-only; `/code-review` + `/security-review` skills composed
as additional lenses)

| File | Role | Model |
|---|---|---|
| `convention-compliance.md` · `phi-pii-guardian.md` · `test-integrity.md` · `regression-hunter.md` · `silent-failure-hunter.md` · `type-design-analyzer.md` · `pr-test-analyzer.md` · `comment-analyzer.md` · `architect-reviewer.md` | Parallel review lenses on the fixed diff; each returns falsifiable findings (`refutable_claim`) | opus / sonnet |
| `finding-verifier.md` | **Refute-by-default** adversarial verifier; severity-scaled (blocker→2–3 skeptics) | opus |
| `review-synthesizer.md` | Verified survivors → pre-gate package + anchors-to-watch(expected) + go/fix-first | opus |
| `completeness-critic.md` | Tier 1+ meta-reviewer; gates loop-until-dry on lens/verify/hunk gaps | opus |

**Stage 4–5 + cross-cutting**

| File | Stage | Role | Model |
|---|---|---|---|
| `handoff-assembler.md` | 4 | Wraps `/handoff`+`/changelog`+`/new-finding`; appends the pre-gate package (read-only) | sonnet |
| `knowledge-curator.md` | 5 | **The lone durable-doc writer**: re-locks gate-confirmed anchors post-merge, via reviewable change | opus |
| `repo-sweep.md` | x | Staleness detector (finder, never fixer); dispatcher freshness slice + standalone backlog | sonnet |

Variant members are **one file run N times** with the variant (`angle` / `axis` /
`lens`) passed in the prompt — the `×N` pattern from finding-034.

## Adaptive depth (the dispatcher's `risk_tier` is the switch)

Depths follow finding-034's **"Adaptive depth — recalibrated for correctness"** table
(the governing version; the earlier per-stage tables are superseded — see the finding's
note resolving that contradiction).

| Tier | Trigger | Plan depth | Review depth |
|---|---|---|---|
| **0 · cosmetic/docs** | docs-only; no anchors; no code | 1 planner → pre-mortem → auditor | code-review + convention; single verify |
| **1 · contained code** | bounded code/CLI; small blast radius; no schema; no anchors | 2 planners → light judge → synthesize → pre-mortem → **auditor panel** | **full lens set** + refute-by-default verify + **loop-until-dry** |
| **2 · schema/pipeline/anchor** | `docs/schemas/`|`ddl/` touched, **or** anchors exposed, **or** large blast radius | full panel (3–4 angles) → per-axis judges → synthesize → 2–3-skeptic pre-mortem → auditor panel + architecture-fit → divergence escalation | all lenses + 3-skeptic verify + completeness-critic |

Lens-gating is **by factor, not tier** — `phi-pii-guardian` on any data/external/config
surface, `regression-hunter` whenever anchors ≥ 1, regardless of tier (the dispatcher
factor-gates `manifest.review_lenses`, which the orchestrator honors when present). The
exact tier formula lives in `scope-dispatcher.md` (copied verbatim from finding-034
§"Risk-tier scoring") with the PR-history back-test as its own regression check.

## Usage

**Standalone** — invoke any member directly (e.g. via the Task/Agent tool) with the
inputs named in its body. Each returns the structured JSON in its "Output" section, so a
member is useful on its own before the orchestrator runs.

**Orchestrated — two paths, same members/depth/gates:**

*(a) Model-driven — `/scope-run PR-6` (`../commands/scope-run.md`).* The runnable-today
path: the lead session spawns members via the Task tool and routes by the command's rules,
stopping at each gate (resume with `--from <stage>`). Flexible, non-deterministic, no
dependency on the workflow runtime — use as the default and the fallback.

*(b) Deterministic — three **segmented** dynamic-workflow scripts in `../workflows/`*, split
**by the two human gates** (a single auto-run cannot cross a human decision):

| Script | Stages | Runs | Ends at |
|---|---|---|---|
| `plan-phase.js` | 0–1 | `/plan-phase PR-6` | **Gate 1** (approve plan) |
| `implement-review.js` | 2–3 | `/implement-review PR-6` (+ approved plan in `args`) | **Gate 2** (verification.md · merge) |
| `close.js` | 5 | `/close PR-6` (+ confirmed gate anchors in `args`) | done (anchor loop closed) |

Each is deterministic (tier-driven parallel fan-out, output-shape validation, bounded
revise/fix-first loops ×2), receives input via the runtime global `args`, and is
**opt-in** — VSC-User triggers each per-scope run; the members remain usable standalone
via the Task/Agent tool. None is wired into `settings.json`, and none auto-approves or
auto-merges: the lifecycle ends at VSC-User's two unchanged human gates.

> **One runtime caveat (all three scripts).** The dynamic-workflows JS *authoring* API
> (the exact subagent-invocation primitive) is not part of Claude Code's public docs. Each
> script isolates that single call behind one `runAgent()` helper that probes the known
> primitive names and throws a loud, actionable error if none resolve — so adapting to a
> given runtime is a one-line change in one place. The orchestration logic itself
> (fan-out, tier gating, verify, synthesis, the bounded loops) is runtime-agnostic and
> `node --check`-clean; it is **not** end-to-end executed here because that primitive is
> environment-provided. `/code-review` and `/security-review` are **skills**, not
> subagents — `implement-review.js` composes them as review lenses and surfaces them in
> the package for the operator/runtime to dispatch.

## Guardrails

The cross-cutting guardrails this team relies on live alongside it:

- **Hooks** (`../hooks/*.sh`, wired via `../settings.json`) — schema-immutability block,
  `git add -A` block, GATE-FILL + CHANGELOG commit nudges.
- **Authoring skills** (`../commands/*.md`) — `/handoff` (the contract skeleton),
  `/changelog` (the `[Unreleased]` entry), `/new-finding` (a durable finding), and
  `/pr-ready` (the pre-PR readiness checklist). Stage 4's `handoff-assembler` wraps the
  first three; `/pr-ready` is the in-loop dry-run of the merge-gate contract.

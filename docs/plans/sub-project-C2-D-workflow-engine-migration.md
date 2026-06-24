# Sub Project C2 + D — Workflow-Engine Migration (everything on the engine)

**Status:** Design approved (brainstorming), ready for an implementation plan.
**Date:** 2026-06-24.
**Source:** Brainstorming session on bolstering `/scope-run`.
**Scope:** The capstone. **Absorbs C2** (budget + resumability) and **all of D** (fidelity-gap
wiring + robustness + observability) into a single move: porting the orchestration onto the
real Workflow engine. Chosen at the **maximal** scope — *everything* (`/scope-run`, A, B, B2,
C1) migrates to the engine; the model-driven path is retained as conductor + fallback.
**Touches:** every prior sub-project (A/B/B2/C1) at the orchestration layer; **their Python
cores are unchanged.**

---

## 1. Context — it collapses, it doesn't split

Where B and C decomposed into independent subsystems, **D + C2 are one thing: the faithful
port of the orchestration to the real Workflow engine.** Everything else falls out of that
single move — each is a *native engine feature turned on*, or a fidelity fix made *while
re-expressing each stage*:

| Concern | Delivered by the port as… |
|---|---|
| **Budget** (C2a) | native `budget.total`/`spent()`/`remaining()` — turned on, not built |
| **Resume** (C2b) | native `resumeFromRunId` (cached agent calls) — turned on, not built |
| **Robustness** (D) | `parallel`/`pipeline` resolve a failed agent to `null` (`.filter(Boolean)`); retry = thin wrapper |
| **Observability** (D) | native `/workflows` live progress + `log()` + per-agent journals |
| **Fidelity** (D) | the gaps below, closed while re-expressing each stage |

**The README's blocker dissolves.** The scripts probe for an *"undocumented
subagent-invocation primitive"* and throw if absent — but that primitive **is** documented:
`agent(prompt, {agentType: '<agent-name>'})`, resolving `.claude/agents/*.md` from the same
registry as the Task tool. The 31 `runAgent()` call-sites (plan-phase 10 · implement-review
15 · close 6) all funnel through **one helper**, so the port is mechanical translation +
fidelity fixes, not new invention.

### The honest flag — this reverses a locked decision

finding-034 / the team README deliberately made **model-driven the default** and the JS
workflows **opt-in** ("the default and the fallback… no dependency on the workflow runtime").
"Everything on the engine" **inverts that** — the engine becomes primary. Like A's
independence relaxation and C1's auto-tuning, this is a deliberate, eyes-open call that must
be a **recorded reversal** (a `DEC-NNNN` row + a finding-034 update), with **model-driven
retained as the conductor + headless/cron fallback** — never silent drift.

---

## 2. Locked decisions (from the brainstorm)

| # | Decision | Choice |
|---|---|---|
| 1 | Port scope | **Everything** — `/scope-run`, A, B, B2, C1 migrate to the engine. |
| 2 | Architecture | **Hybrid** — thin model-driven conductor + gate-segmented Workflows + unchanged Python cores. |
| 3 | Human gates | Each becomes a **Workflow boundary** (the engine cannot pause for a human mid-run). |
| 4 | Model-driven path | **Retained** as the conductor and the headless/cron fallback — not deleted. |
| 5 | The reversal | Recorded (`DEC` + finding-034 update): model-driven-default → engine-primary. |
| 6 | Build shape | **Phased** — 3 team workflows first (reference pattern), then A/B/B2/C1. |

---

## 3. The design

### The architectural truth — the engine can't pause for a human

The Workflow tool runs to completion and returns; it cannot block mid-run for a gate. Every
flow here has human gates (scope-run's two, A's approval, B's two touchpoints, B2's
micro-gate + per-sub-scope gates, C1's loosening approval). So the migration makes
finding-034's *"in-loop agents bracketed by out-of-loop human gates"* **literal**:

- **In-loop segments → Workflows** (native budget / resume / observability / robustness).
- **Out-of-loop human gates → a thin model-driven conductor** that launches each
  segment-Workflow, presents its package at the gate, and on approval launches the next.
- **The Python cores** (`verify_gate`, `fast_follow`, `scope_split`, `campaign`,
  `calibration`) **are unchanged** — substrate-agnostic, called from `agent()` steps or
  directly. The A/B/B2/C1 design work slots in untouched; only orchestration migrates.

### The port mapping (applies to every workflow)

| Abstract-runtime script | Real Workflow dialect |
|---|---|
| `runAgent(name, input, role)` helper | `agent(prompt, {agentType: name, schema})` |
| `Promise.all(xs.map(...))` | `parallel(thunks)` / `pipeline(items, …stages)` |
| `requireKeys` / `coerceJson` | the `schema` option (StructuredOutput, validated at the tool layer) |
| `progress(msg)` | `log(msg)` |
| (none) | `export const meta = {…}` (currently absent in all three) |
| (none) | `budget` (depth-scales-to-budget; loop-until-budget) |
| `--from <stage>` (coarse) | `resumeFromRunId` (cached agent calls — fine-grained) |

### Fidelity gaps closed during the rewrite (from the earlier audit)

- **Stage 4** (`handoff-assembler`) wired into the deterministic path (today absent).
- The **listed-not-invoked** Stage-2 agents (`test-triage`, `deep-debugger`,
  `schema-change-executor`, `fan-out-implementer`) actually invoked (or removed from the
  reported `members`).
- **`finding-verifier` severity scaling** (blocker → 2–3 distinct-angle skeptics) implemented.
- **Tier-0 planner angle** + **Tier-2 architect-reviewer** reconciled with `scope-run.md`.

### Native features turned on

- **Budget** — `budget.total`/`remaining()` scale tier depth + fan-out width; the loops
  (revise, fix-first, loop-until-dry) become budget-aware.
- **Resume** — `resumeFromRunId` makes every segment, the **B2 campaign** (13-PR,
  multi-session), and long Tier-2 runs resumable.
- **Observability** — `/workflows` live progress + `log()` narration + per-agent journals.
- **Robustness** — `parallel`/`pipeline` null-on-failure (`.filter(Boolean)`) + a bounded
  retry wrapper for transient agent failures.

---

## 4. Phasing

- **Phase 1 — port the 3 team workflows** (`plan-phase` / `implement-review` / `close`) as
  the **reference implementation** of the conductor + gate-segmented-Workflow + meta/agent/
  parallel/pipeline/schema/budget/resume pattern. Closes the fidelity gaps. Lowest risk,
  highest learning.
- **Phase 2 — migrate A, then B, B2, C1** onto the same pattern, each as a conductor +
  segment-Workflows reusing its existing Python core.

---

## 5. Error / edge handling

- **Engine unavailable** (headless/cron, or an interactively-authenticated MCP absent) →
  fall back to the model-driven conductor path (retained for exactly this).
- **A segment-Workflow dies mid-run** → `resumeFromRunId` re-runs only the uncached agents;
  the conductor re-launches the segment.
- **Human gate between segments** → always handled by the conductor, never inside a Workflow.
- **Budget exhausted mid-run** → the run stops at the budget ceiling and reports partial
  progress (resumable), never silently truncates without `log()`-ing what was dropped.

---

## 6. Testing

- **Per-workflow `node --check`** + a structured-output (`schema`) contract test per `agent()`
  call (the StructuredOutput layer validates shape, replacing `requireKeys`/`coerceJson`).
- **Tier-driven fan-out** tests (the panel/lens sets per tier still match `scope-run.md`).
- **Budget-scaling** tests (depth/width respond to `budget.total`; loop-until-budget
  terminates).
- **Resume** tests (a killed segment resumes with cached agents; same args → 100% cache hit).
- **Fidelity** tests (Stage 4 runs; the four ex-listed-not-invoked agents invoke; verifier
  spawns N skeptics by severity).
- **Conductor** tests (gate boundaries: a segment returns its package, the conductor stops
  for the human, resumes on approval; never auto-crosses a gate).

---

## 7. Resolved defaults

1. **Model-driven retained** as conductor + headless/cron fallback (not deleted).
2. **Phase 1 = the 3 team workflows** as the reference pattern; A/B/B2/C1 follow.
3. **Cores unchanged** — only orchestration migrates.
4. **The reversal recorded** — `DEC` row + finding-034 update (model-driven-default →
   engine-primary, model-driven retained).

---

## 8. Out of scope

- Deleting the model-driven path — it is retained, not removed.
- Changing any Python core's behavior — this is an orchestration-substrate migration only.
- Putting a human gate *inside* a Workflow — impossible by design; gates live in the conductor.

---

## 9. Dependencies & sequencing

- **Phase 1** depends only on the Workflow engine + the existing agents — buildable now.
- **Phase 2** depends on A/B/B2/C1 existing (model-driven) first, *or* builds them directly
  on the engine. Either way the cores come first.
- Effort-wide order: **A → B → C1 → (this migration, Phase 1) → B2-Phase 1 → Phase 2 of the
  migration absorbing A/B/B2/C1 → B2-Phase 2 (campaign, now resumable on the engine).**
- This is the **largest** sub-project; the phasing is what keeps it tractable.

---

## 10. Next step

Move to an implementation plan (the `writing-plans` skill), **Phase 1 first**: port
`plan-phase` / `implement-review` / `close` to the real Workflow dialect (`export const meta`
+ `agent(agentType)` + `parallel`/`pipeline` + `schema` + `budget` + `resumeFromRunId`),
closing the fidelity gaps, with `node --check` + the structured-output contract tests +
budget/resume/fidelity tests as the verification — and record the model-driven→engine
reversal as a `DEC` row + finding-034 update.

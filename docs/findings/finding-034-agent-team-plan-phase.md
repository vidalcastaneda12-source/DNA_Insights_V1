# Agent-team workflow — per-scope team design (Plan + Implementation phases)

## Status

**Design-only brainstorm. Not to be built until the pre-Phase-6 sequence closes**
(see `ROADMAP.md` → "Pre-Phase-6 sequence"). This document is the implementation
brief for a later session that will build the Plan-phase members as
`.claude/agents/*.md` subagents plus an opt-in orchestration workflow. Captured
2026-06-18 during a workflow-design session between VSC-User and
ClaudeCodeVerification / ClaudeCodePlanning. No code was written.

Decisions locked this session:

- The planner uses **Option B** — a judge-panel of *diverse* candidate plans, not a
  single planner.
- The phase runs at **adaptive depth** — three tiers selected by the dispatcher from
  the scope's risk, so a docstring batch and a canonicalize re-lock get different
  amounts of planning intelligence.
- **The pre-mortem runs at Tier 1 and Tier 2** (not Tier 2 only).
- The low-hanging-fruit finder is **`repo-sweep`** (a read-only *detector*), paired
  with `knowledge-curator` (the *fixer*); a narrow freshness slice of it also lives
  inside the dispatcher.
- The two **human gates** (plan approval; merge verification) are **preserved** — the
  team is segmented *by* them and never substitutes for them.
- **Implementation (Stage 2, added 2026-06-19):** the spine is a single `implementer`
  wrapped in guards. Implementation parallelizes *worst* of the three phases, so agent
  fan-out is reserved for genuinely independent mechanical breadth (the opt-in
  ultra-prefixed mode's niche), not the act of writing a coherent change.
- The **`test-author` is plan-blind** — it writes the §5 tests from the approved plan's
  §5/§6 plus a frozen interface contract, **without reading the implementation's
  bodies/logic**, making the suite an independent oracle against the
  "fixtures-shaped-to-the-implementation" failure mode `verification.md` guards.

This finding covers the **Plan phase (Stage 0–1)** and the **Implementation phase
(Stage 2)** — the latter added 2026-06-19. The remaining stages
(review-fanout→handoff→close) will get their own findings as they are designed.

## Context

The repository is built by five actors (`CLAUDE.md` → "Working with this codebase"):
ClaudeCodeVerification, ClaudeCodeTestingBugs, ClaudeCodePlanning,
ClaudeCodeDevelopment, and the human VSC-User. Today VSC-User hand-routes work
between these as separate chats. The "agent team per scope" goal is to **automate
that routing** for one unit of scope (a numbered ROADMAP slot, e.g. "PR 6"), while
keeping VSC-User's independent verification gate exactly where it is.

The Plan phase turns a ROADMAP slot into a plan VSC-User can approve on first read.
Its members map onto existing actors:

- `scope-dispatcher` → *new* (formalizes the plan-mode "read the listed inputs first"
  step into a structured manifest).
- `planner` → **ClaudeCodePlanning**.
- `plan-judges` / `plan-synthesizer` / `plan-premortem` → *new* intelligence layer.
- `plan-auditor` → **ClaudeCodeVerification** (its plan-review function).
- then the **human plan-approval gate** → **VSC-User**.

The design rule a naive "agent team" would break: the merge-verification gate
(`docs/runbooks/verification.md`: *"not optional and is not negotiable"*) exists
precisely because it runs **outside the loop that produced the change**. An agent
team is still that loop. So the team produces the *pre-gate package*; the human gates
remain independent. The Plan phase therefore ends at a human decision, not at an
auto-approval.

## The per-scope team (the larger frame)

For one scope item the full team is a pipeline **segmented by the two human gates**:

| Stage | Member(s) | Actor |
|---|---|---|
| 0 · Intake | `scope-dispatcher` → scope manifest | — |
| 1 · Plan | this document (panel → judges → synth → pre-mortem → auditor) | Planning + Verification |
| → | **HUMAN GATE: VSC-User approves the plan** | VSC-User |
| 2 · Implement | **this document** — `implementer` + plan-blind `test-author` + guards (see "Implementation phase" below) | Development |
| 3 · Review fan-out | parallel lenses on the diff → verify → synthesize | Verification + TestingBugs |
| 4 · Handoff | `/handoff` + `/changelog` (+ `/new-finding`) | Development |
| → | **HUMAN GATE: VSC-User runs `verification.md`, then merges** | VSC-User |
| 5 · Close | `knowledge-curator` re-locks anchors; `repo-sweep` triage | — |

This finding designs Stage 0–2 (Intake, Plan, Implementation).

## Organizing principle — adaptive depth

The most intelligent thing the Plan phase can do is decide **how much planning the
scope earns**. The dispatcher computes a **scope-risk tier** from
`change_class × blast_radius × |applicable_anchors| × precedent-surprise`, and the
tier selects the depth:

| Tier | Trigger (dispatcher-computed) | Example slot | Planning depth |
|---|---|---|---|
| **0 · cosmetic/docs** | docs-only; no anchors; no code touched | PR 8 (cosmetic batch) | 1 planner → auditor. *(pre-mortem optional)* |
| **1 · contained code** | bounded code/CLI; small blast radius; no schema; no anchors moved | PR 6 (genes seed), PR 12 (CLI tests) | 2 planners (minimal-diff + gate-backward) → light judge → synthesize → **pre-mortem (1 agent)** → auditor |
| **2 · schema/pipeline/anchor-moving** | `docs/schemas/` or `ddl/` touched, **or** `applicable_anchors` non-empty, **or** large blast radius | PR 3 / PR 5a class | full panel (3–4 angles) → per-axis judges → synthesize → **pre-mortem (2–3 skeptics)** → auditor → divergence-escalation |

Elegance = the team right-sizes itself. Everything below is the Tier-2 spine; Tier 0
and 1 are subsets. **Per the locked decision, the pre-mortem fires at Tier 1 and
Tier 2** — contained changes have been bitten by surprises too (e.g. an anchor count
moving when the plan assumed it would not), and a single pre-mortem agent is cheap.

## Plan-phase pipeline

```
scope-dispatcher  ──tier 0──────────────────────────────▶ planner → auditor ─▶ [HUMAN]
  │ (+ precedent, reading-list freshness, risk-tier)
  └─tier 1/2─▶ planner ×N  (diverse angles; each self-reports confidence + riskiest assumption)
                    │
                    ▼
              plan-judges (per-axis scorecard, perspective-diverse)
                    │
                    ▼
              plan-synthesizer (winning skeleton + best-of-breed grafts
                                + divergence signal + merged riskiest-assumptions)
                    │
                    ▼
              plan-premortem  (predict the implementation surprise / gate drift)   ← Tier 1 & 2
                    │
                    ▼
              plan-auditor ──ready──▶ [HUMAN: approves plan + sees assumptions,
                    ▲        │                  divergence, predicted surprises]
                    └─revise─┘   (bounded ×2 → escalate to VSC-User)
```

## Plan-phase members (Stage 0–1)

All members are read-only (`Read, Grep, Glob, Bash`-read-only; **no Edit/Write**) —
the Plan phase produces a plan, not code, matching "ClaudeCodePlanning … does not
write code or run commands." Each returns **structured JSON** so the orchestrator can
route, aggregate, and adversarially verify. Physical form: `.claude/agents/<name>.md`
with YAML frontmatter (`name`, `description`, `tools`, `model`) + a system-prompt body.

### 1 · `scope-dispatcher` (Stage 0 — intake)

**Role.** Read one ROADMAP scope slot and emit the *scope manifest* every downstream
member consumes — done once, deterministically, so planner / judges / pre-mortem /
auditor all work from one source of truth instead of each re-deriving context by hand.
Also computes the risk-tier and runs the **reading-list freshness slice** (below).

**Reads.** Scope id (e.g. `PR-6`); `ROADMAP.md` slot; `CLAUDE.md` (locked decisions,
conventions, real-data anchors, never-do); the `docs/findings/` referenced by the
slot; optional in-progress `git diff`.
**Model/effort.** Sonnet / medium — context-heavy retrieval, light classification.

**Output — the scope manifest:**

```jsonc
{
  "scope_id": "PR-6",
  "title": "Minimal genes seed (Option A)",
  "roadmap_slot": "<verbatim slot text>",
  "change_class": ["schema", "cli"],
  "depends_on": ["PR-4", "PR-5"],
  "gated_by": ["Phase-6 entry"],
  "reading_list": {
    "docs": ["CLAUDE.md#locked-decisions", "docs/schemas/schema_group_3_*.md"],
    "findings": ["finding-005#5"],
    "code": ["backend/src/genome/annotate/…"]
  },
  "locked_decisions_in_play": ["#7 supersession", "#8 provenance"],
  "blast_radius": { "imports_touched": ["…"], "tests_covering": ["…"] },
  "applicable_anchors": [ {"name": "gnomad_matches", "value": 2796952, "src": "CLAUDE.md:obs-4"} ],
  "precedent": [ {"finding": "finding-008", "surprise": "Beagle aborts on male non-PAR ploidy"} ],
  "rebuild_required": true,
  "risk_tier": 2,
  "review_lenses": ["convention-compliance", "regression-hunter"],
  "out_of_scope_candidates": ["full genes/traits/pathways dictionaries → Phase 7"],
  "freshness_flags": [],
  "open_questions": []
}
```

**Prompt checklist.** Locate slot → extract deps/gating → classify `change_class`
from slot text + implied files → resolve every finding ref to a real path → list
locked decisions touched → if pipeline/schema, list applicable anchors *with current
values + source lines* → retrieve the 2–3 nearest past findings/PRs and *what
surprised them* (`precedent`) → compute `blast_radius` (grep importers + tests) →
set `rebuild_required` and `risk_tier` → run the freshness slice → flag
`open_questions`. *Return only the manifest.*

**Reading-list freshness slice.** Over *only the files in `reading_list`*, check that
the anchors / ROADMAP statuses / finding cross-refs the plan will rely on are
internally consistent and current — so the team never plans on stale ground. Anything
found goes in `freshness_flags` (it does **not** block; it warns). This is the narrow,
nearly-free placement of `repo-sweep` (member 7).

**Done when.** Manifest validates; every finding ref resolves; `change_class`
non-empty; `risk_tier` set.
**Hands to.** planner, judges, pre-mortem, auditor, and the Stage-3 review fan-out.

### 2 · `planner` ×N (Stage 1 — Option B candidates)

**Role.** Produce the 8-section plan per the `CLAUDE.md` plan-mode contract. Run **N
in parallel, each with a distinct optimization target** so they genuinely explore
different regions of the solution space — diversity by construction, not by label.

**Angles** (dispatcher picks the set by tier):

- **minimal-diff** — smallest change that satisfies the slot; biased to reuse +
  fewest files touched.
- **gate-backward** — derive the plan *backward* from §6: what must be true at the
  real-data gate (the anchors), then what produces it. (This is the angle that would
  have caught PR-5a's "the `DR2 < 0.3` import gate is structurally dead for male
  non-PAR" *before* implementation — see `finding-031`.)
- **risk-first** *(Tier 2)* — assume it goes wrong; front-load the most uncertain
  step, plan verification around failure modes, maximize escalation surface.
- **convention-purist** *(Tier 2)* — optimize for supersession / provenance /
  locked-decision fit over expedience.

**Reads.** The manifest; every file in `reading_list`; the CLAUDE.md plan contract.
**Model/effort.** **Opus, high/xhigh** — the highest-judgment stage.

**Output — maps 1:1 to the 8 sections, plus self-report:**

```jsonc
{
  "scope_id": "PR-6",
  "angle": "gate-backward",
  "reading_list_confirmed": ["…files actually read…"],        // §1
  "problem_statement": "…specific numbers / errors / symptoms…", // §2
  "constraints": ["…locked decisions respected, immutable schema files, no-refactor zones…"], // §3
  "implementation_plan": [ {"step": 1, "detail": "…", "files": ["…"]} ], // §4
  "tests": { "new": ["…"], "must_still_pass": ["…"] },          // §5
  "verification": { "commands": ["…"], "expected_outputs": ["…"], "anchors_to_recheck": ["…"] }, // §6
  "out_of_scope": ["…explicit…"],                               // §7
  "handoff_note": "…",                                          // §8
  "escalations": ["…questions needing VSC-User…"],
  "confidence": 0.0,                  // self-reported
  "riskiest_assumption": "…the single thing most likely to be wrong…"
}
```

**Prompt checklist (embeds the contract verbatim, plus the angle).** Read every
`reading_list` file *before* planning; confirm in §1 · respect every `locked_decision`
and name them in §3 · never plan a `docs/schemas/` or `ddl/` edit except as a flagged
deliberate schema change · implementation must be **mechanical** — any judgment call
outside the code goes to `escalations` and you STOP, don't improvise · `out_of_scope`
explicit · §6 names concrete expected outputs / anchor numbers, never just "tests
pass" · pursue your assigned `angle` to its honest conclusion; report `confidence`
and your single `riskiest_assumption`.

**Done when.** All 8 sections non-empty; `reading_list_confirmed ⊇`
`manifest.reading_list`; no impl step touches an immutable schema file without an
explicit schema-change flag.
**Hands to.** plan-judges.

### 3 · `plan-judges` (Stage 1 — per-axis scoring)

**Role.** Score *all* candidate plans, perspective-diverse: each judge evaluates every
candidate on **one** axis and returns a scorecard — not a single scalar rank, so no
one judge's bias dominates and the comparison stays informative.

**Axes.** correctness · locked-decision fit · verification strength · scope discipline
· blast-radius/risk. (Tier 1 uses a single "light judge" collapsing these.)
**Reads.** All candidate plans; the manifest.
**Model/effort.** Opus / high; one agent per axis, run in parallel.

**Output:**

```jsonc
{
  "scope_id": "PR-6",
  "scores": [
    { "candidate_angle": "gate-backward",
      "by_axis": { "correctness": 4, "locked_decision_fit": 5, "verification": 5,
                   "scope_discipline": 4, "risk": 3 },
      "notes": { "verification": "only angle that re-checks gnomad_matches" } }
  ],
  "axis_winners": { "verification": "gate-backward", "correctness": "minimal-diff" }
}
```

**Done when.** Every candidate scored on every active axis; `axis_winners` populated.
**Hands to.** plan-synthesizer.

### 4 · `plan-synthesizer` (Stage 1 — graft + signals)

**Role.** Produce a **new** plan: the winning skeleton + the best individual section
from each loser (the `axis_winners` graft — risk-first often has the best §6 even
when its §4 lost). Also computes the two ensemble signals.

**Signals:**

- **Divergence-as-escalation.** Where the planners *agree*, confidence is high. Where
  they *diverge* — e.g., they split on whether a schema change is needed — that
  variance **is** an open question for VSC-User; it auto-populates the escalation list.
- **Merged riskiest-assumptions.** Collect every candidate's `riskiest_assumption`
  into one list the human sees *first*.

**Reads.** All candidates; the judge scorecard.
**Model/effort.** Opus / high.

**Output:**

```jsonc
{
  "scope_id": "PR-6",
  "synthesized_plan": { /* same 8-section shape as a planner output */ },
  "graft_provenance": { "verification": "from risk-first", "skeleton": "gate-backward" },
  "divergence": [ {"on": "schema change needed?", "split": "2 yes / 1 no", "→": "VSC-User"} ],
  "riskiest_assumptions": ["…", "…"],
  "panel_confidence": 0.0
}
```

**Done when.** Synthesized plan complete; divergence + assumptions surfaced.
**Hands to.** plan-premortem.

### 5 · `plan-premortem` (Stage 1.5 — failure prediction) · Tier 1 & 2

**Role.** Assume the synthesized plan was executed. Predict the *implementation
surprise* — the number that drifts, the schema assumption that breaks, the hidden
coupling that bites — **before** it happens. The plan-mode contract says surprises
mean "the plan missed something"; this agent finds the miss at plan time.

**Distinct from `plan-auditor`.** Auditor asks *"does this comply with the contract?"*
(backward, mechanical). Pre-mortem asks *"how does this fail at the real-data gate?"*
(forward, adversarial). The auditor would pass PR-5a's plan; the pre-mortem,
consulting `finding-008`, would flag "this approach meets the male non-PAR ploidy
wall." The pre-mortem is where the dispatcher's `precedent` pays off — it applies past
surprises to the current plan.

**Reads.** Synthesized plan; manifest `precedent` + `applicable_anchors`; the cited
findings; relevant code.
**Model/effort.** Opus / high. **Tier 1:** one agent. **Tier 2:** 2–3 skeptics with
distinct lenses (anchor-drift / schema-assumption / hidden-coupling).

**Output:**

```jsonc
{
  "scope_id": "PR-6",
  "predicted_surprises": [
    { "what": "gwas_matches moves", "mechanism": "rsID merge re-points an aliased id",
      "evidence_finding": "finding-025", "likelihood": "med", "early_warning": "watch the +63 delta" }
  ],
  "anchors_at_risk": ["gwas_matches"],
  "recommend": "proceed" | "revise" | "probe-first"
}
```

`probe-first` is the PR-5a precedent (`finding-029`): run a probe *during planning*
before committing the mechanic.
**Done when.** Recommendation emitted; each predicted surprise carries a mechanism +
evidence ref.
**Hands to.** plan-auditor (carrying the predicted failure modes).

### 6 · `plan-auditor` (Stage 1 — contract compliance)

**Role.** Adversarially grade the plan against the manifest + contract *before* it
reaches VSC-User. **Must be a separate instance from any planner, ideally seeing only
the plan, not its reasoning** — the audit is independent for the same reason
VSC-User's gate is. For high-stakes slots, run 2–3 skeptics with distinct lenses and
merge.

**Reads.** The synthesized plan; the pre-mortem output; the manifest; CLAUDE.md
(contract + decisions); the repo (to verify cited files/findings exist and that the
reading list covers what the plan touches).
**Model/effort.** Opus / high — at least as strong as the planner; independence over
economy.

**Output:**

```jsonc
{
  "scope_id": "PR-6",
  "verdict": "ready" | "revise" | "escalate",
  "section_completeness": { "problem_statement": "ok", "verification": "weak", "…": "…" },
  "reading_list_coverage": { "plan_touches": ["…"], "covered": false, "gaps": ["…"] },
  "locked_decision_check": [ {"decision": "#7", "respected": true, "note": "…"} ],
  "findings": [
    { "severity": "blocker" | "warn" | "nit",
      "category": "missing-section" | "reading-list-gap" | "locked-decision-risk"
                | "scope-creep" | "weak-verification" | "untested-path" | "schema-immutability-risk",
      "detail": "…", "evidence": "file:line | manifest ref", "suggested_fix": "…" }
  ]
}
```

**Prompt checklist (default to skepticism; you grade, you don't improve).**
(1) All 8 sections substantive (not placeholder)? (2) **Reading-list coverage** —
cross-check every file `implementation_plan` will touch against
`reading_list_confirmed`; flag any edited-but-unread file. (3) **Locked-decision
compliance** — each `locked_decisions_in_play`; flag schema-immutability risks
hardest. (4) **Verification adequacy** — §6 names concrete expected outputs / anchors?
If `applicable_anchors` non-empty, does §6 re-check them? (5) **Test coverage** —
every §4 behavior change has a matching §5 test? (6) **Scope discipline** —
`out_of_scope` explicit? Any §4 step outside the slot? (7) **Escalation completeness**
— any judgment call buried in §4 that should be an escalation? (8) Incorporate the
pre-mortem: if it said `probe-first`, the plan must include the probe or escalate.

**Done when.** Verdict emitted; every `blocker` carries evidence + a suggested fix.
**Hands to.** `ready` → human plan-approval gate · `revise` → back to planner(s) with
findings · `escalate` → VSC-User.

### 7 · `repo-sweep` (cross-cutting — staleness detector)

**Role.** Detect stale / inconsistent / now-actionable items; rank by
confidence × leverage; cap; hand to triage. **Finder, never fixer** — read-only,
proposes ranked candidates with evidence, never edits (the supersession /
deliberate-change culture forbids auto-editing schema/findings anyway).

**Two homes.** (a) the **freshness slice** folded into `scope-dispatcher` (narrow:
only the reading-list files, prevents planning on stale ground); (b) a **standalone**
`/repo-sweep` run *between* scope items or on a `schedule` (broad: whole-repo).

**Reads.** `CLAUDE.md` · `ROADMAP.md` · `verification.md` · `docs/findings/**` ·
`CHANGELOG.md` · git history · CLI↔docs cross-refs.
**Model/effort.** Sonnet / medium — cheap by design.

**Detects (examples grounded in this repo).** cross-doc anchor drift (a re-locked
number updated in `finding-020` but not `CLAUDE.md`); lagging ROADMAP statuses (a
`[ ]` slot that actually merged); dangling `[[finding]]` links; dead CLI references in
docs (a renamed `genome …` subcommand still cited); surviving `GATE-FILL` markers;
deferred items whose gating signal has fired (a "do X after dbSNP loads" where dbSNP
has loaded); missing `CHANGELOG` `[Unreleased]` entries.

**Output:**

```jsonc
{
  "fruit": [ { "kind": "anchor-drift", "location": "CLAUDE.md:obs-4 vs finding-020",
               "evidence": "…", "confidence": 0.9, "fix_effort": "low",
               "suggested_action": "…" } ],
  "maybe": [ "…lower-confidence hits…" ],
  "skipped_count": 0,
  "scanned": ["…"]
}
```

It `log`s what it dropped (no silent truncation), feeds the backlog, and **never
blocks a gate**.
**Relationship to `knowledge-curator`.** These are two halves of one capability —
**`repo-sweep` detects, `knowledge-curator` repairs** (under supersession, post-merge
at Stage 5). Finder proposes; curator + human dispose. Kept separate so detection
never silently mutates a durable doc.

## The revise loop and the human gate

A bounded **self-repair loop** (planner ↔ auditor) makes the plan reach the human
already-clean. The cap matters: two failed cycles means a judgment call the agents
cannot resolve, which *is* an `escalate`, not a third retry. The human plan-approval
gate stays exactly where it is; the team's job is to make what arrives there worth
approving on first read.

What the human receives at the gate is therefore richer than a bare 8-section plan:

- the synthesized plan;
- its **merged riskiest-assumptions** (seen first);
- the points the team **could not converge on** (`divergence` → open questions);
- the failures the team **predicts** (`plan-premortem.predicted_surprises`), including
  any `probe-first` recommendation.

## Intelligence multipliers (why Option B beats one planner)

1. **Diversity by construction** — distinct optimization targets, not cosmetic labels.
2. **Per-axis judging** — a scorecard, not a scalar rank; no single judge dominates.
3. **Synthesis with grafting** — a new plan that takes the best section from each
   candidate, strictly beating pick-one.
4. **Divergence-as-signal** — the ensemble's variance becomes the human's open-question
   list (nearly free; you already have N plans).
5. **Confidence + riskiest-assumption self-report** — surfaces epistemic state.
6. **Precedent grounding** — the 32-finding system consulted at plan time so the panel
   learns from history instead of re-discovering it; the pre-mortem applies it.

## Implementation phase (Stage 2)

Implementation is the one phase where the agents **write**, so the optimization flips.
Planning *explores* (diverse candidates); review *fans out* (independent lenses); but
implementation must **converge into one coherent change**. So the Stage-2 members are
mostly **guards and scaffolds around a single `implementer`**, not a creative swarm —
and, honestly, implementation parallelizes *worst* of the three phases: you cannot
split "write a coherent feature" across agents without interface drift. Agent fan-out
helps only for genuinely independent **mechanical breadth** (a sweep across N files),
which is exactly the niche the opt-in, ultra-prefixed implementation mode fills.

**Inputs from the Plan phase.** The approved plan (§4 implementation, §5 tests, §6
verification); `plan-premortem.predicted_surprises` (→ the sentinel's and debugger's
watchlist, so a predicted failure is recognized instantly rather than rediscovered);
and the manifest (`risk_tier`, `change_class`, `blast_radius`).

### Stage-2 pipeline

```
[HUMAN: plan approved]
      │  approved plan  +  pre-mortem predicted_surprises  +  manifest
      ▼
interface-freeze — implementer declares public signatures / CLI / columns as skeleton
      │            stubs (or the plan already pins them); this unblocks the blind test-author
      │
      ├───────────────────────────► test-author  (PLAN-BLIND)
      │                               writes §5 tests from §5/§6 + the frozen interface;
      │                               never reads the implementation bodies/diff  (tests start RED)
      ▼                                       │
implementer  (writes §4)  ◄─────────────────┘
      │   …diff watched in real time by ▼
      │     plan-adherence sentinel — diff vs plan: unlisted file / new dep /
      │            │                   schema-touch / scope-creep / predicted-surprise firing
      │            └── drift → PAUSE + escalate to VSC-User   ("surprise ⇒ the plan missed something")
      ▼
┌─ green loop ───────────────────────────────────────────────────────────┐
│ dev-loop green-keeper: pytest · ruff check · ruff format --check ·       │  red (real) → test-triage
│   mypy --strict backend/src.  implementer fills bodies until the blind   │     → deep-debugger (on-demand)
│   tests go green; green-keeper holds the floor.                          │
│   a green-fix needing a weakened test / a schema touch → ESCALATE        │
└──────────────────────────────────────────────────────────────────────────┘
      │  exit when:  dev-loop green  ∧  sentinel clean  ∧  coverage-of-plan complete
      ▼
[→ Stage 3: review fan-out]

manifest-gated side-channels:
  • change_class ⊇ schema  → schema-change executor runs the documented rebuild protocol
  • blast_radius wide & independent → fan-out implementer (worktree-isolated) = the ultra-mode niche
```

### Adaptive depth (same tiers as the Plan phase)

| Tier | Stage-2 member set |
|---|---|
| **0 · cosmetic/docs** | `implementer` + `green-keeper` |
| **1 · contained code** | + plan-blind `test-author` + `plan-adherence sentinel` |
| **2 · schema/pipeline** | + `test-triage` + `deep-debugger` on standby; `schema-change executor` if `change_class ⊇ schema`, `fan-out implementer` if `blast_radius` wide; pre-mortem watchlist wired into the sentinel |

### Member: `test-author` (plan-blind) — the deep dive

**Role.** Write the scope item's §5 tests from the *approved plan's* §5 (tests to add)
and §6 (verification / expected outputs), **without reading the implementation diff
produced this session**. The blindness is the entire point: tests authored from the
spec rather than from the code are an independent oracle, structurally preventing the
"fixtures shaped to match the implementation rather than the source" failure mode that
`docs/runbooks/verification.md` exists to catch.

**The independence contract — blind to *logic*, sighted on *interface*.** A test must
still `import`, call the right function, and reference the right table/column to run at
all, so the author cannot be blind to the *public contract*. The rule is precise:

- **Reads:** the approved plan (§2 problem statement, §5 tests, §6 expected outputs);
  the **frozen `interface_contract`** (public signatures / CLI command names / table &
  column names — from the plan or a stub pass); the *existing* test suite (for
  conventions, fixtures, style — fixture realism per `finding-013`); the schema docs;
  cited findings (for documented edge cases + anchor numbers).
- **Does NOT read:** the implementation diff this session — the function *bodies*, the
  logic, the actual returned values. It tests the *specified* behavior, not the
  *written* behavior.

If it read the bodies it would (consciously or not) assert whatever the code does, bugs
included. Blindness keeps every assertion anchored to the spec.

**Interface-freeze resolves the tension.** Because the author needs the interface but
not the logic, Stage 2 opens with a tiny **interface-freeze**: the `implementer` (or
the plan itself) declares the public signatures / CLI / columns as skeleton stubs. The
test-author writes against that frozen surface, blind to the bodies the `implementer`
then fills. This is what makes "blind" buildable rather than a slogan.

**Red-green protocol.** Default **test-first**: the tests are authored from the plan and
start **red**; the `implementer` drives them green. Fall back to **test-parallel**
(author + implementer concurrent in separate worktrees, joined at the green loop) only
when the interface is still fluid at plan time.

**Tools.** `Read, Grep, Glob, Bash` + **`Write`/`Edit` confined to `backend/tests/`** —
it is a writer, but only of test files; it is explicitly **denied the implementation
diff** as an input.
**Model/effort.** Opus / high — deriving correct assertions from a spec is real judgment.

**Output:**

```jsonc
{
  "scope_id": "PR-6",
  "authored_from": { "plan_sections": ["§5", "§6"], "interface_contract": "…frozen signatures/CLI/columns…" },
  "blind_to": "implementation diff (bodies / logic / actual return values)",
  "tests": [
    { "path": "backend/tests/test_….py", "name": "test_…",
      "asserts": "…the specified behavior / §6 expected output / anchor…",
      "from": "plan §6 expected: gnomad_matches re-checked",
      "kind": "unit | integration | fixture | property | regression-anchor" }
  ],
  "fixtures_added": ["…"],
  "coverage_of_plan": { "behaviors_in_§4_§5": 7, "behaviors_with_a_test": 7, "gaps": [] },
  "independence_attestation": "did not read the implementation diff; asserted against the frozen interface + plan",
  "expected_initial_state": "red — N failing; implementer drives green"
}
```

**Prompt checklist.**
- Read ONLY the allowed inputs above. **Do not request or read the implementation diff.**
- One test per behavior named in §4/§5; one assertion per §6 expected output (including
  any anchor re-check the plan calls for).
- Assert the **specified** value. If §6 doesn't pin a value you'd otherwise have to read
  the code to know, that's a **plan gap → escalate**; never reverse-engineer the
  expected value from the implementation.
- Match the suite's conventions (pytest; realistic fixtures per `finding-013`; no
  `print`; fully type-annotated; `ruff`/`mypy`-clean).
- Stamp each test's provenance (`from: plan §…`) so the gate can trace **test → spec**.
- Attest independence; report the expected initial red state + `coverage_of_plan`.

**Done when.** Every §4/§5 behavior has a test; every §6 expected output is asserted;
independence attested; the suite runs (red is expected pre-implementation).
**Hands to.** `implementer` (drives green) + `green-keeper` (holds green). At the merge
gate the **test → spec** provenance is what lets Stage-3 `test-integrity` prove no test
was later bent to the implementation — defense in depth with VSC-User's out-of-loop run.

**The failure mode it defends (grounded).** `verification.md`: *"test mutation (e.g.
fixtures shaped to match the implementation rather than the source)."* A plan-blind
author cannot shape fixtures to an implementation it never saw. Combined with the
existing independent human gate you get two independent layers: tests decoupled from
code at author time, and a human run decoupled from the whole loop at merge time.

### Other Stage-2 members (specced briefly)

**`implementer`** (the spine; → ClaudeCodeDevelopment). Executes the approved §4
mechanically. `Read/Grep/Glob/Bash` + `Edit/Write`; Opus/high. On any surprise the plan
didn't cover → **STOP + escalate** (the contract), don't improvise. Drives the blind
tests green. Hands to: the green loop → Stage 3.

**`plan-adherence sentinel`** (write-phase analogue of `plan-auditor`; **read-only**).
Monitors the in-progress diff against the approved plan + manifest. Flags: a file §4
didn't list, an undeclared dependency, any `docs/schemas/` or `ddl/` touch, scope creep,
a `predicted_surprise` materializing. Output `{ drift: [{kind, evidence, severity}],
verdict: "on-rails" | "escalate" }`. The *hard* rules (schema-immutability, `git add
-A`) belong to **hooks**; the sentinel handles the judgment calls. Drift → PAUSE +
escalate to VSC-User.

**`dev-loop green-keeper`** (read-mostly; may run formatters, not edit logic). After each
change runs `pytest · ruff check · ruff format --check · mypy --strict backend/src`;
reports crisply; keeps green. **Escalates instead of improvising** if the only path to
green is weakening a test assertion or touching schema. Output `{ loop: {pytest,
ruff_check, ruff_format, mypy}, blocked_by?, escalate? }`.

**`test-triage`** (→ ClaudeCodeTestingBugs; read-only). On red, classifies *real
regression / flaky / environment skew / test-genuinely-needs-update* and routes —
mirroring `verification.md`'s "classify before you fix" rule. Output `{ class, evidence,
route }`.

**`deep-debugger`** (on-demand). For the gnarly domain breakages (DuckDB FK-on-delete,
Beagle ploidy walls, the two-transaction split). Spun up only when green-keeper + triage
can't resolve. Root-causes, proposes the minimal fix, and never weakens a test to pass.

**`schema-change executor`** (writer; rare). Only when `change_class ⊇ schema`. Drives
the documented protocol exactly: edit schema markdown → re-extract `ddl/*.sql` → `rm -rf
data/ && genome init` → re-ingest per the runbooks. Must **not** "fix" an FTS5 failure by
removing `notes_fts` (never-do list). Output: the rebuild log + the re-ingest anchor
check.

**`fan-out implementer`** (writer; worktree-isolated; situational). Replaces the single
implementer **only** for wide, independent, mechanical scope (a cross-loader sweep, a
multi-table backfill, the "≈684 duplicates across five mechanisms" style of work). Each
unit runs in its own git worktree (`isolation: 'worktree'`) so parallel writers don't
collide; gated by `manifest.blast_radius`. This is the managed niche of the opt-in,
ultra-prefixed mode — the same fan-out, orchestrated.

## Build notes (for the implementation session)

- **Physical form.** One `.claude/agents/<name>.md` per member (frontmatter:
  `name`, `description`, `tools: Read, Grep, Glob, Bash`, `model`). System-prompt body
  = the role + prompt-checklist above.
- **Structured output.** When orchestrated via the `Workflow` tool, pass each member's
  output schema as the `schema` option (validation + retry at the tool layer). When run
  standalone, instruct the member to return JSON matching the shape.
- **Orchestration substrate.** The PR lifecycle is *fixed* (plan → implement → review →
  handoff → gate), so prefer **deterministic orchestration** (the `Workflow` tool:
  parallel fan-out, per-axis judges, adversarial-verify, resumable) over model-driven
  routing. It is **opt-in** — VSC-User triggers the per-scope run; the agents remain
  usable standalone before the orchestrator exists.
- **Independence.** `plan-auditor` and `plan-premortem` must be *different instances*
  from the planners, ideally seeing only plan outputs, not planner reasoning — this is
  the in-loop analogue of VSC-User's out-of-loop gate.
- **Read vs write.** Every Plan-phase member is read-only. In Stage 2 the *writers*
  (`implementer`, `test-author`, `schema-change executor`, `fan-out implementer`) hold
  `Edit`/`Write`; the *monitors* (`plan-adherence sentinel`, `test-triage`) stay
  read-only, and the `green-keeper` may run formatters (`ruff format`) but not edit
  logic. The plan-blind `test-author` must additionally **not** be handed the
  implementation diff — confine its writes to `backend/tests/`.
- **Adaptive depth.** Tier 0/1/2 select which members spawn; the dispatcher's
  `risk_tier` is the switch. Calibrate the tier formula on the first few real runs.

## Out of scope for this doc / follow-ups

- The remaining per-scope team stages (**review fan-out**→handoff→close) — each gets its
  own finding; Stage 0–2 are now designed above. The Stage-3 review fan-out is the
  next-highest-value brainstorm (parallel lenses on the diff: `convention-compliance`,
  `phi-pii-guardian`, `test-integrity`, `regression-hunter`, existing `/code-review`) —
  and the `test-author`'s **test → spec** provenance is what lets `test-integrity` there
  verify no test was later bent to the implementation.
- The converged agent build-set discussed alongside this design
  (`regression-hunter` / drift-sentinel, `phi-pii-guardian`, `convention-compliance`,
  `verification-scoper`, `knowledge-curator`) plus the guardrail hooks
  (schema-immutability, `git add -A` block, `GATE-FILL` stop check, CHANGELOG nudge)
  and authoring skills (`/new-finding`, `/changelog`, `/pr-ready`).
- The exact **risk-tier scoring formula** — to be calibrated on real runs.
- **Candidate cross-examination** (each planner critiques the others' plans) — an
  escalation-only pattern reserved for the rare slot where even Tier 2 diverges hard;
  not baked in (it overlaps the pre-mortem and is expensive).

**Conclusion (two lines).** The Plan phase is an adaptive-depth judge-panel: a
dispatcher manifests the scope and its precedent, N diverse planners produce
candidates that are judged per-axis and synthesized, a pre-mortem predicts the gate
surprise (Tier 1 & 2), and an independent auditor gates a bounded revise loop — all
read-only, all ending at VSC-User's unchanged human approval. Build after the
pre-Phase-6 sequence closes.

**Conclusion — Implementation phase (Stage 2).** Implementation is the write phase, so it
optimizes for *fidelity and containment*, not exploration: a single `implementer`
executes the approved §4, a **plan-blind `test-author`** writes the §5 tests from the
spec (never the code) as an independent oracle, a `plan-adherence sentinel` flags any
drift from the plan in real time, and a `dev-loop green-keeper` holds pytest/ruff/mypy
green — escalating rather than weakening a test or touching schema. Fan-out
(worktree-isolated; the ultra-mode niche) is reserved for genuinely independent
mechanical breadth. Same adaptive-depth tiers; same unchanged human gate at merge.

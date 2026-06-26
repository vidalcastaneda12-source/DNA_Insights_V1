# Sub Project B2 — Scope-Split (smart-cut + orchestrated)

**Status:** Phase 1 (smart-cut detector + Stage-0.5 micro-gate) implemented and merged (PR #106, 2026-06-25); durable record: finding-039. Phase 2 (genome.campaign orchestrator) deferred pending Sub Project C. This plan artifact is transient (see DEC-0084).
**Date:** 2026-06-24.
**Source:** Brainstorming session on bolstering `/scope-run`.
**Scope:** The **intake** dual of the fast-follow loop — split a too-big scope into ordered
sub-scopes. Sibling spec to **Sub Project B** (the fast-follow drain loop). Designed at the
**most ambitious** point on both dials: **smart-cut** detection + **orchestrated** execution.
**Depends on:** the dispatcher's existing call-graph machinery; `/scope-run` (the inner
engine). **Phase 2 (orchestration) leans on Sub Project C's resumability.**

---

> **Currency note (2026-06-26 repo sweep):** kept as the live design for the still-unbuilt
> **Phase 2 (`genome.campaign`)** — its state machine, adaptive re-validation, and resumability
> are sole-sourced here (finding-039 + the ROADMAP slot carry only the one-line summary). One
> update since the 2026-06-24 brainstorm: C2+D (`DEC-0099`) made the dynamic-workflows **engine**
> the primary orchestration path, so Phase-2 wiring should target the engine, not the
> "model-driven path" §4 assumes — the campaign design itself is substrate-independent and carries
> over. Prune this plan once Phase 2 ships (then finding-039 + the code are the record).

## 1. Context — where this fits

Fast-follow (B) drains **too-small** leftovers at the *end* of the pipeline. B2 is its dual
at the *front*: when `/scope-run PR-X` is invoked and the scope is really several PRs, B2
proposes a decomposition instead of ramming it through one giant Tier-2 run.

**The structural insight — B2 is the twin of B.** Both are **outer loops over an inner
gated engine**:

- B (fast-follow) loops over **A** (`/verify-and-merge`) — many *small* batches, each through A's gate.
- **B2 loops over `/scope-run`** — many *sub-scopes* of one *big* scope, each through scope-run's two gates.

**The dispatcher already half-does it.** The manifest carries `change_class` as an *array*,
`depends_on`, and — critically — **`out_of_scope_candidates`** (it already names separable
slices to defer). B2 formalizes that into a real split proposal + a gate + (Phase 2) an
orchestrator.

**The manual prototype** is ROADMAP's **13-PR pre-Phase-6 sequence** — a hand-authored
decomposition into ordered PRs with dependency gates ("Backfills cluster… gated on the
loaded dbSNP build"). B2 automates proposing such cluster-decompositions.

### The governing principle — detect *separability*, not *size*

The trap to avoid. In the risk-tier back-test, **PR 3 (canonicalize, S=8)** and **PR 5a
(chrX, S=7)** are the biggest, hardest scopes — and were *correctly* shipped as single PRs,
because they are **big but atomic** (you cannot split canonicalization in half). A naive
"split anything high-risk" rule would be wrong. **B2 splits only when a scope is big *and*
separable** — weakly-coupled clusters / independently-shippable `change_class`es — never
because the risk score is high.

### The two dials (and the choice made)

B2's ambition is a 2×2, not a ladder:

- **Detection quality:** lean (existing manifest signals) → **smart-cut** (import-graph
  clustering for low-coupling cut lines). **Chosen: smart-cut.**
- **Execution autonomy:** advisory (propose; human runs each) → **orchestrated** (a campaign
  manager that sequences the sub-scope runs). **Chosen: orchestrated.**

Note: orchestration **cannot remove the two human gates** (each sub-scope still needs
plan-approval + merge-verification). It sequences, tracks, and tees-up — it does not
automate across a gate. Everything stays advisory at the human boundary.

---

## 2. The design

### Architecture

Mirrors A/B (thin orchestration + testable `genome.*` cores + reuse):

- **`genome.scope_split`** (Python, testable) — the smart-cut detector.
- **`genome.campaign`** (Python, testable) — the campaign state machine.
- **A thin Stage-0.5 hook in `/scope-run`** (the split check) + a `/campaign`
  resume/status surface.
- **Reused:** the dispatcher's existing **LSP call-graph** blast-radius machinery (the graph
  smart-cut needs already exists; git-grep fallback), `/scope-run` (the inner engine),
  ROADMAP (the human-readable view).

### Part 1 — Smart-cut detection (`genome.scope_split`)

1. **Build the coupling graph** over the footprint: nodes = touched modules
   (`blast_radius.imports_touched` + the slot's implied targets), edges = import/call
   coupling (LSP call-graph; git-grep fallback).
2. **Cluster into weakly-coupled components** — high intra-cluster, low inter-cluster
   coupling. Each cluster = a candidate sub-scope; the low-coupling bridges = the cut lines.
3. **Refine by separability signals the manifest already has** — `change_class` boundaries
   (schema vs. loader vs. pipeline are separable *and* ordered), `applicable_anchors`
   clusters, the existing `out_of_scope_candidates`.
4. **The atomic guard (the PR-3/PR-5a rule)** — one tightly-coupled blob → return
   `{atomic: true}`, do **not** split.
5. **Order the sub-scopes** by topological sort over cut-edge direction + `change_class`
   (schema first) + `depends_on`. A dependency cycle between clusters → merge or flag.
6. **Quality gate** — propose a split only if the cut is *clean*: low cut-cost, each
   sub-scope meaningfully smaller, and the decomposition lowers the max tier (one deep-T2 →
   several T1s). A poor best-cut → report `atomic`. **Never propose a bad split.**
7. **Output** — `{atomic:true}` or `{sub_scopes:[mini-manifest…], order, cut_quality}`, each
   sub-scope a full mini-manifest (change_class, est. blast_radius, anchors, re-scored
   risk_tier, deps) so each proposed PR's shape and tier is visible.

### Part 2 — Orchestration (`genome.campaign`)

A **campaign** is a new persistent object: an ordered, dependency-aware set of sub-scopes
driven through `/scope-run`. It **cannot remove the two human gates**; it sequences, tracks,
and tees-up.

- **State** — a structured campaign ledger (source of truth) + a ROADMAP reflection (human
  view). Each sub-scope: `{id, status ∈ pending|ready|planning|implementing|merged|moot|ejected,
  deps, origin_scope, manifest_snapshot}`.
- **The loop** — pick the next *ready* sub-scope (deps merged) → **re-validate it**
  (re-dispatch: still needed? still the right shape?) → `/scope-run <sub-scope>` → stop at
  Gate 1 (human approves plan) → Stage 2-3 → stop at Gate 2 (= A's `/verify-and-merge`) → on
  merge, advance + tee up the next ready one → repeat until all `merged`/`moot`.
- **Adaptive re-validation (the PR-7-is-moot case)** — ROADMAP literally has *"PR 7 —
  re-scope first; may be moot against the live DB."* So before each sub-scope runs, the
  campaign re-dispatches it and surfaces a **re-scope checkpoint** if it is now moot (→
  `moot`, skip), changed (→ re-propose), or grown (→ re-split, capped at one level then
  escalate). The campaign is adaptive, not a fixed script.
- **Multi-session resumability** — campaign state persists; `/campaign resume` /
  `/campaign status` pick up where you left off. (Overlaps Sub Project C's resumability — the
  campaign is its first real consumer.)

### End-to-end data flow

`/scope-run PR-X` → dispatcher manifest → **Stage 0.5: smart-cut check** → if `atomic`,
normal scope-run (unchanged) → if a clean split, **🚦 pre-plan micro-gate** ("PR-X is really
these 4 ordered PRs — approve / edit / run-as-one") → on accept, create a **campaign**, write
the sub-scopes to ROADMAP, and start the orchestrator loop → each sub-scope flows through its
own normal `/scope-run` (two gates each) → campaign advances on each merge → done when all
`merged`/`moot`.

### Error / edge handling

- **No clean cut** → `atomic`, run as one (never force a bad split).
- **Dependency cycle between clusters** → merge them or flag "not cleanly separable here."
- **Mid-campaign drift** → per-sub-scope re-validation catches moot/changed/grown before running.
- **Recursive re-split** → capped at one level, then force atomic or escalate.
- **Campaign abandonment** → state persists; not all-or-nothing; resumable / cancelable.
- **ROADMAP clobbering** → the campaign *appends* a structured sub-scope block; never
  rewrites the hand-authored ROADMAP structure.

### Testing

- `genome.scope_split` (table-driven synthetic graphs): one blob → `atomic`; two
  weakly-linked clusters → 2 sub-scopes; a chain → ordered sequence; mutual dep → merge/flag;
  poor cut quality → `atomic`.
- `genome.campaign` state-machine tests: next-ready selection, advance-on-merge,
  re-validation transitions (moot/changed/grown), resume from a persisted state.
- A **`--dry-run`**: smart-cut + propose the decomposition + show the campaign plan, but
  create nothing and run no scope-run.
- Integration smoke: a synthetic 3-cluster scope → assert the proposed 3 ordered sub-scopes
  + a campaign that would run them in order.

---

## 3. Resolved defaults

1. **Persistence** → a structured campaign state file is the source of truth; ROADMAP carries
   the human-readable reflection.
2. **Sub-scope ids** → new stable `PR N` slots (the existing convention), each tagged with
   its `origin_scope`.
3. **Re-validation** → always re-dispatch a sub-scope immediately before it runs (baked in).
4. **Recursive re-split** → capped at one level.
5. **Phased build** → **Phase 1: smart-cut detector + the pre-plan micro-gate** (advisory;
   sub-scopes run manually). **Phase 2: the campaign orchestrator** (the persistent
   multi-session state machine). Phase 1 delivers most of the value at a fraction of the cost
   and de-risks Phase 2.

---

## 4. Out of scope (for B2 / this spec)

- Removing or shortcutting the two human gates — orchestration never crosses a gate.
- The fast-follow drain loop (Sub Project B) — the *small*-leftover dual.
- Sub Projects C / D, except where Phase 2 consumes C's resumability infra.
- Wiring into the deterministic JS workflows — B2 targets the model-driven path.

---

## 5. Dependencies & sequencing

- **Phase 1 (smart-cut + micro-gate)** — buildable anytime after the dispatcher's call-graph
  work; advisory, no orchestrator.
- **Phase 2 (campaign orchestrator)** — leans on **Sub Project C's resumability**, so the
  natural overall order is **A → B → C → B2-Phase 2**.
- B2 is the **largest** of the four sub-projects; the phasing is what keeps it tractable.

---

## 6. Next step

Move to an implementation plan (the `writing-plans` skill), **Phase 1 first**: the
`genome.scope_split` smart-cut core (test-first, synthetic graphs), the Stage-0.5 split
check + pre-plan micro-gate in `/scope-run`, and the ROADMAP sub-scope writer — with the
dev-loop and the `--dry-run` integration smoke as the verification. Defer the `genome.campaign`
orchestrator (Phase 2) until C lands.

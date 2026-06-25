# Sub Project B — Fast-Follow Loop (`/fast-follow`)

**Status:** Implemented and merged (PR #104, 2026-06-25). Durable record: finding-038. This plan artifact is transient (see DEC-0084).
**Date:** 2026-06-24.
**Source:** Brainstorming session on bolstering `/scope-run`.
**Scope:** This is the **fast-follow drain loop** half of **Sub Project B** of the four-part
enhancement effort (A–D). The dual half — **scope-split intake** — is deliberately split
into its own sibling spec (call it B2) and is out of scope here.
**Depends on:** **Sub Project A** (`/verify-and-merge`). B reuses A's evidence gate to merge
every drained batch, so A must ship first.

---

## 1. Context — where this fits

`/scope-run` produces leftover work — review nits logged-not-blocked, plan "Out-of-scope"
items, deferred finding sub-items, `repo-sweep` backlog — but nothing **drains** it.
`repo-sweep` is a backlog *producer with no consumer*. B is the missing consumer: a bounded
loop that drains the genuinely-small leftovers without re-running the full machinery, and
ejects anything non-trivial back to a real `/scope-run`.

**The repo already does this by hand.** ROADMAP is the deferral ledger (a "Deferred to later
phases" list and a **"Deliberately deferred — gated on a future signal"** section), and
**`PR 8` is a manual prototype of this loop** — explicitly a *"Deferred docs/cosmetic
batch"* bundling small leftovers (a `MAPPED_TRAIT_URI` truncation finding entry deferred
from 5.3, an imputation docstring filename fix, …). **B automates the PR-8 pattern and makes
it continuous.**

**The queue lives as prose, not a structured table.** There are currently no
`status: deferred` findings; deferred work lives in ROADMAP prose + finding "Out of scope"
sections. So the loop *reads ledgers*, it does not pop a clean queue — ingestion is a scan,
not a dequeue.

---

## 2. Locked decisions (from the brainstorm)

| # | Decision | Choice |
|---|---|---|
| 1 | When it runs | **Post-merge** ("after the full implementation") — a separate fast-follow PR, never folded into the main change. |
| 2 | Mandate (drain vs eject) | **Trivial code too** — Tier-0 + tightly-bounded Tier-1 (small code: a one-line bugfix, a rename, a missing-test add), **never** schema/pipeline/annotation-loader or anchor-exposed code (those always EJECT). |
| 3 | Trigger | **Auto-offer at close + manual** — after a `/verify-and-merge` close it auto-scans and offers a triage plan; plus `/fast-follow` on demand. Never acts without approval. |
| 4 | Human touchpoints | **Two** — (1) **triage approval** before any work; (2) **A's verify-and-merge gate** per drained batch. |
| 5 | Gate reuse | Each drained batch goes through **Sub Project A's `/verify-and-merge`** (one batched gate, not one per item). |

---

## 3. Design

### Architecture (chosen approach)

`/fast-follow` command + a testable `genome.fast_follow` core + reuse of existing agents.
**No new agent** — mirrors Sub Project A's "thin skill + `genome.*` core + reuse" shape, so
the eligibility rules become unit-tested code rather than prose.

- **`genome.fast_follow`** (Python, unit-tested) — the **triage classifier**: given a
  candidate's attributes (`change_class`, `blast_radius`, `applicable_anchors`, kind), apply
  the eligibility rules → `DRAIN | EJECT | DISCARD` + a re-tier. The testable heart (mirrors
  A's `genome.verify_gate`).
- **`.claude/commands/fast-follow.md`** (the skill) — orchestrates scan → triage → approval
  → drain → A's gate → loop.
- **Reused, unchanged:** `repo-sweep` (scan), `implementer` / `fan-out-implementer` /
  `green-keeper` (drain), `/verify-and-merge` (gate + close).

### Data flow (the loop)

1. **Trigger** — auto at a `/verify-and-merge` close, or manual `/fast-follow`.
2. **Scan** — gather candidates: `repo-sweep` `fruit`/`maybe` · ROADMAP "Deliberately
   deferred" entries whose gating signal fired · finding "Out of scope" sub-items · the
   latest scope-run's Stage-3 nits. Dedup against an already-handled **seen-set**.
3. **Triage** — for each candidate, read what it would touch and run `genome.fast_follow`:
   - **DRAIN** — Tier-0, or Tier-1 with no schema/pipeline/annotation-loader,
     `applicable_anchors == 0`, and small blast_radius.
   - **EJECT** — anything bigger or guard-violating → a ROADMAP `/scope-run` candidate.
   - **DISCARD** — stale / already done → logged.

   Emit the **triage plan**.
4. **🚦 Triage approval (touchpoint 1)** — present the plan ("drain these 5, eject these 2,
   discard 0 — with a reason each"); the human approves or overrides a classification. No
   work happens before this.
5. **Batch** — group DRAIN items by file/subsystem (independent items →
   `fan-out-implementer` worktrees). Usually one batch.
6. **Implement** — the writers make the changes; `green-keeper` holds the dev-loop; one
   lightweight review (`/code-review` + `convention-compliance` + a single
   `finding-verifier`). Each change records which backlog item it drains (provenance).
7. **🚦 Verify + merge (touchpoint 2 = A's gate)** — the batch goes through
   `/verify-and-merge`: evidence-gated, the human approves, Claude squash-merges + closes
   (CHANGELOG, `repo-sweep`, branch cleanup).
8. **Loop** — re-scan (a batch may resolve items or surface new trivial ones); drain the
   next batch; terminate on **dry** (only eject/discard remain) or a **hard cap** → overflow
   stays in the backlog.

### The safety composition (why the fast lane can't ship a regression)

Defense in depth — triage can be *wrong* and the system is still safe:

- **Triage guards** keep schema/pipeline/anchor work out of the lane (→ EJECT).
- **A's verify gate is the backstop** — if a "trivial" change *secretly* moves an anchor, A
  BLOCKS the batch → it does not merge → that item is reclassified EJECT and the rest
  re-batched.
- **Two human touchpoints** bracket the work (approve the plan; approve the merge).

### Error / edge handling

- **Item bigger than triaged** (implementer hits a surprise) → STOP that item, reclassify
  EJECT, continue the rest.
- **Batch fails A's gate** → no merge; offending item ejects; rest re-batch.
- **Eject target** → written to ROADMAP (a backlog entry / new sequence slot, exactly how
  `PR 8` was born) so it is never lost. **Discards logged** — no silent truncation.
- **Loop safety** → seen-set dedup + hard cap; a fix that spawns its own nit cannot loop
  forever.
- **Empty / eject-only backlog** → no-op, or "nothing drainable; wrote N ejects to ROADMAP";
  never an empty PR.

### Testing

- Table-driven unit tests for `genome.fast_follow` (docs item → DRAIN; Tier-1 no-anchor code
  → DRAIN; schema item → EJECT; anchor-exposed → EJECT; stale → DISCARD).
- Batcher grouping + loop-termination (dry / cap / dedup) tests.
- A **`--dry-run`** (reuses A's): scan + triage + present plan, no implement/merge.
- Integration smoke: seed a fake backlog (2 trivial + 1 schema), run `/fast-follow
  --dry-run`, assert plan = 2 DRAIN / 1 EJECT and "would drain."

---

## 4. Resolved defaults

1. **Eject target → ROADMAP** — the established backlog; mirrors how `PR 8` was created.
2. **Caps → ≤ 10 items or ≤ 3 batches per invocation**, overflow stays in the backlog.
   Tunable starting numbers.
3. **Auto-offer placement → in A's close step** — so *any* `/verify-and-merge` (even
   standalone) offers a drain, not only `/scope-run` Stage 5.

---

## 5. Out of scope (for B / this spec)

- **Scope-split intake (B2)** — the dual at the *intake* end (the dispatcher proposing a
  decomposition when a scope is too big). Mechanically independent; its own sibling spec.
- Sub Projects **C / D** — cross-run learning, token/context budgeting, resumability,
  fidelity-gap wiring, observability.
- Wiring into the **deterministic JS workflows** — B targets the model-driven path; the JS
  integration is Sub Project D's concern.
- Auto-draining without approval — the triage-approval touchpoint is always required.

---

## 6. Dependencies & sequencing

- **Hard dependency on Sub Project A** — B's batch merge *is* A's `/verify-and-merge`, and
  the auto-offer hooks into A's close step. Build A first.
- After B: **B2 (scope-split)**, then C / D as independent bets.

---

## 7. Next step

Move to an implementation plan (the `writing-plans` skill) once A has landed: break B into
ordered tasks — the `genome.fast_follow` triage classifier (test-first), the `fast-follow.md`
skill (scan → triage → approval → drain → gate → loop), the `repo-sweep` scan hookup, the
ROADMAP eject writer, and the auto-offer hook in A's close — with the dev-loop and the
`--dry-run` integration smoke as the verification.

# Sub Project C1 — Cross-Run Learning (`/calibrate`, L3-asymmetric)

**Status:** Design approved (brainstorming), ready for an implementation plan.
**Date:** 2026-06-24.
**Source:** Brainstorming session on bolstering `/scope-run`.
**Scope:** The **learning/calibration** half of Sub Project C. The other half — **C2
(budget + resumability)** — collapsed into *"the Workflow-engine port"* (a foundational
keystone shared with D and B2-Phase 2) and is **out of scope here**, to be designed with D.
**Depends on:** **Sub Project A** (soft — the outcome write-hook lives in A's close step).

---

## 1. Context — it completes the loop

The system already runs `predict → flag → confirm → record` (the anchor loop). C1 adds
**`→ learn`**. Half already exists: the dispatcher *reads* `precedent` ("the 2–3 nearest past
findings/PRs and **what surprised them**") by grepping findings/git. **C1 writes that side**
— systematically, from what actually happened — and adds a calibration mechanism. It is the
agent-team build's **stated, deferred follow-up** (*"Calibrating the risk-tier formula on
real runs"*), not a speculative idea.

---

## 2. Locked decisions (from the brainstorm)

| # | Decision | Choice |
|---|---|---|
| 1 | Learning ambition | **L3 — auto-tuning**, chosen over the recommended L2 (human-gated report). Deliberate, eyes-open. |
| 2 | Direction asymmetry | **Asymmetric ratchet** — *tightening* (more scrutiny) auto-applies; *loosening* (less scrutiny) stays one-click human-gated. |
| 3 | The rationale | Under-tiering is the **irreversible/expensive** error (missed schema bug → `rm -rf data/`; missed PHI → unrecoverable); over-tiering is **cheap** (wasted tokens). So auto-tightening is safe; auto-loosening is the one dangerous direction → keep a human. |
| 4 | Floors | The trip-wire floors (`schema|ddl → 2`, `anchors → 2`, PHI severity) are **immutable** — never auto-tunable. |
| 5 | Auditability | Every auto-change is **git-committed** (rationale + back-test diff) and reversible; never a silent in-memory mutation. |

This relaxes the finding-034 stance ("the formula is a calibrated, back-tested locked
decision") **in the safe direction only**, consistent with the repo's stated
over-tiering bias ("lower `t2` before raising it").

---

## 3. The design

### Architecture (mirrors A/B/B2)

- **`genome.calibration`** (Python, testable) — the outcome-record schema, the per-knob
  accuracy computation, the ratchet logic, and the back-test re-runner.
- **`risk_weights` config** (new, versioned) — the tunable knobs (C-map, B-buckets, P-levels,
  `t1`/`t2` thresholds) extracted from prose into **data the dispatcher reads and the tuner
  writes**. *Auto-tuning requires the knobs to be data, not prose* — this is the one
  architectural addition L3 forces (L2 did not need it). The **floors are NOT in the config**
  — they stay hard-coded and immutable. finding-034's prose becomes documentation; the config
  becomes source of truth.
- **A write-hook in A's close step** — A already captures the human-confirmed gate data; C1
  piggybacks to write the outcome record.
- **A dispatcher read-extension** — precedent search also reads the outcome ledger.
- **A `/calibrate` command** — on-demand report + the manual trigger for the ratchet.

### The two loops

**Fast loop — precedent enrichment (automatic, advisory, safe).** Every merged scope appends
an outcome record; the dispatcher's precedent search surfaces real "this bit us last time"
data → sharper P-scoring + precedent list. Advisory only — it informs the P sub-score, which
merely *nudges* the tier; the conservative `max`/`floor` rules dominate, so it cannot by
itself cause under-tiering.

**Slow loop — the asymmetric auto-tuning ratchet (L3).** A calibration pass runs
automatically (gated: every N merges *and* ≥K-outcome evidence) and on demand via
`/calibrate`. It computes per-knob systematic tier error over the outcome ledger, then:

- **Tightening** (a knob systematically *under*-tiered → raise it one step / lower `t2`):
  re-run the back-test with the change; **if green, auto-commit it** (rationale + cited
  outcomes + back-test diff in the commit message). No human. Reversible.
- **Loosening** (a knob systematically *over*-tiered → lower it one step): draft the same
  change but **park it for one-click human approval** — never auto-applied.

**Guards (both directions):**
- **Back-test hard gate** — any change that flips a historical PR's known-correct tier is rejected.
- **Floors immutable** — auto-tuning only ever touches the additive C/B/P weights + thresholds.
- **Bounded + hysteresis** — ≤ 1 bucket-step per change; only after the same systematic error across ≥ K outcomes.
- **Thin-data lockout** — no auto-tuning under ~N outcomes (overfitting guard).
- **Kill switch** — a config flag disables auto-tuning entirely.
- **Audit log** — append-only record of every auto-change for periodic human review + easy revert.

### The outcome record (the datum)

Written at A's close, sourced from **human-confirmed gate facts + recorded run artifacts** —
never Claude's self-assessment:

```
{ scope_id, merged_sha, date,
  predicted: { tier, breakdown{C,B,P,A,S}, premortem_surprises[], anchors_to_watch[] },
  actual:    { gate_verdict, review_blockers[], surprises_materialized[],
               surprises_missed[], anchors_moved_unexpected[],
               revise_cycles, fix_first_cycles, needed_deep } }
```

The calibration computation derives, per knob: predicted tier vs. tier-in-hindsight
(systematic under/over-tiering) and pre-mortem precision/recall (crying-wolf vs. blind-spots).

### Data flow

`/verify-and-merge` close (A) → **write outcome record** → next `/scope-run` dispatch
**reads** it as precedent (fast loop) → every N merges (≥ K evidence) or on demand, the
**ratchet** runs: under-tiering → **auto-commit a tighten** (back-test-gated); over-tiering →
**draft a loosen for one-click approval** → `risk_weights` config updated, back-test extended.

### Error / edge handling

- **Thin data (< ~N outcomes)** → ratchet is a no-op; `/calibrate` reports "insufficient data."
- **Change fails the back-test** → suppressed (tighten) / not drafted (loosen); never applied.
- **Outcome write fails** → logged, non-blocking; never holds up a merge.
- **Precedent store missing/corrupt** → dispatcher falls back to today's git/finding grep.
- **Runaway tightening** → bounded step + hysteresis + the audit log + kill switch; a human can revert any auto-commit.

### Testing

- `genome.calibration` table-driven: under-tiering set → tighten **auto-applies**; over-tiering
  set → loosen **drafts but does not apply**; a back-test-failing change → **suppressed**;
  floors → **never touched**; thin data → **no-op**; kill-switch → disabled.
- Pre-mortem precision/recall math; outcome-record round-trip; dispatcher-reads-precedent
  integration; `risk_weights` config read/write + back-test re-run.
- `--dry-run` `/calibrate`: report + show what it *would* commit/draft, change nothing.

---

## 4. Resolved defaults

1. **Storage** → a dedicated append-only **outcome ledger** (source of truth, queryable);
   durable surprises still graduate to a **finding** (which the dispatcher already greps).
2. **Knobs as data** → extract into the versioned `risk_weights` config; floors stay
   hard-coded/immutable.
3. **Trigger** → automatic ratchet every N merges (≥ K-outcome evidence) + `/calibrate` on demand.
4. **Ground truth** → human-confirmed gate + run artifacts, never Claude's self-grade.
5. **Soft dependency on A** → the write-hook lives in A's close; build A first (or stub it).

---

## 5. Out of scope (for C1 / this spec)

- **C2 (budget + resumability) = the Workflow-engine port** — the foundational keystone, to
  be designed with D.
- **Auto-mutating the trip-wire floors** — never; only the additive weights + thresholds tune.
- **Auto-applying a loosening** — always one-click human-gated.
- Wiring into the deterministic JS workflows — C1 targets the model-driven path.

---

## 6. Dependencies & sequencing

- Soft dependency on **A** (the outcome write-hook). Otherwise independent and model-driven —
  buildable on the current path without the Workflow-engine port.
- Natural order across the effort: **A → B → C1 → (Workflow-engine port = C2 + D) → B2-Phase 2.**

---

## 7. Next step

Move to an implementation plan (the `writing-plans` skill): the `genome.calibration` core +
the `risk_weights` config extraction (test-first, including the back-test gate and the
tighten-auto/loosen-gated asymmetry), the outcome write-hook in A's close, the dispatcher
read-extension, and the `/calibrate` command — with the dev-loop and the ratchet test matrix
(tighten/loosen/back-test-fail/floors/thin-data/kill-switch) as the verification.

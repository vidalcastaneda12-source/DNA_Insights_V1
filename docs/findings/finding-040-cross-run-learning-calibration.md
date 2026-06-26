---
type: decision
status: active
actors: [VSC-User, ClaudeCodeDevelopment]
date: 2026-06-25
supersedes: []
superseded_by: []
---
# Finding 040 — Cross-run learning calibration (`/calibrate`, the L3-asymmetric ratchet)

## Status

**Active (2026-06-25).** Adopted at Gate 1 for Sub Project C1. This is the durable provenance
anchor for the `genome.calibration` core, the `genome calibrate` sub-app, the
`compute_tier` loop-closure seam that the `scope-dispatcher` now RUNS, the outcome-write hook in
the `/verify-and-merge` close step, and the ledger rows `DEC-0095 … DEC-0098`. The synthesized
plan artifact is transient (plans get pruned — see `DEC-0084`); this finding is where the design
rationale lives. Ships **report-only** — `auto_tuning_enabled` is seeded `false`.

## Related findings

- [`finding-034`](finding-034-agent-team-plan-phase.md) (the per-scope agent team) — C1 is that
  team's stated, deferred follow-up ("calibrating the risk-tier formula on real runs"): it adds
  `→ learn` to the existing `predict → flag → confirm → record` loop.
- [`finding-039`](finding-039-scope-split-smart-cut.md) (Sub Project B2 — the scope-split
  detector) — C1 reuses B2/B/A's DB-free-core + JSON-seam + independent-vocab + fail-closed shape,
  and **reconciliation-pins** its seed weights to `scope_split.model._C_MAP` / `est_risk_tier`
  (which stay finding-039-frozen).
- [`finding-037`](finding-037-agentic-verify-merge-gate.md) (Sub Project A — the verify-merge
  gate) — A's close step is where the outcome-write hook lives (the soft dependency); the same
  fail-closed-reducer discipline carries here.
- [`finding-038`](finding-038-fast-follow-drain-loop.md) — the hard-coded `data/...` path
  convention (never `get_settings`) is mirrored from `fast_follow.persistence`.

## Context

The per-scope team predicts a risk tier, flags anchors, confirms at the gate, and records the
merge — but never **learns**. The naive learn step is structurally dead one level up from
finding-039: the manifest `risk_tier` had **no deterministic Python consumer** (the markdown
dispatcher computed it from prose; the only Python tier code, `scope_split.est_risk_tier`, is the
splitter's frozen re-scorer, not the manifest tier). A tuner that rewrites a config the dispatcher
never reads in Python cannot provably move the emitted tier — the loop never closes.

## The finding itself

### Loop closure = D1 — `compute_tier` is the single deterministic tier source of truth

`genome.calibration.model.compute_tier(fields, weights) -> (tier, breakdown)` is the **one** place
the additive `S = C + B + P`, the immutable floor, and the `t1`/`t2` banding live, parameterised by
a `RiskWeights` read from the git-tracked `risk_weights.json`. It is exposed as
`genome calibrate compute-tier --manifest -`; the `scope-dispatcher` now **RUNS** it (mirroring its
existing Bash blast-radius shell-out) and **CONSUMES** the returned `{tier, breakdown}` as the
authoritative `risk_tier` + `risk_breakdown`. The inline prose C/B/P/t1/t2 sub-score math is
**demoted** to a non-authoritative "Reference" appendix; a tuner change to `risk_weights.json`
provably moves `compute-tier`'s output, so the probe is a **deterministic pytest**, not a
model-mediated hope. The conservative `+1` bump (`has_open_questions` / `human_bump`) and the
`deep_T2` selector (`(S >= 7) OR (A >= 3)`) are re-homed **into** `compute_tier` (v2.1 amendment);
only the cross-stage pre-mortem=probe-first re-bump stays an explicit dispatcher step.

### The asymmetric L3 ratchet (the safety core)

`ratchet.propose_ratchet(outcomes, weights, merges_since_last) -> RatchetDecision` is a layered,
fail-closed reducer: kill-switch → thin-data (`< 10`) → cadence (`< 5`) → hysteresis (`< 3`
same-direction misses) each reduce to `NO_OP` before an `AUTO_COMMIT` is reachable. It builds a
±1-step candidate on the knob the breakdown attributes (the ledger is PHI-slim, so attribution is
from the sub-scores alone), then classifies **direction by the tier DELTA over the
direction-witness ladder** — a `t1`/`t2` raise is a **LOOSEN**, never the knob's numeric sign. The
asymmetry: a `TIGHTEN` auto-applies **iff** it is back-test-clean **AND** the knob has unfloored
coverage; a `LOOSEN` **or** a clean-by-vacuity tighten **parks** for one-click human approval; a
back-test-dirty tighten is **suppressed**. Under-tiering is the irreversible/expensive error
(missed schema bug, missed PHI); over-tiering is cheap — so auto-tightening is safe and
auto-loosening keeps a human.

### Floors immutable by construction

The trip-wire floor (`schema`/`ddl` touched **or** any anchor → Tier 2) is hard-coded in
`compute_tier` and **not representable** in `RiskWeights`: there is no `floor` field and
`RiskWeights.from_json` rejects a `"floor"` key. Every `from_json` rejects an unexpected field, so
PHI is structurally impossible in the ledger / manifest. The only tunable surface is the additive
`c_map` / `b_buckets` / `p_levels` maps and the `t1` / `t2` thresholds.

### Git-write has zero precedent — a tested CommitPlan, never a subprocess

`commit_plan.render_commit_plan` emits argv data (`git add -- <weights>` /
`git commit -F - -- <weights>`), **both** pathspec-scoped, never `-A` / `-u` / `.` / a bare commit;
the Python core never imports `subprocess`. The skill runs git gated on the CLI exit, asserting a
clean index + on-C1-branch.

### Accepted divergence + report-only ship

`scope_split.model` stays finding-039-frozen (NOT refactored to call `compute_tier`); reconciliation
pins the **SEED** (`SEED_RISK_WEIGHTS.c_map == _C_MAP` AND `compute_tier(·, SEED) == est_risk_tier`
on the 6 back-test rows). Post-tighten the dispatcher (live, via `compute-tier`) and the splitter
(frozen seed) diverge until a **deferred convergence PR** (the splitter is advisory). The seed
`auto_tuning_enabled=false`, so the ratchet is **dark** (always `NO_OP`); the `ratchet` CLI defaults
to `--dry-run`. Enablement is gated on the deterministic loop-closure test, the three safety fixes
(coverage-PARK, delta-direction, write-hook), and VSC-User confirming the `tier_in_hindsight`
default.

### The three frozen fixtures (durable identifiers)

These are **exact / deterministic** (not tolerance-banded — a static formula + curated fixtures):

- `BACKTEST_ROWS` (6 rows) reproduce `{PR-8:0, PR-12:1, PR-6:1, PR-7:1, PR-5a:2, PR-3:2}` under the
  seed; PR-5a / PR-3 are pinned at Tier 2 by the immutable anchor floor.
- `DIRECTION_WITNESS_LADDER` = the 6 rows ∪ 4 synthetic **unfloored** band witnesses at
  `S ∈ {0, 1, 4, 5}`; the unfloored `S = 5` witness is load-bearing — without it a `t2` raise
  yields zero tier deltas and the loosen-inversion is invisible.
- `KNOB_COVERAGE` marks the 9 PARK-ONLY knobs whose only covering rows are floored
  (`c_map.{annotation-loader, analysis, insights, pipeline, schema, ddl}`,
  `b_buckets.{moderate, large}`, `p_levels.correction`) — a tighten of any is clean **by vacuity**
  and is human-gated, never auto-committed.

`tier_in_hindsight` is a strict priority ladder: a blocked/escalate verdict **or** any review
blocker / materialized surprise / unexpected anchor move / needed-deep → Tier 2; else `>= 2` combined
revise+fix cycles → a **hard** Tier 1 (this is what makes a predicted-Tier-2 + mild-friction outcome
register as over-tiered → the loosen signal); else the predicted tier (a clean run confirms the
call).

## Provenance — CAPTURE / RETRIEVAL / LIFECYCLE

This finding is the citable knowledge unit the `genome docs check` gate validates across its three
categories:

- **CAPTURE** — born with the `---`-fenced frontmatter (`type` / `status` / `actors` / `date` /
  `supersedes` / `superseded_by`) the gate requires; `DEC-0095 … DEC-0098` are appended to the
  `MEMORY.md` ledger with their `detail-link` pointing back here.
- **RETRIEVAL** — a `scope-dispatcher` runs `genome calibrate compute-tier` for every scope's tier;
  a `plan-premortem` / `regression-hunter` can cite `finding-040` for the calibration design and its
  asymmetry invariant; the named knobs (`THIN_DATA_MIN_OUTCOMES=10`, `CADENCE_MIN_MERGES=5`,
  `HYSTERESIS_MIN_RUNS=3`; seed `t1=1`, `t2=5`) are the tunables, and the seed weights live in
  `backend/src/genome/calibration/risk_weights.json`.
- **LIFECYCLE** — `status: active`; the enablement flip (`auto_tuning_enabled=true`), the deferred
  dispatcher/splitter convergence PR, and any future knob added to `KNOB_COVERAGE` are
  insert-then-flip supersessions, never in-place edits.

## Consequences / follow-ups

- The dispatcher/splitter post-tighten divergence is **accepted** and tracked to a deferred
  convergence PR; until then the splitter (`est_risk_tier`) is advisory and the dispatcher's
  `compute-tier` is authoritative.
- The unattended every-N-merges close-hook auto-commit is **deferred**; on-demand `/calibrate`
  (`genome calibrate ratchet`) is first.
- The write-hook learns a scope **only** if `compute-tier --persist` ran at dispatch before A's
  close sources it; an absent manifest is a **visible drop** (`outcome NOT recorded` warning +
  exit 0 + no append), never a silent or corrupt row — the residual is that one un-learned scope.

### Pre-enablement residuals — the dark `apply-parked` / `ratchet --apply` write path

The Stage-3 review (per-scope agent team, `finding-034`) cleared C1 to merge **report-only/dark**
(`auto_tuning_enabled=false`). The completeness-critic then found a contained cluster of latent gaps
in the `apply-parked` write path (fix E). **All are unreachable at ship**: `propose_ratchet`'s first
gate is the kill switch, so while dark *no `PARK` row is ever produced*, so `apply-parked` has nothing
to act on — the whole ratchet→park→apply chain is inert until the enablement signoff. They are
**pre-enablement must-fix, not merge blockers**, recorded here so the enablement-flip PR inherits them:

- **Stale full-snapshot apply (lost-update).** `apply_parked_cmd` writes the park-time `RiskWeights`
  snapshot wholesale, not a delta re-derived against the live weights. If an intervening auto-commit
  moved a *different* knob between park and approval, approving the stale park silently reverts it; the
  apply-time TOCTOU re-check (`run_backtest` + `classify_direction` vs live) only catches a revert that
  moves a ladder/back-test tier, so a tier-neutral revert slips both gates. *Fix at enablement:*
  re-derive the candidate as a one-knob delta on live, or assert the candidate's non-target knobs equal
  live before write.
- **Parked row never consumed (re-appliable).** Approval appends an `applied=true` row but never retires
  the original `applied=false` `PARK` row; the `not row.applied` filter re-selects it, so a clean-by-
  vacuity tighten is re-appliable (duplicate write + duplicate `CommitPlan` → the skill attempts an empty
  commit), and only the most-recent parked row is ever actionable (older ones strand). *Fix at
  enablement:* mark the parked row consumed on approval (insert-then-supersede, never in-place).
- **`apply-parked` does not read the kill switch — an open design decision for VSC-User.** Whether
  one-click human approval is *exempt* from `auto_tuning_enabled=false` (manual-override semantics) or
  must also honor it (strict "no weight write until signoff") was never decided. Reachable only via
  toggle-off-after-park (no parked rows exist while dark). **This is a `VSC-User` call at the enablement
  gate**, not a mechanical fix — surfaced in the Stage-4 pre-gate package.
- **Coverage of the above is deferred** with them: tests for `apply-parked` under kill-switch-off, parked-
  row re-apply/consumption, and stale-snapshot clobber land with the enablement fixes they exercise.

Lower-severity Stage-3 review nits deferred to the backlog (non-blocking, dark or report-only):
reference-table doc-drift after the first tighten (accepted per `DEC-0095`); `per_knob_tally` silently
dropping an unattributable all-zero-breakdown outcome (fail-safe direction, report-only); assorted
test-adequacy nits (`report`/`format_ratchet_decision` NO_OP smoke, tie-break, `_bump_version` fallback,
empty-tally NO_OP); and the documented `est_risk_tier` dual-formula convergence deferral.

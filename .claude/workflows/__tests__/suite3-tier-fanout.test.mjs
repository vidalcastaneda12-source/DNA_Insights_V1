// Suite 3 — tier-fanout: the plan-phase call graph per tier matches scope-run.md.
//
// from: .claude/commands/scope-run.md §"Stage 1 — Plan" (lines 88-96) + §"Depth
//       quick-reference" (lines 187-194); finding-034 §"Adaptive depth — recalibrated"
//       (lines 1129-1133); C2D-Phase1-synthesized-plan.md §Tests "Suite 3 tier-fanout"
//       (Tier-0 angle minimal-diff; Tier-2 distinct architect-reviewer; deep_T2 -> 3
//       skeptics) + GAP-6a/6b.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PLAN_PHASE, loadWorkflow, countOf, callsOf, callAngle, PLAN_TIER_EXPECT } from './_harness.mjs';

async function planAt(hooks) {
  const r = await loadWorkflow(PLAN_PHASE, { auditorVerdict: 'ready', premortemRecommend: 'proceed', ...hooks });
  assert.equal(r.error, null, 'plan-phase errored: ' + (r.error && r.error.message));
  return r;
}
const anglesOf = (calls) => new Set(callsOf(calls, 'planner').map(callAngle));

// ---- Tier 0: 1 planner (minimal-diff) -> pre-mortem(1) -> auditor; no judges/synth/architect.
test('Tier 0: single minimal-diff planner; pre-mortem(1); no judges, no synthesizer, no architect-reviewer', async () => {
  const { calls } = await planAt({ tier: 0 });
  const exp = PLAN_TIER_EXPECT[0];
  assert.equal(countOf(calls, 'planner'), exp.planners, 'Tier 0 runs exactly 1 planner');
  assert.ok(anglesOf(calls).has('minimal-diff'), "Tier 0's lone planner is the minimal-diff angle (GAP-6a: not ['general'])");
  assert.equal(countOf(calls, 'plan-judges'), exp.judges, 'Tier 0 runs no judges');
  assert.equal(countOf(calls, 'plan-synthesizer'), exp.synthesizer, 'Tier 0 runs no synthesizer (nothing to graft)');
  assert.equal(countOf(calls, 'plan-premortem'), exp.premortem, 'pre-mortem fires at Tier 0 (all-tiers rule)');
  assert.equal(countOf(calls, 'architect-reviewer'), exp.architect, 'no separate architect-reviewer below Tier 2');
  assert.ok(countOf(calls, 'plan-auditor') >= 1, 'Tier 0 still runs the auditor');
});

// ---- Tier 1: 2 planners (minimal-diff + gate-backward) -> light judge(1) -> synth -> pre-mortem(1).
test('Tier 1: 2 planners (minimal-diff + gate-backward); single light judge; synthesizer; no architect-reviewer', async () => {
  const { calls } = await planAt({ tier: 1 });
  const exp = PLAN_TIER_EXPECT[1];
  assert.equal(countOf(calls, 'planner'), exp.planners, 'Tier 1 runs exactly 2 planners');
  const angles = anglesOf(calls);
  for (const a of exp.plannerAngles) assert.ok(angles.has(a), `Tier 1 includes the ${a} planner`);
  assert.equal(countOf(calls, 'plan-judges'), exp.judgesExact, 'Tier 1 uses a single combined "light" judge');
  assert.equal(countOf(calls, 'plan-synthesizer'), exp.synthesizer, 'Tier 1 runs the synthesizer');
  assert.equal(countOf(calls, 'plan-premortem'), exp.premortem, 'Tier 1 pre-mortem = 1 agent');
  assert.equal(countOf(calls, 'architect-reviewer'), exp.architect, 'the separate architect-reviewer is Tier-2-only');
});

// ---- Tier 2 (standard): full 4-angle panel -> per-axis judges -> synth -> pre-mortem(2)
//      -> auditor panel + a DISTINCT architect-reviewer.
test('Tier 2 standard: 4-angle panel; per-axis judges; pre-mortem(2); a DISTINCT architect-reviewer', async () => {
  const { calls } = await planAt({ tier: 2, deepT2: false });
  const exp = PLAN_TIER_EXPECT[2];
  assert.equal(countOf(calls, 'planner'), exp.planners, 'Tier 2 runs the full 4-angle panel');
  assert.deepEqual([...new Set(callsOf(calls, 'planner').map(callAngle))].sort(), [...exp.plannerAngles].sort(), 'Tier 2 planner angles are minimal-diff/gate-backward/risk-first/convention-purist');
  assert.ok(countOf(calls, 'plan-judges') >= exp.judgesMin, 'Tier 2 fans out per-axis judges (>1)');
  assert.equal(countOf(calls, 'plan-synthesizer'), exp.synthesizer, 'Tier 2 runs the synthesizer');
  assert.equal(countOf(calls, 'plan-premortem'), exp.premortemStandard, 'standard Tier 2 = 2 pre-mortem skeptics');
  assert.equal(countOf(calls, 'architect-reviewer'), exp.architect, 'Tier 2 adds exactly one architect-reviewer (GAP-6b)');
  assert.ok(countOf(calls, 'plan-auditor') >= 1, 'Tier 2 runs the plan-auditor panel');
  // GAP-6b: the architect-reviewer is DISTINCT from the auditor (both present, different agentTypes).
  assert.notEqual('architect-reviewer', 'plan-auditor');
  assert.ok(countOf(calls, 'architect-reviewer') >= 1 && countOf(calls, 'plan-auditor') >= 1, 'architect-reviewer and plan-auditor are both run, as distinct members');
});

// ---- deep_T2 -> 3 pre-mortem skeptics.
test('Tier 2 deep_T2: pre-mortem scales to 3 distinct-lens skeptics', async () => {
  const { calls } = await planAt({ tier: 2, deepT2: true });
  assert.equal(countOf(calls, 'plan-premortem'), PLAN_TIER_EXPECT[2].premortemDeep, 'deep_T2 runs 3 pre-mortem skeptics (else 2)');
});

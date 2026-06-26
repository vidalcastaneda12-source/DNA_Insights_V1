// Suite 8 — fidelity-verifier-severity: severity-scaled refute-by-default verification
// (GAP-5) — blocker -> 2-3 skeptics, warn -> 1, nit -> 0 — and strict-majority survival
// (D6): a tie / minority-not-refuted blocker is KILLED; a majority-not-refuted one
// survives.
//
// from: .claude/agents/finding-verifier.md (blocker -> 2-3 distinct-angle skeptics;
//       warn -> 1; nit -> not verified; killed unless a majority FAIL to refute);
//       .claude/commands/scope-run.md §"Stage 3" line 140; C2D-Phase1-synthesized-plan.md
//       §Implementation steps 5 (verifyFresh severity scaling: blocker -> parallel
//       ['reproduce','reachable','documented-exception'] (3, ->2 budget-tight); warn->1;
//       nit->logged; majority-survives) + §Tests "Suite 8"; plan-v2-delta D6 (strict
//       majority: survives iff > half NOT-refuted; a tie -> KILLED).
//
// OBSERVABILITY: the workflow's verifyFresh computes strict-majority survival and records
// the verified survivor set on the documented pre-gate package field `result.stage3.survivors`
// (scope-run.md §"Stage 3" step 4; review-synthesizer.md "keep survivors only"). We assert
// DIRECTLY on that field — the production strict-majority path — NOT merely on the downstream
// handoff-assembler presence (which is driven by the synthesizer's own verdict and so could
// pass even while verifyFresh is dead-keyed; that indirection masked the top-level-`survives`
// stub-fidelity bug fixed in _harness.mjs this round). Each case keeps the handoff/route
// observable too, as an independent corroborator: KILL -> verdict 'go' -> Stage-4 handoff;
// SURVIVE -> 'fix-first' -> back to Stage 2, bounded x2 -> escalate (no handoff).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { IMPLEMENT_REVIEW, loadWorkflow, countOf, deepHasValue, survivorCount, survivorsInclude } from './_harness.mjs';

function oneFinding(severity) {
  return {
    id: 'find-1',
    severity,
    where: 'backend/src/genome/stub.py:1',
    claim: 'stub claim',
    evidence: 'stub evidence',
    refutable_claim: 'stub refutable claim',
    suggested_fix: 'stub fix',
    confidence: 0.5,
  };
}

async function reviewOneFinding(severity, extra = {}) {
  const r = await loadWorkflow(IMPLEMENT_REVIEW, {
    tier: 2,
    deepT2: true,
    reviewLenses: ['convention-compliance'],
    lensFindings: { 'convention-compliance': [oneFinding(severity)] },
    findingSeverity: severity,
    ...extra,
  });
  assert.equal(r.error, null, 'implement-review errored: ' + (r.error && r.error.message));
  return r;
}

// ---- severity -> skeptic count.
test('a single blocker is verified by 2-3 distinct-angle skeptics', async () => {
  // all-refuted => killed in one pass (no fix-first loop to inflate the count).
  const { calls } = await reviewOneFinding('blocker', { verifierVotes: [true, true, true] });
  const n = countOf(calls, 'finding-verifier');
  assert.ok(n >= 2 && n <= 3, `a blocker must be verified by 2-3 skeptics (saw ${n}); ample budget/deep_T2 => 3, budget-tight => 2`);
});

test('a single warn is verified by exactly 1 skeptic', async () => {
  const { calls } = await reviewOneFinding('warn', { verifierVotes: [true] });
  assert.equal(countOf(calls, 'finding-verifier'), 1, 'a warn gets exactly 1 verifier skeptic');
});

test('a single nit is NOT verified (logged, not blocked)', async () => {
  const { calls } = await reviewOneFinding('nit');
  assert.equal(countOf(calls, 'finding-verifier'), 0, 'a nit is logged and batched, never verified');
});

// ---- strict-majority survival (D6).
test('strict-majority KILLS a minority/tie not-refuted blocker (absent from stage3.survivors)', async () => {
  // votes [not-refuted, refuted, refuted]: 2 skeptics -> [nr, r] TIE -> killed;
  // 3 skeptics -> [nr, r, r] minority -> killed.
  const { calls, result } = await reviewOneFinding('blocker', { verifierVotes: [false, true, true] });
  // DIRECT (fix #2): the verified survivor set on the pre-gate package excludes the blocker.
  assert.equal(survivorCount(result), 0, 'a killed (tie/minority) blocker must be ABSENT from result.stage3.survivors (0 survivors)');
  assert.equal(survivorsInclude(result, 'find-1'), false, 'the killed blocker must not appear in result.stage3.survivors');
  // Corroborating route observable: killed => verdict 'go' => Stage-4 handoff reached, never fix-first.
  assert.ok(countOf(calls, 'handoff-assembler') >= 1, "a killed blocker yields verdict 'go' -> Stage-4 handoff is reached");
  assert.equal(deepHasValue(result, 'fix-first'), false, 'a killed blocker must NOT route fix-first');
});

test('strict-majority lets a majority-not-refuted blocker SURVIVE (present in stage3.survivors)', async () => {
  // every skeptic fails to refute => majority-not-refuted => survives.
  const { calls, result } = await reviewOneFinding('blocker', { verifierDefaultRefuted: false });
  // DIRECT (fix #2): the verified survivor set on the pre-gate package contains the blocker.
  assert.equal(survivorCount(result), 1, 'a surviving blocker must be PRESENT in result.stage3.survivors (1 survivor)');
  assert.equal(survivorsInclude(result, 'find-1'), true, 'the surviving blocker must appear in result.stage3.survivors');
  // Corroborating route observable: survives => fix-first => back to Stage 2, bounded x2 -> escalate; handoff never reached.
  assert.equal(countOf(calls, 'handoff-assembler'), 0, 'a surviving blocker routes back to Stage 2 (fix-first), never to Stage-4 handoff');
});

// ---- the GENUINE even-tie: exactly 2 skeptics, [refuted, not-refuted], 1 > 2/2 -> KILLED (fix #3).
// The 2-skeptic strict-majority TIE path is unreachable for the suites above (no budget set =>
// 3 skeptics always; Suite 4 forces synthVerdict). Here a tight-but-not-exhausted budget shaves
// the blocker's skeptic fan-out from 3 to 2 (finding-verifier.md "2-3 skeptics"; the
// budget-tight -> 2 narrowing), and a split vote [refuted, not-refuted] produces the genuine
// even tie. Strict-majority (plan-v2-delta D6 + finding-034 refute-by-default): survives iff
// NOT-refuted > half; here NOT-refuted=1, half=2/2=1, and 1 > 1 is FALSE -> the tie KILLS.
//
// BUDGET-INTERFACE NOTE (flagged): `{ total: 5 }` (with a small per-call cost, startSpent 0)
// is a tight-but-not-exhausted budget that empirically lands in the 2-skeptic band (vs >=3
// skeptics under an ample/null budget) WITHOUT tripping the Suite-4 exhaustion break (which
// needs a pre-exhausted startSpent). The EXPECTED OUTPUTS asserted here — exactly 2 skeptics
// and the even-tie KILL — are spec-pinned; the specific budget magnitude is the stimulus that
// reaches the spec's "budget-tight" regime. If the port retunes the budget-tight threshold,
// retune this budget; the assertions stand.
test('budget-tight even tie: exactly 2 skeptics + [refuted, not-refuted] KILLS the blocker (1 > 2/2)', async () => {
  const { calls, result } = await reviewOneFinding('blocker', {
    verifierVotes: [true, false], // skeptic 1 refutes, skeptic 2 does not -> even tie
    budget: { total: 5, perCall: 1, startSpent: 0 }, // tight-but-not-exhausted -> 2 skeptics
  });
  // exactly 2 skeptics fanned out and both returned (the even number that makes the tie possible).
  assert.equal(countOf(calls, 'finding-verifier'), 2, 'a tight-but-not-exhausted budget must shave the blocker to exactly 2 distinct-angle skeptics');
  // the genuine 1 > 2/2 even tie KILLS the blocker (refute-by-default: a tie is not a majority-not-refuted).
  assert.equal(survivorCount(result), 0, 'the 2-skeptic even tie [refuted, not-refuted] must KILL the blocker -> 0 survivors in result.stage3.survivors');
  assert.equal(survivorsInclude(result, 'find-1'), false, 'the even-tie-killed blocker must be ABSENT from result.stage3.survivors');
  // the kill is a clean strict-majority kill, not a budget-exhaustion escalation (that path names the budget).
  assert.equal(deepHasValue(result, 'fix-first'), false, 'an even-tie KILL routes go, not fix-first');
});

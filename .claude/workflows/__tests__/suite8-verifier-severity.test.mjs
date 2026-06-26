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
// OBSERVABILITY NOTE: the workflow's verifyFresh computes strict-majority and forwards the
// survivor set to review-synthesizer, whose go/fix-first verdict routes Stage-4. The KILL
// observable is therefore handoff-assembler PRESENCE (go), and SURVIVE is its ABSENCE
// (fix-first routes back to Stage 2, bounded x2 -> escalate). The vote sequences are
// crafted so the strict-majority OUTCOME is identical whether the workflow ran 2 or 3
// skeptics, so the test is robust to the budget-driven 2-vs-3 skeptic count.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { IMPLEMENT_REVIEW, loadWorkflow, countOf, deepHasValue } from './_harness.mjs';

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
test('strict-majority KILLS a minority/tie not-refuted blocker (survives === false)', async () => {
  // votes [not-refuted, refuted, refuted]: 2 skeptics -> [nr, r] TIE -> killed;
  // 3 skeptics -> [nr, r, r] minority -> killed. Killed => go => handoff is assembled.
  const { calls, result } = await reviewOneFinding('blocker', { verifierVotes: [false, true, true] });
  assert.ok(countOf(calls, 'handoff-assembler') >= 1, "a killed (tie/minority) blocker yields verdict 'go' -> Stage-4 handoff is reached");
  assert.equal(deepHasValue(result, 'fix-first'), false, 'a killed blocker must NOT route fix-first');
});

test('strict-majority lets a majority-not-refuted blocker SURVIVE (routes fix-first, not handoff)', async () => {
  // every skeptic fails to refute => survives => fix-first => bounded x2 -> escalate;
  // handoff is never reached.
  const { calls } = await reviewOneFinding('blocker', { verifierDefaultRefuted: false });
  assert.equal(countOf(calls, 'handoff-assembler'), 0, 'a surviving blocker routes back to Stage 2 (fix-first), never to Stage-4 handoff');
});

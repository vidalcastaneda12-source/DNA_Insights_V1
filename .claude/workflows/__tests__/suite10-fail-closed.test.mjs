// Suite 10 — fail-closed (this round's fix #4). The team's review/plan adjudication must
// FAIL CLOSED: an infra crash (a member returning `null`) is NOT a clean signal and must
// never be silently read as a pass/refutation. These are authored TEST-FIRST against the
// contracts — some are expected RED until the implementer lands the corresponding guards.
//
// from: finding-034 (refute-by-default — "default to refuted when uncertain"; the asymmetry
//       protects the human boundary); .claude/agents/finding-verifier.md (a finding is killed
//       ONLY when a majority of skeptics REFUTE — a crash is not a refutation);
//       .claude/agents/plan-auditor.md (verdict ready|revise|escalate; "default to skepticism")
//       + .claude/agents/plan-premortem.md (proceed|revise|probe-first); .claude/agents/
//       phi-pii-guardian.md + regression-hunter.md (factor-gated SAFETY lenses — a dropped
//       safety lens on a data/anchor surface must not be silently swallowed); scope-run.md
//       §"Stage 1"/§"Stage 3"; this round's fix #4 (null-skeptic / factor-gated-drop /
//       auditor-pool-empty / premortem-empty must escalate-or-retain, never silently pass).
//
// INTERFACE-LEVEL (not implementation internals): every assertion reads only the documented
// package fields (`result.stage3.survivors`, `result.audit.verdict`, `result.premortem.recommend`,
// `result.route_to`/escalation) and `log()` output — never the workflow's private logic.
//
// CRASH MODEL: a crashed member is injected as a stub that resolves `null` (the engine's
// parallel() also yields a null slot on async rejection). The harness schema-guard (fix #5)
// deliberately SKIPS null returns, so a null models a CRASH the workflow must handle — not a
// stub shape-drift.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { IMPLEMENT_REVIEW, PLAN_PHASE, loadWorkflow, countOf, deepFindString, survivorCount } from './_harness.mjs';

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
const escalates = (result) => deepFindString(result, 'escalat'); // route_to / escalation_reason carry the word
const logHits = (logs, who, signal) => (logs || []).some((l) => who.test(l) && signal.test(l));

// ===========================================================================
// (a) NULL-SKEPTIC fail-closed: a blocker whose finding-verifier skeptics all return null
//     (crashed infra) must NOT be silently killed — a crash is not a refutation.
// ===========================================================================
test('null finding-verifier skeptics do NOT silently kill a blocker (crash != refutation)', async () => {
  const { result, calls } = await loadWorkflow(IMPLEMENT_REVIEW, {
    tier: 2,
    deepT2: true,
    reviewLenses: ['convention-compliance'],
    lensFindings: { 'convention-compliance': [oneFinding('blocker')] },
    findingSeverity: 'blocker',
    returns: { 'finding-verifier': () => null }, // every skeptic crashes -> null vote
  });
  const handoff = countOf(calls, 'handoff-assembler');
  const retained = survivorCount(result) >= 1;
  const escalated = escalates(result);
  // The crash must surface as RETAIN-or-ESCALATE — never a clean Stage-4 go-handoff with the
  // blocker silently dropped purely because its skeptics returned null.
  assert.ok(retained || escalated, 'a blocker whose finding-verifier skeptics return null must be RETAINED or ESCALATED — a crash is not a refutation, so it must not be silently killed');
  assert.equal(handoff >= 1 && !escalated, false, 'null skeptics must not yield a clean go-handoff as if the blocker were confirmed-refuted');
});

// ===========================================================================
// (b) FACTOR-GATED SAFETY-LENS DROP: a gated safety lens (phi-pii-guardian / regression-hunter)
//     that returns null must ESCALATE the review (not 'go') AND log() the drop.
// ===========================================================================
test('a factor-gated safety lens (phi-pii-guardian) returning null escalates the review and logs the drop', async () => {
  const { result, logs } = await loadWorkflow(IMPLEMENT_REVIEW, {
    tier: 2,
    deepT2: true,
    changeClass: ['cli', 'annotation'], // a data/privacy surface => the safety lens is gated ON
    reviewLenses: ['convention-compliance', 'phi-pii-guardian'],
    returns: { 'phi-pii-guardian': () => null }, // the safety lens crashes
  });
  assert.ok(escalates(result), 'a dropped (null) factor-gated SAFETY lens must escalate the review, not silently route go — a safety lens cannot be assumed-clean when it crashed');
  assert.ok(logHits(logs, /phi-pii-guardian/i, /(drop|null|escalat|crash|fail|degrad)/i), 'the dropped safety lens must be log()-ged (which lens, and that it was dropped) so the gap is observable');
});

// ===========================================================================
// (c) AUDITOR-POOL-EMPTY: all plan-auditor skeptics null => merged audit verdict 'escalate',
//     never 'ready'. (plan-auditor "default to skepticism"; an unaudited plan is not ready.)
// ===========================================================================
test('all plan-auditor skeptics null => merged audit verdict escalate, never ready', async () => {
  const { result } = await loadWorkflow(PLAN_PHASE, {
    tier: 2,
    deepT2: false, // tier-2 runs an auditor PANEL (contract + architecture-fit) -> all crash
    premortemRecommend: 'proceed', // isolate the auditor signal (premortem does not independently escalate)
    returns: { 'plan-auditor': () => null },
  });
  const verdict = result && result.audit && result.audit.verdict;
  assert.notEqual(verdict, 'ready', 'an all-crashed auditor pool must NOT yield a ready plan — an unaudited plan is not ready (fail closed)');
  assert.equal(verdict, 'escalate', "a fully-null plan-auditor pool must merge to verdict 'escalate' (route to VSC-User), not pass the plan through");
});

// ===========================================================================
// (d) PREMORTEM-EMPTY: all plan-premortem skeptics null => conservative recommendation
//     (NOT 'proceed'); the fan-out drop-count is log()-ged.
// ===========================================================================
test('all plan-premortem skeptics null => conservative recommendation (not proceed) + drop-count logged', async () => {
  const { result, logs } = await loadWorkflow(PLAN_PHASE, {
    tier: 2,
    deepT2: false, // tier-2 runs 2 premortem skeptics -> both crash
    auditorVerdict: 'ready', // isolate the premortem signal
    returns: { 'plan-premortem': () => null },
  });
  const recommend = result && result.premortem && result.premortem.recommend;
  assert.notEqual(recommend, 'proceed', 'an all-crashed premortem pool must NOT recommend proceed — with no failure-prediction signal the conservative default holds (fail closed)');
  assert.ok(logHits(logs, /premortem/i, /(drop|null|crash|fail|0\s*\/|of\s*\d)/i), 'the premortem fan-out drop-count (how many skeptics returned null) must be log()-ged so the degraded coverage is observable');
});

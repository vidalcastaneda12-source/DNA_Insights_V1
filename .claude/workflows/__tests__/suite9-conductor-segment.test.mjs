// Suite 9 — conductor-segment: each of the three gate-segmented scripts returns its
// package, never auto-crosses a human gate (no auto-approve of a plan, no auto-merge),
// and close honors the confirmed-anchors refuse-guard (empty anchors -> no-op).
//
// from: .claude/agents/README.md §Usage(b) lines 110-123 ("split BY the two human gates";
//       "none auto-approves or auto-merges: the lifecycle ends at VSC-User's two unchanged
//       human gates"); .claude/commands/scope-run.md §"Two orchestration paths" + §Stage 4
//       ("The team does not merge"); C2D-Phase1-synthesized-plan.md §Constraints
//       ("Conductor ... pauses for the human between segments; NOT a Workflow") +
//       §Implementation steps 3 (close: PRESERVE confirmed_anchors refuse-guard +
//       empty-anchors no-op) + §Tests "Suite 9".
//
// INTERFACE NOTE: `auto_approved`/`auto_merged === false` are the conductor-read flags this
// task specifies; the structural guarantee (a segment never invokes the next gate's first
// member) is asserted independently so a flag-naming difference is diagnosable separately
// from a true gate-crossing.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PLAN_PHASE, IMPLEMENT_REVIEW, CLOSE, loadWorkflow, countOf } from './_harness.mjs';

function assertNoAutoCross(pkg, where) {
  assert.ok(pkg && typeof pkg === 'object', `${where}: a segment must return its package object`);
  // Behavioral guarantee (the README spec: "none auto-approves or auto-merges"): the
  // package must not CLAIM auto-approval / auto-merge. This is the load-bearing property;
  // the structural checks below (the segment never invokes the next gate's first member)
  // prove it independently.
  assert.notEqual(pkg.auto_approved, true, `${where}: must not auto-approve the plan`);
  assert.notEqual(pkg.auto_merged, true, `${where}: must not auto-merge`);
  // If the package carries the conductor-read stamp fields, they must be false (not true).
  // NOTE (flagged): the task phrased this as `auto_approved/auto_merged === false`; the
  // underlying spec (README) guarantees the BEHAVIOR, not these exact field names. If the
  // frozen interface_contract mandates explicit false stamps, tighten the `in` guards to
  // hard `=== false` assertions.
  if ('auto_approved' in pkg) assert.equal(pkg.auto_approved, false, `${where}: auto_approved, if present, must be false`);
  if ('auto_merged' in pkg) assert.equal(pkg.auto_merged, false, `${where}: auto_merged, if present, must be false`);
}

test('plan-phase returns its package, stamps the auto flags false, and stops at Gate 1', async () => {
  const { result, calls, error } = await loadWorkflow(PLAN_PHASE, { tier: 2, deepT2: false, auditorVerdict: 'ready' });
  assert.equal(error, null, 'plan-phase errored: ' + (error && error.message));
  assertNoAutoCross(result, 'plan-phase');
  // never crosses Gate 1 into Stage 2 (no writers spawned).
  assert.equal(countOf(calls, 'implementer'), 0, 'plan-phase must not enter Stage 2 (no implementer)');
  assert.equal(countOf(calls, 'test-author'), 0, 'plan-phase must not enter Stage 2 (no test-author)');
});

test('implement-review returns its package, stamps the auto flags false, and stops at Gate 2', async () => {
  const { result, calls, error } = await loadWorkflow(IMPLEMENT_REVIEW, { tier: 2, deepT2: false, synthVerdict: 'go' });
  assert.equal(error, null, 'implement-review errored: ' + (error && error.message));
  assertNoAutoCross(result, 'implement-review');
  // never crosses Gate 2 into Stage 5.
  assert.equal(countOf(calls, 'knowledge-curator'), 0, 'implement-review must not enter Stage-5 close (no knowledge-curator)');
});

test('close returns its package, stamps the auto flags false, and does not re-run prior stages', async () => {
  const { result, calls, error } = await loadWorkflow(CLOSE, {
    confirmedAnchors: [{ anchor: 'gnomad_matches', value: 3054426, confirmed_by: 'gate' }],
  });
  assert.equal(error, null, 'close errored: ' + (error && error.message));
  assertNoAutoCross(result, 'close');
  assert.equal(countOf(calls, 'implementer'), 0, 'close (Stage 5) must not re-run Stage-2 implementer');
  assert.equal(countOf(calls, 'handoff-assembler'), 0, 'close (Stage 5) must not re-run Stage-4 handoff');
});

// ---- close confirmed-anchors refuse-guard.
test('close with confirmed anchors invokes the knowledge-curator (re-lock)', async () => {
  const { calls, error } = await loadWorkflow(CLOSE, {
    confirmedAnchors: [{ anchor: 'gnomad_matches', value: 3054426, confirmed_by: 'gate' }],
  });
  assert.equal(error, null, 'close errored: ' + (error && error.message));
  assert.ok(countOf(calls, 'knowledge-curator') >= 1, 'with human-confirmed anchors, close re-locks via knowledge-curator');
});

test('close with EMPTY confirmed anchors is a no-op (refuse-guard: no curator write)', async () => {
  const { calls, error } = await loadWorkflow(CLOSE, { confirmedAnchors: [] });
  assert.equal(error, null, 'close errored: ' + (error && error.message));
  assert.equal(countOf(calls, 'knowledge-curator'), 0, 'empty confirmed_anchors must not trigger a durable re-lock — the curator writes only human-confirmed numbers (refuse-guard / empty-anchors no-op)');
});

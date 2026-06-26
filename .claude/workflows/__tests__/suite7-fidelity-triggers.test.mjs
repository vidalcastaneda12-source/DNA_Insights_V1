// Suite 7 — fidelity-triggers: the four trigger-gated Stage-2 members (GAP-4) fire on
// the real triggers; fan-out-implementer REPLACES the single implementer (D7); none of
// the four fire for THIS scope's manifest (a JS-only port: no schema, narrow blast,
// green expected to pass — synthesized-plan riskiest-assumption #5).
//
// from: C2D-Phase1-synthesized-plan.md §Implementation steps 5 (GAP-4: green red ->
//       test-triage -> deep-debugger tier>=2; schema -> schema-change-executor;
//       wide -> fan-out-implementer isolation:'worktree'; NONE fire this scope) + §Tests
//       "Suite 7 fidelity-triggers"; plan-v2-delta D7 (fan-out REPLACES, does not augment
//       — do not also invoke the implementer; the 4 triggers' live invocation is recorded
//       as residual-risk: stubs validate SELECTION logic, not the live writer semantics).
//
// SELECTION-ONLY ATTESTATION: these tests prove the workflow's trigger SELECTION against
// stubs; they do NOT exercise the live engine's invocation / worktree writer semantics
// (deferred-unverified residual risk per D7).
//
// INTERFACE ASSUMPTION FLAGGED: the "wide & independent" gate is signalled here via a
// large blast_radius.imports_touched plus explicit independent/wide flags; the exact
// gating field is an interface assumption. The fan-out path asserts implementer count 0
// (D7 literal "REPLACES, not augments") — if the port keeps an interface-freeze
// implementer stub pass alongside fan-out, that is the reconciliation point.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { IMPLEMENT_REVIEW, loadWorkflow, callsOf, countOf, c2dManifestHooks } from './_harness.mjs';

async function review(hooks) {
  // Resilient by design: the four trigger-gated members fire in STAGE 2, before the
  // Stage-3 review seam, so their SELECTION is observable independently of any later
  // seam error. We assert on the recorded Stage-2 calls, not on clean completion.
  return loadWorkflow(IMPLEMENT_REVIEW, { synthVerdict: 'go', ...hooks });
}

// ---- schema trigger -> schema-change-executor.
test('change_class ⊇ schema fires schema-change-executor', async () => {
  const { calls } = await review({ tier: 2, deepT2: true, changeClass: ['schema', 'ddl', 'pipeline'], rebuildRequired: true });
  assert.ok(countOf(calls, 'schema-change-executor') >= 1, "a schema/ddl change_class must fire schema-change-executor");
});

// ---- wide & independent blast radius -> fan-out-implementer REPLACES implementer.
test('wide & independent blast_radius fires fan-out-implementer with isolation worktree and REPLACES the implementer (D7)', async () => {
  const wide = {
    imports_touched: Array.from({ length: 25 }, (_, i) => `mod_${i}`),
    tests_covering: Array.from({ length: 10 }, (_, i) => `t_${i}`),
    independent: true,
    wide: true,
    parallelizable: true,
  };
  const { calls } = await review({ tier: 2, deepT2: true, blastRadius: wide });
  const fanout = callsOf(calls, 'fan-out-implementer');
  assert.ok(fanout.length >= 1, 'a wide & independent blast_radius must fire fan-out-implementer');
  for (const c of fanout) {
    assert.equal(c.isolation ?? c.opts?.isolation, 'worktree', "fan-out-implementer must be invoked with isolation 'worktree'");
  }
  // D7: it REPLACES the single implementer — the implementer is not also invoked.
  assert.equal(countOf(calls, 'implementer'), 0, 'fan-out-implementer REPLACES the implementer (two writers would collide) — the implementer must not also run');
});

// ---- green red -> test-triage ; routed -> deep-debugger (tier >= 2).
test('a real green-red fires test-triage', async () => {
  const { calls } = await review({ tier: 2, deepT2: true, greenRed: true, triageRoute: 'implementer' });
  assert.ok(countOf(calls, 'test-triage') >= 1, 'a red dev-loop must route to test-triage (classify before fixing)');
});

test('green-red routed gnarly fires deep-debugger at tier >= 2', async () => {
  const { calls } = await review({ tier: 2, deepT2: true, greenRed: true, triageClass: 'real-regression', triageRoute: 'deep-debugger' });
  assert.ok(countOf(calls, 'deep-debugger') >= 1, 'test-triage routing to deep-debugger at tier>=2 must spin up deep-debugger');
});

// ---- THIS scope's manifest: none of the four fire; the single implementer DOES run.
test("THIS scope's manifest (JS-only port) fires none of the four trigger-gated members", async () => {
  const { calls } = await review(c2dManifestHooks({ synthVerdict: 'go' }));
  for (const m of ['schema-change-executor', 'fan-out-implementer', 'test-triage', 'deep-debugger']) {
    assert.equal(countOf(calls, m), 0, `${m} must NOT fire for C2D-Phase1 (no schema, narrow blast, green passes)`);
  }
  assert.ok(countOf(calls, 'implementer') >= 1, 'with no fan-out trigger, the single implementer is the writer for this scope');
});

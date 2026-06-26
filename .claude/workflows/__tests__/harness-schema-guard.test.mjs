// harness-schema-guard — the root-cause guard (this round's fix #5). The recorder now
// validates every schema-bearing agent() return against the schema's `required` keys and
// records any drift onto `loadWorkflow(...).schemaViolations`. This is what makes a future
// stub-contract drift REDDEN a test instead of silently dead-keying production — the failure
// mode that let the finding-verifier stub return top-level `refuted` (no `survives`) while
// the workflow's strict-majority path read the contract's top-level `survives` and got
// `undefined` in every test.
//
// from: this round's fix #5 (validate stub returns vs schema.required; fail loud on drift) +
//       fix #1 (.claude/agents/finding-verifier.md Output: top-level `survives`, `refuted`
//       only inside votes[]); plan-v2-delta D1 (schema.required is the workflow's strict read).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  assertStubSatisfiesSchema,
  documentedOutputKeys,
  IMPLEMENT_REVIEW,
  PLAN_PHASE,
  CLOSE,
  loadWorkflow,
} from './_harness.mjs';

const blocker = {
  id: 'find-1', severity: 'blocker', where: 'backend/src/genome/stub.py:1',
  claim: 'c', evidence: 'e', refutable_claim: 'rc', suggested_fix: 'f', confidence: 0.5,
};
const reviewBlocker = (returns) =>
  loadWorkflow(IMPLEMENT_REVIEW, {
    tier: 2, deepT2: true, reviewLenses: ['convention-compliance'],
    lensFindings: { 'convention-compliance': [blocker] }, findingSeverity: 'blocker', returns,
  });

// ---- the validation PRIMITIVE (pure, throwing).
test('assertStubSatisfiesSchema throws when a required key is missing (the drift the recorder must catch)', () => {
  assert.throws(
    () => assertStubSatisfiesSchema('finding-verifier', { required: ['survives'] }, { id: 'x', refuted: true, votes: [] }),
    /stub-contract drift/,
    'a return missing a schema-required key must throw (this is the historical finding-verifier bug)',
  );
});

test('assertStubSatisfiesSchema accepts a contract-complete return', () => {
  assert.doesNotThrow(() =>
    assertStubSatisfiesSchema('finding-verifier', { required: ['survives'] }, { id: 'x', survives: false, votes: [{ angle: 'a', refuted: true, reason: 'r' }], verified_severity: 'blocker', confidence: 0.5 }),
  );
});

test('assertStubSatisfiesSchema SKIPS null/undefined returns (an injected crash is not a shape-drift)', () => {
  assert.doesNotThrow(() => assertStubSatisfiesSchema('finding-verifier', { required: ['survives'] }, null), 'null = crashed infra, handled by the workflow fail-closed guard — not a stub drift');
  assert.doesNotThrow(() => assertStubSatisfiesSchema('finding-verifier', { required: ['survives'] }, undefined));
});

test('assertStubSatisfiesSchema is a no-op for a schema-less call (prose / verdict-less members)', () => {
  assert.doesNotThrow(() => assertStubSatisfiesSchema('handoff-assembler', undefined, 'a prose handoff string'));
  assert.doesNotThrow(() => assertStubSatisfiesSchema('handoff-assembler', null, { anything: 1 }));
});

test('assertStubSatisfiesSchema throws when a schema-bearing call returns a non-object', () => {
  assert.throws(() => assertStubSatisfiesSchema('review-synthesizer', { required: ['verdict'] }, 'not-an-object'), /stub-contract drift/);
});

test('assertStubSatisfiesSchema derives required from a properties{}-style schema too', () => {
  assert.throws(() => assertStubSatisfiesSchema('plan-auditor', { properties: { verdict: {}, findings: {} } }, { findings: [] }), /stub-contract drift/, 'missing `verdict` from a properties-style schema must still throw');
  assert.doesNotThrow(() => assertStubSatisfiesSchema('plan-auditor', { properties: { verdict: {} } }, { verdict: 'ready' }));
});

// ---- the CONTRACT anchor that ties the stub fix to the source of truth.
test('finding-verifier.md documents top-level `survives` and NOT top-level `refuted`', () => {
  const keys = documentedOutputKeys('finding-verifier');
  assert.equal(keys.has('survives'), true, 'finding-verifier.md Output has a top-level `survives` — the field the workflow reads for strict-majority');
  assert.equal(keys.has('refuted'), false, '`refuted` is documented ONLY inside votes[], never at the top level — the stub must not put it there');
});

// ---- the recorder is WIRED: a drifting schema-bearing return is recorded (robust to the
//      workflow's inlined agent()/retry seam swallowing a thrown error).
test('recorder RECORDS a schema-bearing drift: the historical finding-verifier shape reddens', async () => {
  const { schemaViolations } = await reviewBlocker({
    'finding-verifier': () => ({ id: 'x', refuted: true, votes: [{ angle: 'a', refuted: true, reason: 'r' }] }), // OLD shape: no top-level `survives`
  });
  assert.ok(schemaViolations.length >= 1, 'injecting the historical finding-verifier shape (top-level refuted, no survives) must record at least one schema violation');
  const fv = schemaViolations.find((v) => v.agentType === 'finding-verifier');
  assert.ok(fv, 'the recorded violation must name finding-verifier');
  assert.ok(fv.required.includes('survives'), 'the violation must show the workflow REQUIRED `survives`');
  assert.match(fv.message, /stub-contract drift/, 'the recorded violation message must name the drift');
});

// ---- the NO-DRIFT regression: every live default stub satisfies the schema its call carries.
//      (This is the assertion that, had it existed, would have caught the original bug.)
test('default stubs satisfy every schema-bearing call across all three segments (zero drift)', async () => {
  const planPhase = await loadWorkflow(PLAN_PHASE, { tier: 2, deepT2: true });
  const review = await reviewBlocker({}); // exercises the (now-fixed) finding-verifier schema {required:['survives']}
  const close = await loadWorkflow(CLOSE, { confirmedAnchors: [{ anchor: 'gnomad_matches', value: 3054426, confirmed_by: 'gate' }] });
  for (const [seg, run] of [['plan-phase', planPhase], ['implement-review', review], ['close', close]]) {
    assert.deepEqual(run.schemaViolations, [], `${seg}: a default-stub return dropped a schema-required key:\n` + run.schemaViolations.map((v) => v.message).join('\n'));
  }
});

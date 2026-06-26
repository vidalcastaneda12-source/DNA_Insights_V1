// Suite 2 — schema-contract: every schema-bearing agent() call's required keys are a
// subset of that member's documented Output keys; prose/verdict-less members are
// called schema-less.
//
// from: C2D-Phase1-synthesized-plan.md §Tests "Suite 2 schema-contract" + §Verification
//       EC2; plan-v2-delta D1 (the GENERAL RULE + TEST: `schema.required ⊆ the agent .md's
//       documented Output JSON keys` — a schema requiring a key the member doesn't emit
//       forces StructuredOutput fabrication / retry-to-null, a runtime-only failure stubs
//       can't catch). handoff-assembler returns PROSE => schema-LESS; architect-reviewer
//       and the in-loop silent-failure-hunter are verdict-less.
//
// AMBIGUITY FLAGGED (see returned report): this task lists architect-reviewer +
// in-loop silent-failure-hunter as "called SCHEMA-LESS", while plan-v2-delta D1 gives
// architect-reviewer a "findings-only schema (no verdict)". Both satisfy the load-bearing
// invariant — the call must NOT require a key the member doesn't emit (notably `verdict`,
// which neither .md emits). This suite asserts that unifying invariant (so a D1-compliant
// findings-only schema is NOT falsely flagged), plus the strict schema-less rule for the
// one unambiguous prose member, handoff-assembler.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  PLAN_PHASE,
  IMPLEMENT_REVIEW,
  CLOSE,
  loadWorkflow,
  documentedOutputKeys,
  schemaRequiredKeys,
  callsOf,
  PROSE_MEMBERS,
  VERDICTLESS_MEMBERS,
  makeManifest,
} from './_harness.mjs';

// Drive each segment along a broad happy path so the schema-bearing calls are captured.
// Resilient to a downstream crash: the schema of every call that WAS made is still
// validatable even if a later seam errors (so the D1 general rule keeps its independent
// value). The handoff/terminal assertions below rely on the segment running far enough.
async function gatherCalls() {
  const planTier2 = await loadWorkflow(PLAN_PHASE, { tier: 2, deepT2: true });
  const review = await loadWorkflow(IMPLEMENT_REVIEW, {
    tier: 2,
    deepT2: true,
    reviewLenses: [
      'convention-compliance',
      'phi-pii-guardian',
      'test-integrity',
      'regression-hunter',
      'silent-failure-hunter',
      'type-design-analyzer',
      'pr-test-analyzer',
      'comment-analyzer',
      'architect-reviewer',
    ],
  });
  const close = await loadWorkflow(CLOSE, { confirmedAnchors: [{ anchor: 'x', value: 1, confirmed_by: 'gate' }] });
  return { calls: [...planTier2.calls, ...review.calls, ...close.calls], runs: { planTier2, review, close } };
}

test('every schema-bearing agent() call: schema.required ⊆ the member.md documented Output keys', async () => {
  const { calls } = await gatherCalls();
  const schemaBearing = calls.filter((c) => schemaRequiredKeys(c.schema) !== null);

  // The port REPLACES hand-rolled coerceJson/requireKeys with `schema` for its validated
  // members — at least one schema-bearing call must exist, or the schema mechanism (the
  // core of the port) was never used.
  assert.ok(schemaBearing.length >= 1, 'expected at least one schema-bearing agent() call across the three segments');

  const violations = [];
  for (const c of schemaBearing) {
    const required = schemaRequiredKeys(c.schema);
    const documented = documentedOutputKeys(c.agentType);
    const extra = required.filter((k) => !documented.has(k));
    if (extra.length) {
      violations.push(`${c.agentType}: schema requires ${JSON.stringify(extra)} not in documented Output keys ${JSON.stringify([...documented])}`);
    }
  }
  assert.deepEqual(violations, [], 'schema.required must be a subset of the member.md documented Output keys:\n' + violations.join('\n'));
});

test('handoff-assembler, whenever called, is SCHEMA-LESS (prose output — 0 JSON keys)', async () => {
  // Suite 2's job is the schema SHAPE; the invocation itself is Suite 6's job. So this
  // asserts the contract on any handoff-assembler call that occurs, without requiring the
  // segment to reach Stage 4 (keeping the check independent of a downstream seam crash).
  const { calls } = await gatherCalls();
  for (const m of PROSE_MEMBERS) {
    for (const c of callsOf(calls, m)) {
      assert.equal(schemaRequiredKeys(c.schema), null, `${m} must be called schema-less (its Output is prose); a schema would JSON-coerce a prose handoff`);
    }
  }
});

test('verdict-less members (architect-reviewer, in-loop silent-failure-hunter) never require a verdict key', async () => {
  const { calls } = await gatherCalls();
  for (const m of VERDICTLESS_MEMBERS) {
    const documented = documentedOutputKeys(m);
    assert.equal(documented.has('verdict'), false, `${m}.md does not document a top-level 'verdict' key`);
    for (const c of callsOf(calls, m)) {
      const required = schemaRequiredKeys(c.schema); // null if schema-less (also acceptable)
      if (required) {
        assert.equal(required.includes('verdict'), false, `${m} must not be given a schema requiring 'verdict' (it emits none) — derive the verdict in mergeAudits instead`);
      }
    }
  }
});

// Parser self-tests (guard the SCHEMAS module so the subset checks aren't vacuous).
test('documentedOutputKeys parser: real members vs the prose member', () => {
  assert.ok(documentedOutputKeys('plan-auditor').has('verdict'), "plan-auditor.md documents 'verdict'");
  assert.ok(documentedOutputKeys('review-synthesizer').has('anchors_to_watch'), "review-synthesizer.md documents 'anchors_to_watch'");
  assert.ok(documentedOutputKeys('architect-reviewer').has('findings'), "architect-reviewer.md documents 'findings'");
  assert.equal(documentedOutputKeys('architect-reviewer').has('verdict'), false, 'architect-reviewer.md has NO top-level verdict');
  assert.equal(documentedOutputKeys('handoff-assembler').size, 0, 'handoff-assembler.md Output is prose => 0 documented JSON keys');
  // union-type annotations must not leak the value tokens as keys
  const sd = documentedOutputKeys('scope-dispatcher');
  assert.ok(sd.has('risk_tier') && sd.has('change_class'));
  assert.equal(sd.has('ready'), false);
  assert.equal(sd.has('revise'), false);
});

test('schemaRequiredKeys handles required[] / properties{} / bare object / null', () => {
  assert.deepEqual(schemaRequiredKeys({ required: ['a', 'b'] }), ['a', 'b']);
  assert.deepEqual(schemaRequiredKeys({ properties: { a: 1, b: 2 } }), ['a', 'b']);
  assert.deepEqual(schemaRequiredKeys({ a: 1 }), ['a']);
  assert.equal(schemaRequiredKeys(undefined), null);
  assert.equal(schemaRequiredKeys(null), null);
});

// Manifest fixture realism guard (finding-013): the synthetic manifest carries the keys
// the workflows route on, so the captured call graph is meaningful.
test('makeManifest fixture carries the routing keys the workflows consume', () => {
  const m = makeManifest({ tier: 2, deepT2: true });
  for (const k of ['risk_tier', 'change_class', 'blast_radius', 'review_lenses', 'applicable_anchors', 'deep_T2']) {
    assert.ok(k in m, `manifest fixture must carry '${k}'`);
  }
});

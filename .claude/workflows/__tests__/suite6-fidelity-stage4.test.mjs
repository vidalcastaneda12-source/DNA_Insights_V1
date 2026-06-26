// Suite 6 — fidelity-stage4: the Stage-4 handoff-assembler is wired on the `go` path
// (GAP-3 — it was previously wired nowhere) and is invoked SCHEMA-LESS (it returns a
// prose handoff stored as a string), as the terminal action before the Gate-2 return.
//
// from: C2D-Phase1-synthesized-plan.md §Implementation steps 5 (GAP-3: on 'go' before
//       done(): agent({agentType:'handoff-assembler'}) ... composed_skills) + §Tests
//       "Suite 6 fidelity-stage4 (handoff-assembler before Gate-2 return)"; plan-v2-delta
//       D1 (handoff-assembler is schema-LESS; the seam must NOT JSON-coerce it) + D8
//       (its composed skill set is /handoff + /changelog + /new-finding, not /pr-ready).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { IMPLEMENT_REVIEW, loadWorkflow, callsOf, countOf, schemaRequiredKeys, agentTypesIn } from './_harness.mjs';

async function reviewGo() {
  const r = await loadWorkflow(IMPLEMENT_REVIEW, { tier: 2, deepT2: false, synthVerdict: 'go' });
  assert.equal(r.error, null, 'implement-review errored on go path: ' + (r.error && r.error.message));
  return r;
}

test('on `go`: handoff-assembler is invoked', async () => {
  const { calls } = await reviewGo();
  assert.ok(countOf(calls, 'handoff-assembler') >= 1, "Stage 4 must invoke handoff-assembler on the review verdict 'go' (GAP-3)");
});

test('on `go`: handoff-assembler is called SCHEMA-LESS', async () => {
  const { calls } = await reviewGo();
  for (const c of callsOf(calls, 'handoff-assembler')) {
    assert.equal(schemaRequiredKeys(c.schema), null, 'handoff-assembler must be schema-less — its output is a prose handoff stored as a string, not JSON-coerced (D1)');
  }
});

test('on `go`: handoff-assembler is the terminal agent() call before the Gate-2 return', async () => {
  const { calls } = await reviewGo();
  const types = agentTypesIn(calls);
  assert.equal(types[types.length - 1], 'handoff-assembler', 'handoff-assembler is the last agent() invocation before the Gate-2 return (no member runs after it; /pr-ready is a skill, not an agent call)');
  // Stage 5 must NOT run inside implement-review — the segment stops at Gate 2.
  assert.equal(countOf(calls, 'knowledge-curator'), 0, 'implement-review must not cross Gate 2 into Stage-5 close');
});

// Contrast: on `fix-first`, the handoff is NOT assembled (blockers route back to Stage 2).
test('on `fix-first`: handoff-assembler is NOT invoked', async () => {
  const r = await loadWorkflow(IMPLEMENT_REVIEW, { tier: 2, deepT2: false, synthVerdict: 'fix-first' });
  assert.equal(r.error, null, 'implement-review errored on fix-first path: ' + (r.error && r.error.message));
  assert.equal(countOf(r.calls, 'handoff-assembler'), 0, "handoff-assembler is gated on 'go'; a 'fix-first' verdict routes back to Stage 2, not to handoff");
});

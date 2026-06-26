// Suite 5 — resume-determinism: identical args produce a byte-identical recorded
// agent() sequence, and the bodies contain no wall-clock / RNG nondeterminism source.
//
// from: C2D-Phase1-synthesized-plan.md §Tests "Suite 5 resume-determinism (identical
//       args -> byte-identical sequence + no-Date.now/Math.random scan)"; plan-v2-delta
//       D9 (run the determinism check with budget.total=null so no budget-dependent branch
//       perturbs the sequence) + D2 (nondeterminism-source scan); hard-dialect rule
//       "no Date.now()/Math.random()/argless new Date()".

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  WORKFLOW_PATHS,
  stemOf,
  loadWorkflow,
  loadParts,
  readWorkflowText,
  forbiddenTokensIn,
  NONDETERMINISM_TOKENS,
} from './_harness.mjs';

// Project each recorded call to the load-bearing, comparable fields and serialize.
function sequenceSignature(calls) {
  return JSON.stringify(
    calls.map((c) => ({
      agentType: c.agentType ?? null,
      angle: c.angle ?? null,
      axis: c.axis ?? null,
      lens: c.lens ?? null,
      prompt: c.prompt ?? null,
      schema: c.schema ?? null,
    })),
  );
}

// Fixed hooks -> loadWorkflow derives a deterministic, COMPLETE args string (the
// manifest/plan/confirmed_anchors each segment requires after its gate), so the resume
// (cached-prefix) scenario replays the identical call order. budget.total is null (D9) so
// no budget branch is in play.
const FIXED = (extra) => ({
  tier: 2,
  deepT2: true,
  auditorVerdict: 'ready',
  premortemRecommend: 'proceed',
  synthVerdict: 'go',
  confirmedAnchors: [{ anchor: 'gnomad_matches', value: 3054426, confirmed_by: 'gate' }],
  ...extra,
});

for (const path of WORKFLOW_PATHS) {
  const stem = stemOf(path);

  test(`[${stem}] identical args -> byte-identical agent() sequence`, async () => {
    const hooks = FIXED();
    const a = await loadWorkflow(path, hooks);
    const b = await loadWorkflow(path, hooks);
    assert.equal(a.error, null, `${stem} errored (run a): ` + (a.error && a.error.message));
    assert.equal(b.error, null, `${stem} errored (run b): ` + (b.error && b.error.message));
    assert.equal(sequenceSignature(a.calls), sequenceSignature(b.calls), `${stem}: two runs with identical args must yield a byte-identical agent() sequence`);
  });

  test(`[${stem}] body has no Date.now / Math.random / new Date() nondeterminism source`, () => {
    const { body } = loadParts(readWorkflowText(path));
    const found = forbiddenTokensIn(body, NONDETERMINISM_TOKENS);
    assert.deepEqual(found, [], `${stem}.js must contain none of: ${NONDETERMINISM_TOKENS.join(' ')} (nondeterminism breaks byte-identical resume)`);
  });
}

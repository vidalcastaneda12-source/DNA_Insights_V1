// seam-dialect — the workflows invoke the parallel/pipeline engine seams with the
// probe-confirmed signatures: parallel(thunks) takes an ARRAY of thunks; pipeline(items,
// ...stages) takes VARIADIC function stages (NOT an array).
//
// from: C2D-Phase1-probe-results.md §"Confirmed" (parallel/pipeline) + the task brief's
//       EMPIRICAL engine semantics: "parallel(thunks) and pipeline(items, ...stages)
//       (VARIADIC stages — NOT an array)". This is the focused, named oracle for a seam
//       misuse that would otherwise only surface as an opaque downstream crash: passing
//       stages as an array makes `stages = [[...]]` on the variadic engine, so a stage is
//       an array -> `stage(item)` throws -> the run crashes on the real engine.
//
// The shapes are recorded at seam-invocation, so they are available even when a later
// stage throws and the run errors.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { WORKFLOW_PATHS, stemOf, loadWorkflow, makeManifest } from './_harness.mjs';

async function runForShapes(path) {
  // a broad config so the seams are exercised; errors are tolerated (shapes are recorded
  // at invocation, before any propagating stage-throw).
  const hooks = {
    tier: 2,
    deepT2: true,
    synthVerdict: 'go',
    confirmedAnchors: [{ anchor: 'x', value: 1, confirmed_by: 'gate' }],
    argsObject: {
      scope_id: 'C2D-Phase1',
      manifest: makeManifest({ tier: 2, deepT2: true }),
      plan: { implementation_plan: [], tests: { new: [] }, verification: {} },
      confirmed_anchors: [{ anchor: 'x', value: 1, confirmed_by: 'gate' }],
    },
  };
  return loadWorkflow(path, hooks);
}

for (const path of WORKFLOW_PATHS) {
  const stem = stemOf(path);

  test(`[${stem}] every pipeline() call passes VARIADIC function stages (not an array)`, async () => {
    const { pipelineShapes } = await runForShapes(path);
    for (const s of pipelineShapes) {
      assert.equal(s.itemsIsArray, true, `${stem}: pipeline()'s first arg (items) must be an array`);
      assert.ok(
        s.stageTypes.length >= 1 && s.stageTypes.every((t) => t === 'function'),
        `${stem}: pipeline() stages must be VARIADIC functions, got ${JSON.stringify(s.stageTypes)} — the engine is pipeline(items, ...stages); a single array stage becomes stages=[[...]] and crashes. Spread them: pipeline(items, ...stages)`,
      );
    }
  });

  test(`[${stem}] every parallel() call passes an ARRAY of thunks`, async () => {
    const { parallelShapes } = await runForShapes(path);
    for (const s of parallelShapes) {
      assert.equal(s.argIsArray, true, `${stem}: parallel() takes a single array of thunks`);
      assert.ok(s.thunkTypes.every((t) => t === 'function'), `${stem}: parallel()'s array must contain thunks (functions), got ${JSON.stringify(s.thunkTypes)}`);
    }
  });
}

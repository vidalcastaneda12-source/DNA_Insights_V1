// Suite 1 — construct-check + hard-constraints static scan.
//
// from: C2D-Phase1-synthesized-plan.md §Tests "Suite 1 construct-check/syntax-gate" +
//       §Verification EC1; plan-v2-delta D2 (forbidden-token scan; meta.name===stem;
//       last-top-level-statement-is-return; nested-phases / comment-bait round-trip).
//       GT-2 construct-check is the AsyncFunction body-wrap, NOT raw `node --check`.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  WORKFLOW_PATHS,
  PLAN_PHASE,
  IMPLEMENT_REVIEW,
  CLOSE,
  stemOf,
  constructable,
  loadParts,
  readWorkflowText,
  forbiddenTokensIn,
  endsWithTopLevelReturn,
  findMetaDecl,
  FORBIDDEN_TOKENS,
} from './_harness.mjs';

for (const path of WORKFLOW_PATHS) {
  const stem = stemOf(path);

  // EC1: the engine loads a workflow by extracting meta + wrapping the body in an async
  // function — the per-script syntax gate. (NOT raw node --check, which rejects the dialect.)
  test(`[${stem}] GT-2 construct-check: AsyncFunction body builds`, () => {
    assert.equal(constructable(path), true, `${stem}.js body must build as an AsyncFunction with the 8 hooks injected`);
  });

  // D2: forbidden-token static scan over the post-meta body must be EMPTY.
  test(`[${stem}] forbidden-token scan: no CJS / abstract-runtime / Node-API residue`, () => {
    const { body } = loadParts(readWorkflowText(path));
    const found = forbiddenTokensIn(body, FORBIDDEN_TOKENS);
    assert.deepEqual(found, [], `${stem}.js body must not use any of: ${FORBIDDEN_TOKENS.join(' ')}`);
  });

  // D2: meta.name === filename stem (the conductor launches segments by name; the
  // construct-check strips meta, so nothing else verifies it).
  test(`[${stem}] meta.name === filename stem`, () => {
    const { meta } = loadParts(readWorkflowText(path));
    assert.equal(meta.name, stem, `meta.name must equal the file stem '${stem}'`);
  });

  // D2: the LAST top-level statement must be `return …` (no leftover
  // `planPhase().then(setResult)` IIFE that builds green but resolves undefined).
  test(`[${stem}] last top-level statement is a return`, () => {
    const { body } = loadParts(readWorkflowText(path));
    assert.equal(endsWithTopLevelReturn(body), true, `${stem}.js must terminate with a top-level \`return pkg\``);
  });
}

// Sanity: the three segments are the expected, distinct files (no accidental aliasing).
test('three distinct workflow segments: plan-phase / implement-review / close', () => {
  assert.deepEqual(
    WORKFLOW_PATHS.map(stemOf),
    ['plan-phase', 'implement-review', 'close'],
  );
  assert.notEqual(PLAN_PHASE, IMPLEMENT_REVIEW);
  assert.notEqual(IMPLEMENT_REVIEW, CLOSE);
});

// Harness self-test (guards the meta extractor so Suite 1 cannot pass vacuously):
// a NESTED `phases:[{...},{...}]` literal round-trips AND a header comment containing
// the literal string `export const meta` does NOT false-match (plan-v2-delta D2/D10).
test('harness meta-extractor: nested phases round-trip + comment-bait is not matched', () => {
  const synthetic = [
    '// header: this comment mentions `export const meta` as false-match bait',
    "export const meta = { name: 'demo', phases: [ { title: 'Intake', detail: 'a' }, { title: 'Plan', detail: 'b{not:1}' } ] };",
    '',
    'log("body runs");',
    'return { ok: true };',
    '',
  ].join('\n');
  const { metaSrc } = findMetaDecl(synthetic);
  // The matched literal must be the REAL declaration's object, not the comment's text.
  const meta = Function('"use strict"; return (' + metaSrc + ');')();
  assert.equal(meta.name, 'demo');
  assert.equal(meta.phases.length, 2);
  assert.equal(meta.phases[0].title, 'Intake');
  assert.equal(meta.phases[1].detail, 'b{not:1}');
});

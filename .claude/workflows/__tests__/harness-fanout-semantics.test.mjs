// arch-1 drift-guard — the harness parallel()/pipeline() fan-out seams must keep mirroring the
// engine semantics the C2D-Phase1 load-probe confirmed (and the C2D-Phase2 D7 probe re-confirmed):
// NULL on an async rejection, PROPAGATE on a synchronous throw. finding-034 "C2D-Phase1 residual
// risk" arch-1: the probes retired this at the ENGINE level; this suite exhaustively covers the
// HARNESS-stub mirror so it cannot silently drift from the engine it stands in for. The footgun it
// pins: an async rejection is swallowed to `null`, but a SYNC throw escapes and crashes the run.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makeRecorder } from './_harness.mjs';

function fanout() {
  const rec = makeRecorder({});
  return { parallel: rec.parallel, pipeline: rec.pipeline };
}

// ── parallel(thunks): array in -> array out ────────────────────────────────────────────────

test('parallel: resolved thunks pass their values through in order', async () => {
  const { parallel } = fanout();
  assert.deepEqual(await parallel([() => Promise.resolve('A'), () => Promise.resolve('B')]), ['A', 'B']);
});

test('parallel: an async rejection becomes null in that slot (the call still resolves)', async () => {
  const { parallel } = fanout();
  const out = await parallel([
    () => Promise.resolve('A'),
    () => Promise.reject(new Error('boom')),
    () => Promise.resolve('C'),
  ]);
  assert.deepEqual(out, ['A', null, 'C']);
});

test('parallel: only the rejecting slots null; resolved slots survive', async () => {
  const { parallel } = fanout();
  const out = await parallel([
    () => Promise.resolve(1),
    () => Promise.reject(new Error('x')),
    () => Promise.resolve(3),
    () => Promise.reject(new Error('y')),
  ]);
  assert.deepEqual(out, [1, null, 3, null]);
});

test('parallel: a SYNCHRONOUS throw PROPAGATES out of parallel (does NOT become null)', async () => {
  const { parallel } = fanout();
  await assert.rejects(
    async () =>
      parallel([
        () => Promise.resolve('A'),
        () => {
          throw new Error('sync-thunk');
        },
      ]),
    /sync-thunk/,
  );
});

// ── pipeline(items, ...stages): VARIADIC stages, per-item threading ─────────────────────────

test('pipeline: threads each item through all stages', async () => {
  const { pipeline } = fanout();
  assert.deepEqual(await pipeline([1, 2, 3], (x) => x * 10, (y) => y + 1), [11, 21, 31]);
});

test('pipeline: async stages thread their awaited values', async () => {
  const { pipeline } = fanout();
  assert.deepEqual(await pipeline([1, 2], async (x) => x + 1, async (y) => y * 2), [4, 6]);
});

test('pipeline: a stage async-rejection nulls THAT item; the others continue', async () => {
  const { pipeline } = fanout();
  const out = await pipeline(
    [1, 2, 3],
    (x) => (x === 2 ? Promise.reject(new Error('drop 2')) : Promise.resolve(x)),
    (y) => y * 100,
  );
  assert.deepEqual(out, [100, null, 300]);
});

test('pipeline: a SYNCHRONOUS throw in a stage PROPAGATES (does NOT null the item)', async () => {
  const { pipeline } = fanout();
  await assert.rejects(
    async () =>
      pipeline([1, 2], (x) => {
        if (x === 2) throw new Error('sync-stage');
        return x;
      }),
    /sync-stage/,
  );
});

// drift-check — the inlined agent()/retry seam is byte-identical across the three files;
// + a reversal-gate note (the genome docs check is a Python CLI the green-keeper runs).
//
// from: C2D-Phase1-synthesized-plan.md §Constraints GT-1 ("runtime workflows CANNOT
//       import a sibling lib ... the agent()/retry seam stay INLINE per file") + §Tests
//       "drift-check (inlined seam copies byte-identical) ... reversal-gate (genome docs
//       check exit 0)". Because GT-1 forbids a shared import, the seam is DUPLICATED; this
//       check fails if one copy drifts from the others.
//
// SEAM-LOCATION CONVENTION (interface assumption): the duplicated seam is located by, in
// order: (1) `// agent-seam:start` / `// agent-seam:end` sentinel markers; (2) fallback —
// the smallest brace-balanced block enclosing the first `agent(` call. Delimiting the
// duplicated block with the sentinel is the recommended way to keep a verbatim-duplicated
// seam detectably in sync under GT-1.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { WORKFLOW_PATHS, stemOf, loadParts, readWorkflowText, stripCommentsAndStrings } from './_harness.mjs';

// local brace matcher (string/comment aware) over the original text.
function matchBrace(src, start) {
  let depth = 0;
  let i = start;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    const c2 = src[i + 1];
    if (c === '/' && c2 === '/') {
      while (i < n && src[i] !== '\n') i++;
      continue;
    }
    if (c === '/' && c2 === '*') {
      i += 2;
      while (i < n && !(src[i] === '*' && src[i + 1] === '/')) i++;
      i += 2;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') {
      const q = c;
      i++;
      while (i < n) {
        if (src[i] === '\\') {
          i += 2;
          continue;
        }
        if (src[i] === q) {
          i++;
          break;
        }
        i++;
      }
      continue;
    }
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return i;
    }
    i++;
  }
  return -1;
}

function extractSeam(body) {
  const sentinel = /\/\/\s*agent-seam:start([\s\S]*?)\/\/\s*agent-seam:end/.exec(body);
  if (sentinel) return { how: 'sentinel', text: sentinel[1].trim() };
  // fallback: smallest brace-balanced block enclosing the first real `agent(` call.
  const code = stripCommentsAndStrings(body);
  const target = code.indexOf('agent(');
  if (target < 0) return { how: 'none', text: null };
  for (let i = target; i >= 0; i--) {
    if (code[i] === '{') {
      const end = matchBrace(body, i);
      if (end > target) return { how: 'enclosing-block', text: body.slice(i, end + 1).trim() };
    }
  }
  return { how: 'none', text: null };
}

// Normalize the two LEGITIMATE per-file dimensions of an otherwise-duplicated seam:
//  - the per-workflow NAME label embedded in the prompt / log prefix (e.g. "Plan-phase"
//    vs "implement-review" vs "close" — the seam should identify its own workflow);
//  - incidental string-literal line-wrapping (`"a " + "b"` vs `"a b"`).
// What REMAINS after normalization is the seam LOGIC — withRetry, the agent() call, the
// schema handling — which GT-1 requires to be identical (no drift) across the three
// self-contained copies. A real logic drift (a different retry count, different schema
// branching) survives normalization and fails the assertion.
function normalizeSeam(text, stem) {
  return text
    .toLowerCase()
    .replaceAll(stem, 'WF') // the per-file workflow-name label (lowercased so "Plan-phase" -> stem)
    .replace(/['"`]\s*\+\s*['"`]/g, '') // join adjacent string-literal concatenations (line-wrap)
    .replace(/\s+/g, ' ') // collapse whitespace
    .trim();
}

test('GT-1: the inlined agent()/retry seam LOGIC is identical across plan-phase / implement-review / close', () => {
  const seams = WORKFLOW_PATHS.map((p) => {
    const { body } = loadParts(readWorkflowText(p));
    const s = extractSeam(body);
    return { stem: stemOf(p), how: s.how, raw: s.text, norm: s.text == null ? null : normalizeSeam(s.text, stemOf(p)) };
  });
  for (const s of seams) {
    assert.notEqual(s.raw, null, `could not locate the agent()/retry seam in ${s.stem}.js — delimit it with // agent-seam:start / // agent-seam:end so GT-1 drift is detectable`);
  }
  const [a, b, c] = seams;
  assert.equal(a.norm, b.norm, `agent()/retry seam LOGIC drifted between ${a.stem}.js and ${b.stem}.js (GT-1 requires the inlined copies to stay logically identical; only the per-workflow name label may differ)`);
  assert.equal(b.norm, c.norm, `agent()/retry seam LOGIC drifted between ${b.stem}.js and ${c.stem}.js (GT-1 requires the inlined copies to stay logically identical; only the per-workflow name label may differ)`);
});

// Reversal-gate (EC5) — LANDED in C2+D Phase 2 PR 2 (finding-034 / DEC-0122). The Python-CLI
// gate `genome workflows check` (seam-drift + schema-validity, fail-closed) is the enforcement
// surface the green-keeper runs in the dev-loop; this suite is its NODE MIRROR. The GT-1 test
// above mirrors the seam-drift comparison; the test below asserts the precondition the Python
// gate's deterministic primary path depends on — every seam is sentinel-delimited.
test('reversal-gate (node mirror): the agent()/retry seam is sentinel-delimited in all three workflows', () => {
  for (const p of WORKFLOW_PATHS) {
    const { body } = loadParts(readWorkflowText(p));
    const s = extractSeam(body);
    assert.equal(
      s.how,
      'sentinel',
      `${stemOf(p)}.js: the agent()/retry seam must be delimited with // agent-seam:start / // agent-seam:end — the Python reversal-gate (genome workflows check) requires the sentinel convention and fails closed without it`,
    );
  }
});

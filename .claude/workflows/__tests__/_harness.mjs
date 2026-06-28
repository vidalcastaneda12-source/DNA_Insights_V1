// _harness.mjs — plan-blind test harness for the ported team workflows (C2D-Phase1).
//
// from: C2D-Phase1-synthesized-plan.md §"Implementation steps" step 2 + §"Tests";
//       C2D-Phase1-probe-results.md (EMPIRICAL engine semantics); plan-v2-delta D2/D9.
//       In-repo substantiation (the two C2D-Phase1-* plan files above are session-local, NOT
//       committed to the repo): docs/findings/c2d-load-probe-wf_a37802b2-c92.js (the committed
//       load-probe for the engine load model + semantics — null-on-async-reject /
//       propagate-on-sync-throw / variadic pipeline(items, ...stages)) +
//       docs/findings/finding-034-agent-team-plan-phase.md (the probe appendix).
//
// This module loads a workflow EXACTLY how the empirically-confirmed dynamic-workflows
// engine does (probe `wf_a37802b2-c92`): it statically extracts the pure-literal
// `export const meta` and wraps the REST of the body in an AsyncFunction with the eight
// hooks injected as parameters. It then RUNS that body against recording stubs that
// mirror the probe's semantics. The suites assert against the recorded `agent()` call
// graph + the returned package — never against the implementation's logic, which this
// author never read (plan-blind / independent-oracle contract, finding-034).
//
// NOTE ON BLINDNESS: nothing here READS a workflow body for an assertion. The text is
// consumed programmatically (extract meta -> wrap body -> construct/run), which is the
// engine's own load model. Expectations come from the SPEC (scope-run.md depth table +
// the .claude/agents/*.md Output contracts), not from the ported code.

import { readFileSync } from 'node:fs';
import { dirname, join, basename } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
export const WORKFLOWS_DIR = join(HERE, '..'); // .claude/workflows
export const AGENTS_DIR = join(HERE, '..', '..', 'agents'); // .claude/agents

export const PLAN_PHASE = join(WORKFLOWS_DIR, 'plan-phase.js');
export const IMPLEMENT_REVIEW = join(WORKFLOWS_DIR, 'implement-review.js');
export const CLOSE = join(WORKFLOWS_DIR, 'close.js');
export const WORKFLOW_PATHS = [PLAN_PHASE, IMPLEMENT_REVIEW, CLOSE];

export const stemOf = (p) => basename(p, '.js');

// ---------------------------------------------------------------------------
// Forbidden tokens (plan-v2-delta D2). The engine injects ONLY
// agent/parallel/pipeline/log/phase/budget/workflow/args and forbids the Node API;
// any surviving abstract-runtime / CJS residue ReferenceErrors at runtime.
// ---------------------------------------------------------------------------
export const FORBIDDEN_TOKENS = [
  'require(',
  'module.',
  'process.',
  '__dirname',
  '__filename',
  'console.',
  'globalThis.',
  'setResult',
  'emitProgress',
  'require.main',
];

// Non-determinism sources (plan-v2-delta D2 + synthesized-plan Suite 5).
export const NONDETERMINISM_TOKENS = ['Date.now', 'Math.random', 'new Date('];

// ---------------------------------------------------------------------------
// Members the ported workflows MUST call SCHEMA-LESS (plan-v2-delta D1 + this
// task's Suite 2): handoff-assembler returns PROSE (0 JSON keys); architect-reviewer
// and the in-loop silent-failure-hunter are verdict-less. The load-bearing invariant
// (which both this task's "schema-less" wording and D1's "findings-only schema"
// satisfy) is: their call must NOT require a key the member does not emit — caught by
// the general `schema.required ⊆ documentedOutputKeys` rule below.
// ---------------------------------------------------------------------------
export const PROSE_MEMBERS = ['handoff-assembler']; // strictly schema-less (no JSON output at all)
export const VERDICTLESS_MEMBERS = ['architect-reviewer', 'silent-failure-hunter'];

// ===========================================================================
// Source scanning: a single string/comment-aware state machine, reused to
// (a) blank comments+strings for token scans, and (b) brace-match the meta literal.
// ===========================================================================

// Blank out the content of comments AND string/template literals with spaces,
// preserving length + newlines so indices map 1:1 to the original text. The result
// is "code only" — what a forbidden-token scan should see (a token in a comment or a
// string is not CJS residue). Template `${...}` interiors are blanked too (a rare
// edge; clean ports do not hide a Node global inside an interpolation).
export function stripCommentsAndStrings(src) {
  let out = '';
  let i = 0;
  const n = src.length;
  const keep = (ch) => (ch === '\n' || ch === '\r' || ch === '\t' ? ch : ' ');
  while (i < n) {
    const c = src[i];
    const c2 = src[i + 1];
    if (c === '/' && c2 === '/') {
      while (i < n && src[i] !== '\n') {
        out += keep(src[i]);
        i++;
      }
      continue;
    }
    if (c === '/' && c2 === '*') {
      out += '  ';
      i += 2;
      while (i < n && !(src[i] === '*' && src[i + 1] === '/')) {
        out += keep(src[i]);
        i++;
      }
      if (i < n) {
        out += '  ';
        i += 2;
      }
      continue;
    }
    if (c === '"' || c === "'" || c === '`') {
      const quote = c;
      out += ' ';
      i++;
      while (i < n) {
        if (src[i] === '\\') {
          out += '  ';
          i += 2;
          continue;
        }
        if (src[i] === quote) {
          out += ' ';
          i++;
          break;
        }
        out += keep(src[i]);
        i++;
      }
      continue;
    }
    out += c;
    i++;
  }
  return out;
}

// Given an index of an opening brace in the ORIGINAL text, return the index of its
// matching close brace (string/comment aware).
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
  throw new Error('unbalanced braces from index ' + start);
}

// ===========================================================================
// Workflow load model (the engine's, faithfully): extract meta, wrap body.
// ===========================================================================

export function readWorkflowText(path) {
  return readFileSync(path, 'utf8');
}

// Locate the REAL `export const meta = { ... }` declaration. Robust to:
//  - a header COMMENT that contains the literal string `export const meta` (D2/D10
//    false-match bait): we search the comment/string-stripped code, so a commented
//    occurrence is invisible;
//  - a NESTED `phases:[{...},{...}]` literal: brace-matching handles arbitrary nesting.
export function findMetaDecl(text) {
  const code = stripCommentsAndStrings(text);
  const m = /\bexport\s+const\s+meta\b/.exec(code);
  if (!m) throw new Error('no `export const meta` found in code');
  let i = m.index + m[0].length;
  while (i < code.length && code[i] !== '=') i++;
  if (code[i] !== '=') throw new Error('no `=` after `export const meta`');
  i++;
  while (i < code.length && code[i] !== '{') i++;
  if (code[i] !== '{') throw new Error('meta is not an object literal');
  const braceStart = i;
  const braceEnd = matchBrace(text, braceStart);
  const metaSrc = text.slice(braceStart, braceEnd + 1);
  let j = braceEnd + 1;
  while (j < text.length && /\s/.test(text[j])) j++;
  const declEnd = text[j] === ';' ? j + 1 : braceEnd + 1;
  return { metaSrc, declStart: m.index, declEnd };
}

function evalLiteral(src) {
  // The spec guarantees a PURE literal (no calls, no Date.now). Function-eval round-trips
  // arbitrary nesting (phases:[{...},{...}]) exactly as the engine's static read does.
  // eslint-disable-next-line no-new-func
  return Function('"use strict"; return (' + src + ');')();
}

// Returns { meta, body }. `body` is the file with the single `export const meta = {…};`
// statement removed — i.e. exactly what the engine wraps in an async function. A clean
// port has NO other `export` (else the AsyncFunction build throws → Suite 1 RED).
export function loadParts(text) {
  const { metaSrc, declStart, declEnd } = findMetaDecl(text);
  const meta = evalLiteral(metaSrc);
  const body = text.slice(0, declStart) + text.slice(declEnd);
  return { meta, body };
}

const HOOK_PARAMS = ['agent', 'parallel', 'pipeline', 'log', 'phase', 'budget', 'workflow', 'args'];
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

// GT-2 construct-check: build the body as an AsyncFunction with the eight hooks injected.
// True iff it builds (the engine's load model) — NOT raw `node --check`, which rejects
// the top-level-`return` dialect.
export function constructable(path) {
  try {
    const { body } = loadParts(readWorkflowText(path));
    // eslint-disable-next-line no-new
    new AsyncFunction(...HOOK_PARAMS, body);
    return true;
  } catch {
    return false;
  }
}

// The LAST top-level statement must be `return …` (plan-v2-delta D2 — no leftover
// `planPhase().then(setResult)` IIFE that builds green but resolves undefined).
export function endsWithTopLevelReturn(body) {
  const code = stripCommentsAndStrings(body);
  // walk depth-0 looking for the last `return` keyword at statement level
  let depth = 0;
  let lastReturn = -1;
  const re = /\breturn\b/g;
  // track brace depth across the whole body; record `return` indices seen at depth 0
  let idx = 0;
  let mr;
  // Precompute depth at each index via a single pass + marker array would be heavy;
  // instead scan char-by-char and detect the keyword inline.
  for (idx = 0; idx < code.length; idx++) {
    const c = code[idx];
    if (c === '{' || c === '(' || c === '[') depth++;
    else if (c === '}' || c === ')' || c === ']') depth--;
    else if (depth === 0 && code.startsWith('return', idx) && /\W/.test(code[idx - 1] ?? ' ') && /\W/.test(code[idx + 6] ?? ' ')) {
      lastReturn = idx;
    }
  }
  if (lastReturn < 0) return false;
  // After the last depth-0 return: allow the return expression, then at most one `;`,
  // then only whitespace to EOF (no further top-level statement).
  const tail = code.slice(lastReturn + 6);
  // find the first depth-0 `;` in tail
  let d = 0;
  let semi = -1;
  for (let k = 0; k < tail.length; k++) {
    const c = tail[k];
    if (c === '{' || c === '(' || c === '[') d++;
    else if (c === '}' || c === ')' || c === ']') d--;
    else if (c === ';' && d === 0) {
      semi = k;
      break;
    }
  }
  const after = semi >= 0 ? tail.slice(semi + 1) : tail;
  return after.trim().length === 0;
}

export function forbiddenTokensIn(body, tokens = FORBIDDEN_TOKENS) {
  const code = stripCommentsAndStrings(body);
  return tokens.filter((t) => code.includes(t));
}

// ===========================================================================
// Recording stubs that mirror the probe-confirmed engine semantics.
// ===========================================================================

const CALL_CAP = 2000; // safety net: a stub config that loops forever surfaces loudly.

function deepStringify(x) {
  try {
    return typeof x === 'string' ? x : JSON.stringify(x);
  } catch {
    return String(x);
  }
}

// review-synthesizer survivor logic (mirrors finding-034 refute-by-default +
// plan-v2-delta D6 strict-majority): a blocker survives iff > half of its skeptics
// FAILED to refute it; a tie/minority kills it. Works whether verifyFresh forwards
// the survivor set or the raw per-skeptic verdicts (both designs reduce to the same
// outcome here). This lets the workflow's strict-majority become observable via the
// downstream go/fix-first routing.
function deriveSynthVerdict(prompt, opts) {
  const s = deepStringify(prompt) + ' ' + deepStringify(opts);
  const hasBlocker = /"severity"\s*:\s*"blocker"|"verified_severity"\s*:\s*"blocker"/.test(s);
  if (!hasBlocker) return 'go';
  const refutedVals = [...s.matchAll(/"refuted"\s*:\s*(true|false)/g)].map((m) => m[1] === 'true');
  let survives;
  if (refutedVals.length) {
    const notRefuted = refutedVals.filter((r) => !r).length;
    survives = notRefuted > refutedVals.length / 2; // strict-majority; tie => killed
  } else {
    // a blocker forwarded with no refutation info => treat as a standing survivor
    survives = true;
  }
  return survives ? 'fix-first' : 'go';
}

function lensReturn(lens, hooks) {
  const findings = (hooks.lensFindings && hooks.lensFindings[lens]) || [];
  const ret = { lens, findings };
  if (lens === 'regression-hunter') ret.anchors_to_watch = hooks.anchorsToWatch || [];
  if (lens === 'pr-test-analyzer') ret.coverage_summary = { behaviors_changed: 0, behaviors_tested: 0, untested: [] };
  return ret;
}

const SCOPE = 'C2D-Phase1';

// Canonical default returns keyed by agentType. Each drives the happy path; suites
// override control-flow fields via `hooks`. Shapes echo each member's .md Output.
const DEFAULT_RETURNS = {
  'scope-dispatcher': (ctx) => makeManifest(ctx.hooks),
  planner: (ctx) => ({
    scope_id: SCOPE,
    angle: ctx.opts?.angle ?? 'minimal-diff',
    reading_list_confirmed: [],
    problem_statement: 'stub',
    constraints: [],
    implementation_plan: [],
    tests: { new: [], must_still_pass: [] },
    verification: { commands: [], expected_outputs: [], anchors_to_recheck: [] },
    out_of_scope: [],
    handoff_note: 'stub',
    escalations: [],
    confidence: 0.8,
    riskiest_assumption: 'stub',
  }),
  'plan-judges': (ctx) => ({
    scope_id: SCOPE,
    axis: ctx.opts?.axis ?? 'combined',
    scores: [],
    axis_winner: 'minimal-diff',
    axis_winners: {},
  }),
  'plan-synthesizer': () => ({
    scope_id: SCOPE,
    synthesized_plan: { implementation_plan: [], tests: { new: [] }, verification: {} },
    graft_provenance: {},
    divergence: [],
    riskiest_assumptions: [],
    panel_confidence: 0.8,
  }),
  'plan-premortem': (ctx) => ({
    scope_id: SCOPE,
    lens: ctx.opts?.lens ?? 'general',
    predicted_surprises: ctx.hooks.predictedSurprises || [],
    anchors_at_risk: [],
    recommend: ctx.hooks.premortemRecommend || 'proceed',
  }),
  'plan-auditor': (ctx) => ({
    scope_id: SCOPE,
    lens: ctx.opts?.lens ?? 'contract',
    verdict: ctx.hooks.auditorVerdict || 'ready',
    section_completeness: {},
    reading_list_coverage: { covered: true, gaps: [] },
    locked_decision_check: [],
    findings: ctx.hooks.auditorFindings || [],
  }),
  'architect-reviewer': (ctx) => ({ lens: 'architect-reviewer', findings: ctx.hooks.architectFindings || [] }),
  implementer: () => ({
    scope_id: SCOPE,
    implemented_steps: [],
    files_touched: [],
    green_loop: { pytest: 'pass', ruff_check: 'pass', ruff_format: 'pass', mypy: 'pass' },
    blind_tests: { started: 'red', now: 'green', count: 0 },
    predicted_surprises_seen: [],
    escalations: [],
    ready_for_review: true,
  }),
  'test-author': () => ({
    scope_id: SCOPE,
    authored_from: { plan_sections: ['§5', '§6'], interface_contract: 'frozen' },
    blind_to: 'implementation diff',
    tests: [],
    fixtures_added: [],
    coverage_of_plan: { gaps: [] },
    independence_attestation: 'did not read the diff',
    expected_initial_state: 'red',
  }),
  'plan-adherence-sentinel': (ctx) => ({
    scope_id: SCOPE,
    verdict: ctx.hooks.sentinelVerdict || 'on-rails',
    drift: ctx.hooks.sentinelDrift || [],
    predicted_surprises_seen: [],
  }),
  'green-keeper': (ctx) => {
    // greenRed => fail on the FIRST green-keeper call, recover green afterwards so the
    // red->triage->debugger path fires once and the loop terminates.
    const gkCalls = ctx.calls.filter((c) => c.agentType === 'green-keeper').length;
    const red = !!ctx.hooks.greenRed && gkCalls <= (ctx.hooks.greenRedCount ?? 1);
    return {
      scope_id: SCOPE,
      loop: {
        pytest: red ? 'fail' : 'pass',
        ruff_check: 'pass',
        ruff_format: 'pass',
        mypy: 'pass',
      },
      first_error: red ? 'stub failure' : null,
      blocked_by: null,
      escalate: false,
      route: red ? 'test-triage' : 'none',
    };
  },
  'test-triage': (ctx) => ({
    scope_id: SCOPE,
    failures: [
      {
        test: 'backend/tests/test_stub.py::test_x',
        class: ctx.hooks.triageClass || 'real-regression',
        evidence: 'stub',
        route: ctx.hooks.triageRoute || 'deep-debugger',
        spec_backed: true,
      },
    ],
  }),
  'deep-debugger': () => ({
    scope_id: SCOPE,
    symptom: 'stub',
    root_cause: { mechanism: 'stub', evidence: 'stub', precedent: null },
    proposed_fix: { detail: 'stub', files: [], weakens_a_test: false, touches_schema: false },
    verify_by: 'stub',
    escalate: false,
    escalate_reason: null,
  }),
  'schema-change-executor': () => ({
    scope_id: SCOPE,
    schema_files_changed: [],
    ddl_reextracted: [],
    rebuild: { rm_data: true, genome_init: 'ok', reingest: 'ok' },
    anchor_check: [],
    fts5_shortcut_taken: false,
    escalate: false,
  }),
  'fan-out-implementer': () => ({
    scope_id: SCOPE,
    units: [],
    join: { merged: true, full_dev_loop: { pytest: 'pass', ruff_check: 'pass', ruff_format: 'pass', mypy: 'pass' } },
    coupling_violations: [],
    escalations: [],
    ready_for_review: true,
  }),
  'convention-compliance': (ctx) => lensReturn('convention-compliance', ctx.hooks),
  'phi-pii-guardian': (ctx) => lensReturn('phi-pii-guardian', ctx.hooks),
  'test-integrity': (ctx) => lensReturn('test-integrity', ctx.hooks),
  'regression-hunter': (ctx) => lensReturn('regression-hunter', ctx.hooks),
  'silent-failure-hunter': (ctx) => lensReturn('silent-failure-hunter', ctx.hooks),
  'type-design-analyzer': (ctx) => lensReturn('type-design-analyzer', ctx.hooks),
  'pr-test-analyzer': (ctx) => lensReturn('pr-test-analyzer', ctx.hooks),
  'comment-analyzer': (ctx) => lensReturn('comment-analyzer', ctx.hooks),
  'finding-verifier': (ctx) => {
    const fvIdx = ctx.calls.filter((c) => c.agentType === 'finding-verifier').length - 1;
    const votes = ctx.hooks.verifierVotes || [];
    const refuted = fvIdx < votes.length ? votes[fvIdx] : (ctx.hooks.verifierDefaultRefuted ?? true);
    const angle = ['reproduce', 'reachable', 'documented-exception'][fvIdx % 3];
    // Contract shape (.claude/agents/finding-verifier.md Output, lines 46-57): the
    // TOP-LEVEL field is `survives`; `refuted` lives ONLY inside each `votes[]` entry.
    // The workflow's strict-majority adjudication reads top-level `survives`, so the
    // previous top-level `refuted` dead-keyed it to `undefined` in every test (the
    // test-fidelity bug). `survives: !refuted` makes the production path observable;
    // `votes[].refuted` is preserved for any helper (e.g. deriveSynthVerdict) that reads it.
    return {
      id: ctx.opts?.findingId || 'find-1',
      survives: !refuted,
      votes: [{ angle, refuted, reason: 'stub' }],
      verified_severity: ctx.hooks.findingSeverity || 'blocker',
      confidence: 0.5,
    };
  },
  'review-synthesizer': (ctx) => {
    const verdict = ctx.hooks.synthVerdict || deriveSynthVerdict(ctx.prompt, ctx.opts);
    return {
      scope_id: SCOPE,
      verdict,
      blockers: verdict === 'fix-first' ? [{ id: 'find-1', where: 'stub', claim: 'stub', refutation_trail: [], lenses: [] }] : [],
      warns: [],
      nits_count: 0,
      nits_appendix: [],
      anchors_to_watch: ctx.hooks.anchorsToWatch || [],
      correctness_attestation: 'stub',
      residual_risk: 'stub',
    };
  },
  'completeness-critic': () => ({
    scope_id: SCOPE,
    round: 1,
    gaps: [],
    skipped_deliberately: [],
    converged: true, // converge immediately so loop-until-dry terminates
    next_round_work: [],
  }),
  'handoff-assembler': () => 'HANDOFF (prose): branch, commit SHAs, files changed, verification commands, PR URL, pytest baseline/result + Agent-team appendix.',
  'repo-sweep': () => ({ fruit: [], maybe: [], skipped_count: 0, scanned: [] }),
  'knowledge-curator': () => ({
    scope_id: SCOPE,
    relocks: [],
    roadmap_flip: 'PR [ ] -> [x]',
    cross_links: [],
    cross_check_passed: true,
    escalations: [],
  }),
};

// A realistic scope manifest (finding-013 fixture realism), tunable per suite.
export function makeManifest(hooks = {}) {
  return {
    scope_id: SCOPE,
    title: hooks.title || 'C2D-Phase1 — port team workflows to the engine dialect',
    roadmap_slot: 'C2D-Phase1',
    change_class: hooks.changeClass || ['cli', 'tests'],
    depends_on: [],
    gated_by: [],
    reading_list: { docs: ['CLAUDE.md'], findings: ['finding-034'], code: ['.claude/workflows/'] },
    locked_decisions_in_play: [],
    blast_radius: hooks.blastRadius || { imports_touched: ['a'], tests_covering: ['t'] },
    applicable_anchors: hooks.applicableAnchors || [],
    precedent: hooks.precedent || [],
    rebuild_required: false,
    risk_tier: hooks.tier ?? 2,
    risk_breakdown: { C: 1, B: 0, P: 0, A: 0, S: 1, floor: 0, deep_T2: !!hooks.deepT2 },
    review_lenses: hooks.reviewLenses || ['convention-compliance', 'test-integrity'],
    deep_T2: !!hooks.deepT2,
    out_of_scope_candidates: [],
    freshness_flags: [],
    open_questions: [],
  };
}

// The real C2D-Phase1 manifest shape: JS-only port, no Python schema, narrow blast,
// no numeric real-data anchors, green expected to pass => none of the 4 trigger-gated
// Stage-2 writers should fire (synthesized-plan riskiest-assumption #5).
export function c2dManifestHooks(extra = {}) {
  return {
    changeClass: ['tests', 'cli'],
    blastRadius: { imports_touched: ['.claude/workflows/plan-phase.js'], tests_covering: ['.claude/workflows/__tests__/'] },
    applicableAnchors: [],
    reviewLenses: ['convention-compliance'],
    tier: 1,
    deepT2: false,
    greenRed: false,
    ...extra,
  };
}

function makeBudget(hooks) {
  const total = hooks.budget && 'total' in hooks.budget ? hooks.budget.total : null; // null when unset (probe)
  let spent = hooks.budget?.startSpent ?? 0;
  const perCall = hooks.budget?.perCall ?? 1000;
  return {
    obj: {
      total,
      spent: () => spent,
    },
    spend() {
      spent += perCall;
    },
    get spent() {
      return spent;
    },
  };
}

// The eight injected hooks, recording into shared arrays. parallel/pipeline mirror the
// probe: NULL on async rejection, PROPAGATE on a synchronous throw. Exported so the arch-1
// drift-guard suite (harness-fanout-semantics.test.mjs) can exercise those seams directly.
export function makeRecorder(hooks) {
  const calls = [];
  const logs = [];
  const phases = [];
  const pipelineShapes = []; // engine-seam fidelity: recorded pipeline() arg shapes
  const parallelShapes = []; // engine-seam fidelity: recorded parallel() arg shapes
  const schemaViolations = []; // fix #5: schema-bearing calls whose stub return dropped a required key
  const budget = makeBudget(hooks);

  const agent = (prompt, opts = {}) => {
    if (calls.length > CALL_CAP) {
      throw new Error('CALL_CAP exceeded (' + CALL_CAP + ') — stub config likely loops forever');
    }
    const agentType = opts.agentType;
    const call = {
      agentType,
      prompt,
      schema: opts.schema,
      angle: opts.angle,
      axis: opts.axis,
      lens: opts.lens,
      label: opts.label,
      model: opts.model,
      effort: opts.effort,
      isolation: opts.isolation,
      opts,
    };
    calls.push(call);
    budget.spend();
    const ctx = { hooks, calls, prompt, opts };
    const resolver = DEFAULT_RETURNS[agentType];
    let ret;
    if (hooks.returns && agentType in hooks.returns) {
      const r = hooks.returns[agentType];
      ret = typeof r === 'function' ? r(ctx) : r;
    } else if (resolver) {
      ret = resolver(ctx);
    } else {
      ret = {}; // unknown agentType -> empty object (never crashes)
    }
    // Fix #5: validate the stub return against the schema THIS call carried. We RECORD any
    // drift onto `schemaViolations` rather than re-throwing into the workflow — whose inlined
    // agent()/retry seam SWALLOWS a synchronous throw and turns it into a perturbing retry, so
    // a bare throw would neither surface loudly NOR leave the call graph faithful. Recording
    // captures the drift verbatim; a dedicated test (harness-schema-guard) reddens on a
    // non-empty `schemaViolations`. The pure `assertStubSatisfiesSchema` primitive still throws
    // (unit-tested directly). A thenable is left for Promise.resolve to chain (no sync inspect).
    const isThenable = ret != null && typeof ret.then === 'function';
    if (!isThenable) {
      try {
        assertStubSatisfiesSchema(agentType, opts.schema, ret);
      } catch (e) {
        schemaViolations.push({
          agentType,
          required: schemaRequiredKeys(opts.schema),
          got: ret && typeof ret === 'object' ? Object.keys(ret) : typeof ret,
          message: e.message,
        });
      }
    }
    return Promise.resolve(ret);
  };

  // parallel(thunks): array in -> array out. async rejection -> null; sync throw -> propagate.
  const parallel = (thunks) => {
    parallelShapes.push({
      argIsArray: Array.isArray(thunks),
      thunkTypes: (Array.isArray(thunks) ? thunks : []).map((t) => typeof t),
    });
    const ps = thunks.map((t) => {
      const p = t(); // a SYNCHRONOUS throw here propagates out of parallel (probe finding 1)
      return Promise.resolve(p).then(
        (v) => v,
        () => null, // async rejection -> null slot
      );
    });
    return Promise.all(ps);
  };

  // pipeline(items, ...stages): VARIADIC stages (probe — NOT an array). Per item, thread
  // through stages. async rejection on a stage -> that item becomes null; a synchronous
  // throw propagates (probe finding 1). Record the call shape for the seam-dialect check.
  const pipeline = (items, ...stages) => {
    pipelineShapes.push({
      itemsIsArray: Array.isArray(items),
      stageTypes: stages.map((s) => (Array.isArray(s) ? 'array' : typeof s)),
    });
    const run = async () => {
      const out = [];
      for (const item of items) {
        let v = item;
        let nulled = false;
        for (const stage of stages) {
          const p = stage(v); // sync throw propagates
          try {
            v = await p; // async rejection -> null this item
          } catch {
            nulled = true;
            break;
          }
        }
        out.push(nulled ? null : v);
      }
      return out;
    };
    return run();
  };

  const log = (...a) => {
    logs.push(a.map((x) => (typeof x === 'string' ? x : deepStringify(x))).join(' '));
  };
  const phase = async (title, fn) => {
    phases.push(title);
    if (typeof fn === 'function') return await fn();
    return undefined;
  };
  const workflow = (...a) => {
    logs.push('workflow:' + a.map(deepStringify).join(','));
    return undefined;
  };

  return { agent, parallel, pipeline, log, phase, workflow, budget: budget.obj, calls, logs, phases, pipelineShapes, parallelShapes, schemaViolations };
}

// Run a workflow file against the recording stubs. Returns the recorded surface.
export async function loadWorkflow(path, hooks = {}) {
  const text = readWorkflowText(path);
  const { meta, body } = loadParts(text);
  const rec = makeRecorder(hooks);
  // eslint-disable-next-line no-new-func
  const fn = new AsyncFunction(...HOOK_PARAMS, body);

  let args;
  if (hooks.args !== undefined) {
    args = hooks.args; // suites that want a raw string (e.g. resume-determinism) pass it
  } else {
    // `args` arrives as a STRING (probe finding 2); encode an object the port can parse.
    const obj = {
      scope_id: SCOPE,
      manifest: makeManifest(hooks),
      plan: hooks.plan || { implementation_plan: [], tests: { new: [] }, verification: {} },
      confirmed_anchors: hooks.confirmedAnchors ?? hooks.confirmed_anchors ?? [],
    };
    args = JSON.stringify(hooks.argsObject || obj);
  }

  let result;
  let error = null;
  try {
    result = await fn(rec.agent, rec.parallel, rec.pipeline, rec.log, rec.phase, rec.budget, rec.workflow, args);
  } catch (e) {
    error = e;
  }
  return { meta, body, result, error, calls: rec.calls, logs: rec.logs, phases: rec.phases, budgetObj: rec.budget, pipelineShapes: rec.pipelineShapes, parallelShapes: rec.parallelShapes, schemaViolations: rec.schemaViolations };
}

// ===========================================================================
// Documented-output-keys parser (the SCHEMAS module, arch-6).
// Parses each .claude/agents/<name>.md "## Output" jsonc block and extracts the
// TOP-LEVEL keys via a brace-depth scan (robust to // comments, union-type
// annotations like `"x": "a" | "b"`, trailing commas, and nested objects/arrays).
// A member whose Output is PROSE (handoff-assembler) yields an EMPTY set.
// ===========================================================================
const _keysCache = new Map();

export function agentMdPath(agentType) {
  return join(AGENTS_DIR, agentType + '.md');
}

function extractOutputJsoncBlock(md) {
  // Find the "## Output" heading, then the first fenced ``` block after it.
  const h = /^##\s+Output\b.*$/m.exec(md);
  if (!h) return null;
  const after = md.slice(h.index + h[0].length);
  const fence = /```[a-zA-Z]*\n([\s\S]*?)```/.exec(after);
  return fence ? fence[1] : null;
}

function topLevelKeysOf(jsonc) {
  // walk the first {...} object, collecting `"key":` tokens at object-depth 1.
  const keys = new Set();
  let i = jsonc.indexOf('{');
  if (i < 0) return keys;
  let depth = 0;
  const n = jsonc.length;
  while (i < n) {
    const c = jsonc[i];
    const c2 = jsonc[i + 1];
    if (c === '/' && c2 === '/') {
      while (i < n && jsonc[i] !== '\n') i++;
      continue;
    }
    if (c === '/' && c2 === '*') {
      i += 2;
      while (i < n && !(jsonc[i] === '*' && jsonc[i + 1] === '/')) i++;
      i += 2;
      continue;
    }
    if (c === '"' || c === "'") {
      // read the string literal
      const q = c;
      let j = i + 1;
      let str = '';
      while (j < n) {
        if (jsonc[j] === '\\') {
          str += jsonc[j + 1];
          j += 2;
          continue;
        }
        if (jsonc[j] === q) break;
        str += jsonc[j];
        j++;
      }
      // is it a KEY? (depth 1, followed by optional ws/comment then ':')
      if (depth === 1) {
        let k = j + 1;
        // skip ws + comments
        while (k < n) {
          if (/\s/.test(jsonc[k])) {
            k++;
            continue;
          }
          if (jsonc[k] === '/' && jsonc[k + 1] === '/') {
            while (k < n && jsonc[k] !== '\n') k++;
            continue;
          }
          break;
        }
        if (jsonc[k] === ':') keys.add(str);
      }
      i = j + 1;
      continue;
    }
    if (c === '{' || c === '[') depth++;
    else if (c === '}' || c === ']') depth--;
    i++;
  }
  return keys;
}

export function documentedOutputKeys(agentType) {
  if (_keysCache.has(agentType)) return _keysCache.get(agentType);
  const md = readFileSync(agentMdPath(agentType), 'utf8');
  const block = extractOutputJsoncBlock(md);
  const keys = block ? topLevelKeysOf(block) : new Set(); // prose => empty
  _keysCache.set(agentType, keys);
  return keys;
}

// Extract the "required keys" a schema asserts. The dialect's schema carries `required`
// (synthesized-plan step 4: "required = the exact keys requireKeys asserted"); fall back
// to the property/object key set so a differently-shaped schema is still checkable.
export function schemaRequiredKeys(schema) {
  if (!schema || typeof schema !== 'object') return null; // schema-less
  if (Array.isArray(schema.required)) return schema.required.slice();
  if (schema.properties && typeof schema.properties === 'object') return Object.keys(schema.properties);
  return Object.keys(schema);
}

// ROOT-CAUSE GUARD (this task's fix #5). The recorder previously returned stub values
// WITHOUT checking them against the schema the workflow's `agent()` call carried — which is
// exactly why the finding-verifier stub's top-level-`refuted`-instead-of-`survives` drift
// went silently uncaught and dead-keyed the production strict-majority path. This makes a
// stubbed return that does NOT carry the schema's required keys fail LOUD at the call site,
// so future stub-contract drift reddens a test instead of silently dead-keying production.
//
// Deliberately SKIPPED for a null/undefined return: that is an INJECTED infra crash (the
// fail-closed tests), not a shape-drift — the workflow's own fail-closed guard is what those
// tests exercise, and a crash must reduce to a null vote, not a harness throw.
export function assertStubSatisfiesSchema(agentType, schema, ret) {
  const required = schemaRequiredKeys(schema);
  if (required === null) return; // schema-less call — nothing to enforce
  if (ret === null || ret === undefined) return; // injected crash, not stub drift — skip
  if (typeof ret !== 'object') {
    throw new Error(
      `stub-contract drift: '${agentType}' was called WITH a schema (required ${JSON.stringify(required)}) ` +
        `but the stub returned a ${typeof ret}, not an object`,
    );
  }
  const missing = required.filter((k) => !(k in ret));
  if (missing.length) {
    throw new Error(
      `stub-contract drift: '${agentType}' stub return is missing schema-required key(s) ${JSON.stringify(missing)} ` +
        `(stub emitted ${JSON.stringify(Object.keys(ret))}). The stub must echo the member's documented Output shape ` +
        `so the workflow's strict read of those keys is exercised, not silently dead-keyed.`,
    );
  }
}

// ===========================================================================
// Small assertion helpers shared by the suites.
// ===========================================================================
export const callsOf = (calls, agentType) => calls.filter((c) => c.agentType === agentType);
export const countOf = (calls, agentType) => callsOf(calls, agentType).length;
export const agentTypesIn = (calls) => calls.map((c) => c.agentType);

// The verified-survivor set lives at the documented pre-gate package field
// `result.stage3.survivors` (scope-run.md §"Stage 3" step 4 — review-synthesizer's
// "keep survivors only"; review-synthesizer.md). These two readers are shape-agnostic:
// the field may be a SET (array of surviving findings) or a COUNT (number of survivors);
// both reduce to the same observable for the single-finding Suite-8 scenarios. A missing/
// other-shaped field yields `undefined` / `false`, so a direct assertion reddens loudly
// (rather than silently passing) if the workflow stops emitting it.
export function survivorCount(result) {
  const s = result && result.stage3 && result.stage3.survivors;
  if (Array.isArray(s)) return s.length;
  if (typeof s === 'number') return s;
  return undefined;
}
export function survivorsInclude(result, id) {
  const s = result && result.stage3 && result.stage3.survivors;
  if (Array.isArray(s)) return deepHasValue(s, id);
  if (typeof s === 'number') return s >= 1; // count shape: with one finding under review, present <=> count>=1
  return false;
}

export function callAngle(call) {
  return call.angle ?? call.opts?.angle ?? call.label ?? call.opts?.label ?? promptAngle(call.prompt);
}
function promptAngle(prompt) {
  const s = deepStringify(prompt);
  for (const a of ['minimal-diff', 'gate-backward', 'risk-first', 'convention-purist']) {
    if (s.includes(a)) return a;
  }
  return undefined;
}

export function deepHasValue(obj, val, seen = new Set()) {
  if (obj === val) return true;
  if (obj && typeof obj === 'object') {
    if (seen.has(obj)) return false;
    seen.add(obj);
    for (const k of Object.keys(obj)) {
      if (deepHasValue(obj[k], val, seen)) return true;
    }
  }
  return false;
}

export function deepFindString(obj, substr, seen = new Set()) {
  if (typeof obj === 'string') return obj.includes(substr);
  if (obj && typeof obj === 'object') {
    if (seen.has(obj)) return false;
    seen.add(obj);
    for (const k of Object.keys(obj)) {
      if (deepFindString(obj[k], substr, seen)) return true;
    }
  }
  return false;
}

// The expected per-tier plan-phase call graph (scope-run.md §"Stage 1" + depth table,
// lines 89-96 / 187-194). Used by Suite 3.
export const PLAN_TIER_EXPECT = {
  0: { planners: 1, plannerAngles: ['minimal-diff'], judges: 0, synthesizer: 0, premortem: 1, architect: 0 },
  1: { planners: 2, plannerAngles: ['minimal-diff', 'gate-backward'], judgesExact: 1, synthesizer: 1, premortem: 1, architect: 0 },
  2: { planners: 4, plannerAngles: ['minimal-diff', 'gate-backward', 'risk-first', 'convention-purist'], judgesMin: 2, synthesizer: 1, premortemStandard: 2, premortemDeep: 3, architect: 1 },
};

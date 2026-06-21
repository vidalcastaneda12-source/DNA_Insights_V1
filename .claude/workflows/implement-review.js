/**
 * implement-review.js — per-scope agent team, Stage 2 (Implement) + Stage 3 (Review).
 *
 * The middle segment of the PR lifecycle designed in
 * docs/findings/finding-034-agent-team-plan-phase.md. It runs AFTER Human Gate 1
 * (VSC-User approved the plan) and ENDS at Human Gate 2 (VSC-User runs
 * verification.md and merges). The two human gates are why the lifecycle is split
 * into segmented workflow scripts rather than one auto-run: a single deterministic
 * script cannot cross a human decision. This script owns everything between them.
 *
 *   plan-phase.js   → Gate 1 (approve plan)
 *   implement-review.js (THIS)  → Gate 2 (verification.md · merge)
 *   close.js        → post-merge re-lock
 *
 * ── Stage 2 · Implement ───────────────────────────────────────────────────
 * interface-freeze → plan-blind test-author ∥ implementer → green loop watched by
 * the plan-adherence sentinel + an in-loop silent-failure check; tier-gated
 * side-channels (schema-change-executor when change_class ⊇ schema, fan-out
 * implementer when blast_radius is wide & independent). Converges to ONE coherent
 * change — the write phase does not fan out the act of writing.
 *
 * ── Stage 3 · Review ──────────────────────────────────────────────────────
 * Tier-0 = code-review + convention only; Tier-1+ runs the FULL lens set (finding-034
 * recalibrated). Lenses verify-as-they-complete (pipeline; barrier only for wide
 * blast_radius) → refute-by-default finding-verifier → completeness-critic gates a
 * loop-until-dry at Tier 1+ → review-synthesizer → the pre-gate package. Bounded
 * fix-first loop ×2 → escalate.
 *
 * ── Runtime caveat (same as plan-phase.js) ────────────────────────────────
 * The dynamic-workflows JS authoring API (the subagent-invocation primitive) is not
 * public. Every subagent call funnels through the single runAgent() helper, which
 * probes the known primitives and throws loudly if none resolve — a one-line fix in
 * one place. /code-review and /security-review are SKILLS, not subagents; they are
 * composed as review lenses (see skillLenses(): /code-review always, /security-review
 * when the diff warrants it) and surfaced in the package for the operator/runtime to
 * dispatch. The orchestration logic is node --check-clean; it is not end-to-end
 * executed here because the primitive is environment-provided.
 *
 * Args: `/implement-review PR-6` plus the approved plan + manifest + predicted
 * surprises delivered via the runtime global `args` (or args.scope_id / process.argv).
 */

'use strict';

const MAX_FIX_FIRST_CYCLES = 2; // bounded Stage-3 → Stage-2 loop; then escalate.
const MAX_REVIEW_ROUNDS = 3; // loop-until-dry cap (backstop against a stuck critic).
const DRY_STREAK_TO_CONVERGE = 2; // K consecutive rounds with nothing new ⇒ converged.

// Skill-backed review lenses (composed, not subagents). The runtime/operator
// dispatches these; the synthesizer is told to incorporate their findings.
// /code-review runs always (≥ Tier 0); /security-review only "when the diff warrants
// it" (finding-034 lens table) — i.e. a data / external / config surface.
function skillLenses(manifest) {
  const skills = ['/code-review'];
  if (touchesDataSurface(manifest) || touchesExternalOrConfig(manifest)) skills.push('/security-review');
  return skills;
}

// ── Tier → Stage-2 member set (finding-034 "Adaptive depth — recalibrated"). ─
function stage2Members(tier, manifest) {
  const m = ['implementer', 'green-keeper'];
  if (tier >= 1) m.push('test-author', 'plan-adherence-sentinel', 'silent-failure-hunter');
  if (tier >= 2) m.push('test-triage', 'deep-debugger');
  if ((manifest.change_class || []).includes('schema')) m.push('schema-change-executor');
  if (wideAndIndependent(manifest)) m.push('fan-out-implementer');
  return m;
}

// ── Stage-3 agent-backed lens set. Two principles from finding-034:
// (1) lens-gating is BY FACTOR, not tier — phi-pii on any data/external/config surface,
//     regression-hunter whenever anchors ≥ 1, regardless of tier; the manifest wins when
//     it lists review_lenses (the dispatcher already factor-gated them).
// (2) "Adaptive depth — recalibrated for correctness": Tier 0 = code-review + convention
//     only; Tier 1+ runs the FULL code-quality lens set (not the partial original set).
function stage3Lenses(tier, manifest) {
  if (Array.isArray(manifest.review_lenses) && manifest.review_lenses.length) {
    return manifest.review_lenses.filter((l) => l in LENS_TO_AGENT).map((l) => LENS_TO_AGENT[l]);
  }
  const set = new Set(['convention-compliance']); // always (≥ Tier 0)
  if (tier >= 1) {
    // Recalibrated Tier 1 = the full code-quality lens set.
    for (const l of ['test-integrity', 'silent-failure-hunter', 'type-design-analyzer', 'pr-test-analyzer', 'comment-analyzer', 'architect-reviewer']) {
      set.add(l);
    }
  }
  // Factor-gated lenses fire by factor at ANY tier, not by tier threshold.
  if (touchesDataSurface(manifest) || touchesExternalOrConfig(manifest)) set.add('phi-pii-guardian');
  if (anchorsExposed(manifest)) set.add('regression-hunter');
  return [...set];
}

// manifest.review_lenses uses friendly names; map the ones with an agent file.
const LENS_TO_AGENT = {
  'convention-compliance': 'convention-compliance',
  'phi-pii-guardian': 'phi-pii-guardian',
  'test-integrity': 'test-integrity',
  'regression-hunter': 'regression-hunter',
  'silent-failure-hunter': 'silent-failure-hunter',
  'type-design-analyzer': 'type-design-analyzer',
  'pr-test-analyzer': 'pr-test-analyzer',
  'comment-analyzer': 'comment-analyzer',
  'architect-reviewer': 'architect-reviewer',
};

function wideAndIndependent(manifest) {
  const n = ((manifest.blast_radius || {}).imports_touched || []).length;
  return n > 15; // "large" per the dispatcher's B-bucket; independence is the agent's gate.
}
function touchesDataSurface(manifest) {
  const cc = manifest.change_class || [];
  return ['pipeline', 'schema', 'annotation-loader', 'data-backfill'].some((c) => cc.includes(c));
}
function touchesExternalOrConfig(manifest) {
  const cc = manifest.change_class || [];
  return ['external', 'config', 'privacy', 'cli'].some((c) => cc.includes(c));
}
function anchorsExposed(manifest) {
  // regression-hunter fires whenever anchors ≥ 1 (finding-034: gated by factor, not tier).
  if (Array.isArray(manifest.applicable_anchors) && manifest.applicable_anchors.length) return true;
  const cc = manifest.change_class || [];
  return ['pipeline', 'schema', 'annotation-loader'].some((c) => cc.includes(c));
}

/**
 * THE one runtime-coupled call (see header). Invoke a `.claude/agents/<name>.md`
 * subagent with JSON input and return its parsed JSON output.
 */
async function runAgent(name, input, role) {
  const prompt =
    `You are being invoked as the \`${name}\` subagent in the per-scope agent team's ` +
    `implement-review workflow. Follow your agent definition exactly and return ONLY the ` +
    `JSON described in your "Output" section — no prose before or after.\n\n` +
    `INPUT (JSON):\n${JSON.stringify(input, null, 2)}`;

  progress(`→ ${role || name} (${name})`);

  let raw;
  if (typeof globalThis.runAgent === 'function' && globalThis.runAgent !== runAgent) {
    raw = await globalThis.runAgent({ agent: name, prompt });
  } else if (typeof globalThis.invokeSubagent === 'function') {
    raw = await globalThis.invokeSubagent({ agent: name, prompt });
  } else if (typeof globalThis.task === 'function') {
    raw = await globalThis.task({ subagent_type: name, prompt });
  } else if (typeof globalThis.agent === 'function') {
    raw = await globalThis.agent(name, prompt);
  } else {
    throw new Error(
      `implement-review.js: no subagent-invocation primitive found in the workflow ` +
        `runtime global scope (tried runAgent/invokeSubagent/task/agent). The ` +
        `dynamic-workflows JS authoring API is not publicly documented — inspect a ` +
        `generated workflow under ~/.claude/projects/<session>/ to find the real ` +
        `primitive, then wire it into runAgent() in this file (the only place to change).`,
    );
  }
  return coerceJson(raw, name);
}

function coerceJson(raw, name) {
  if (raw && typeof raw === 'object') return raw;
  const text = String(raw == null ? '' : raw);
  const fenced = text.match(/```(?:json[c]?)?\s*([\s\S]*?)```/i);
  const body = (fenced ? fenced[1] : text).trim();
  try {
    return JSON.parse(body);
  } catch (err) {
    throw new Error(
      `implement-review.js: ${name} did not return parseable JSON (members must ` +
        `"return only this JSON"). First 200 chars: ${body.slice(0, 200)}`,
    );
  }
}

function requireKeys(obj, keys, who) {
  const missing = keys.filter((k) => !(k in (obj || {})));
  if (missing.length) {
    throw new Error(`implement-review.js: ${who} output missing required keys: ${missing.join(', ')}`);
  }
  return obj;
}

function progress(msg) {
  if (typeof globalThis.emitProgress === 'function') globalThis.emitProgress(msg);
  else console.log(`[implement-review] ${msg}`);
}

/** Stage 2 — converge to one green change under the guards. */
async function stageImplement(ctx, fixFindings) {
  const { manifest, plan, predicted_surprises } = ctx;
  const tier = Number(manifest.risk_tier);
  const members = stage2Members(tier, manifest);
  progress(`Stage 2 · tier=${tier} · members=[${members.join(', ')}]`);

  // Interface-freeze unblocks the plan-blind test-author; the implementer declares
  // (or the plan pins) the public surface before bodies are written.
  const freeze = await runAgent(
    'implementer',
    { manifest, plan, mode: 'interface-freeze', predicted_surprises },
    'interface-freeze',
  );

  // Plan-blind test-author ∥ implementer. The author is denied the implementation
  // diff by contract; here it receives ONLY the plan + the frozen interface.
  const writers = [];
  if (members.includes('test-author')) {
    writers.push(
      runAgent(
        'test-author',
        { plan, interface_contract: freeze.interface_contract || plan, predicted_surprises },
        'test-author (plan-blind)',
      ),
    );
  }
  writers.push(
    runAgent('implementer', { manifest, plan, predicted_surprises, fix_findings: fixFindings }, 'implementer'),
  );
  const writerOut = await Promise.all(writers);
  const impl = writerOut[writerOut.length - 1];

  // Guards run on the produced diff: sentinel (drift) + in-loop silent-failure.
  const guards = [];
  if (members.includes('plan-adherence-sentinel')) {
    guards.push(runAgent('plan-adherence-sentinel', { manifest, plan, predicted_surprises }, 'sentinel'));
  }
  if (members.includes('silent-failure-hunter')) {
    guards.push(runAgent('silent-failure-hunter', { manifest, mode: 'in-loop' }, 'silent-failure (in-loop)'));
  }
  const guardOut = await Promise.all(guards);
  const sentinel = guardOut.find((g) => g && 'verdict' in g);

  // green-keeper holds the dev-loop; on red the implementer/triage/debugger resolve
  // (the agents own that sub-loop) — the orchestrator records the final verdict.
  const green = await runAgent('green-keeper', { manifest }, 'green-keeper');

  const blocked =
    (sentinel && sentinel.verdict === 'escalate') ||
    Boolean(green.escalate) ||
    impl.ready_for_review === false;

  return { freeze, impl, sentinel, green, blocked, members };
}

/**
 * One review round. Verify-as-you-go (PIPELINE) is the default — each lens's findings
 * flow to the verifier the moment that lens completes (finding-034 "Pipeline, not
 * barrier"). The BARRIER (Promise.all all lenses → dedup across lenses → verify once) is
 * the documented exception for scope with heavy cross-lens overlap, signalled by a wide
 * blast_radius. `seenIds` dedups findings across rounds so a stable finder is verified
 * once. nits are logged unverified; blocker/warn get a refute-by-default verifier.
 */
async function reviewRound(lenses, ctx, diffSummary, seenIds, useBarrier) {
  const { manifest, predicted_surprises } = ctx;
  const lensInput = { manifest, predicted_surprises, diff: diffSummary };

  const verifyFresh = async (fromLens, findings) => {
    const fresh = (findings || []).filter((f) => f.severity !== 'nit' && f.id && !seenIds.has(f.id));
    fresh.forEach((f) => seenIds.add(f.id));
    return Promise.all(
      fresh.map((f) =>
        runAgent('finding-verifier', { finding: f, from_lens: fromLens }, `verify:${f.id}`).then((v) => ({
          ...v,
          lens: fromLens,
          finding: f,
        })),
      ),
    );
  };

  if (useBarrier) {
    // Barrier: collect every lens, dedup by id across lenses, then verify once.
    const lensOut = await Promise.all(
      lenses.map((l) =>
        runAgent(l, lensInput, `lens:${l}`).then((o) => ({ lens: l, findings: o.findings || [], anchors_to_watch: o.anchors_to_watch || [] })),
      ),
    );
    const byId = new Map();
    for (const o of lensOut) {
      for (const f of o.findings) {
        if (!f.id) continue;
        if (!byId.has(f.id)) byId.set(f.id, { ...f, lenses: [o.lens] });
        else byId.get(f.id).lenses.push(o.lens);
      }
    }
    const verdicts = await verifyFresh('(deduped)', [...byId.values()]);
    return { lensOut, verdicts };
  }

  // Pipeline: each lens verifies its own findings as it lands.
  const lensOut = [];
  const verdictChunks = await Promise.all(
    lenses.map((l) =>
      runAgent(l, lensInput, `lens:${l}`).then((o) => {
        lensOut.push({ lens: l, findings: o.findings || [], anchors_to_watch: o.anchors_to_watch || [] });
        return verifyFresh(l, o.findings);
      }),
    ),
  );
  return { lensOut, verdicts: verdictChunks.flat() };
}

/** Stage 3 — fan out lenses, adversarially verify, synthesize the pre-gate package. */
async function stageReview(ctx, diffSummary) {
  const { manifest, predicted_surprises } = ctx;
  const tier = Number(manifest.risk_tier);
  const lenses = stage3Lenses(tier, manifest);
  const skills = skillLenses(manifest);
  const useBarrier = wideAndIndependent(manifest); // heavy cross-lens overlap → dedup before verify
  progress(
    `Stage 3 · tier=${tier} · lenses=[${lenses.join(', ')}] · skills=[${skills.join(', ')}] · ${useBarrier ? 'barrier' : 'pipeline'}`,
  );

  // Loop-until-dry at Tier 1+ (finding-034 recalibrated): the completeness-critic gates
  // the loop — keep running rounds until it reports converged, with a dry-streak + round
  // cap as backstops. Tier 0 is a single pass. `seenIds` keeps re-runs from re-verifying
  // stable findings, so a clean diff converges on round 1. (Each round re-runs the gated
  // lens set; the critic's targeted "spawn" is approximated by idempotent re-finders.)
  const seenIds = new Set();
  const allLensOut = [];
  const allVerdicts = [];
  let dryStreak = 0;

  for (let round = 1; round <= MAX_REVIEW_ROUNDS; round++) {
    const before = allVerdicts.length;
    const { lensOut, verdicts } = await reviewRound(lenses, ctx, diffSummary, seenIds, useBarrier);
    allLensOut.push(...lensOut);
    allVerdicts.push(...verdicts);

    if (tier < 1) break; // Tier 0: single pass, no loop-until-dry.

    const critic = await runAgent(
      'completeness-critic',
      { manifest, ran_lenses: lenses, verdicts: allVerdicts, predicted_surprises, round },
      `completeness-critic r${round}`,
    );
    if (critic && critic.converged === true) {
      progress(`completeness-critic converged at round ${round}`);
      break;
    }
    dryStreak = allVerdicts.length - before === 0 ? dryStreak + 1 : 0;
    if (dryStreak >= DRY_STREAK_TO_CONVERGE) {
      progress(`loop-until-dry: ${DRY_STREAK_TO_CONVERGE} consecutive dry rounds — converged`);
      break;
    }
    if (round === MAX_REVIEW_ROUNDS) progress(`loop-until-dry hit round cap ${MAX_REVIEW_ROUNDS}`);
  }

  const survivors = allVerdicts.filter((v) => v.survives);

  // Synthesize the pre-gate package for VSC-User.
  const pkg = requireKeys(
    await runAgent(
      'review-synthesizer',
      { manifest, lens_findings: allLensOut, verdicts: allVerdicts, predicted_surprises, composed_skills: skills },
      'review-synthesizer',
    ),
    ['verdict'],
    'review-synthesizer',
  );
  return { lenses, survivors_count: survivors.length, package: pkg };
}

/** Run Stage 2 → Stage 3 with the bounded fix-first loop. */
async function implementReview(ctx) {
  if (!ctx.scope_id) {
    throw new Error('implement-review.js: no scope id. Invoke as `/implement-review <SCOPE_ID>` with the approved plan in args.');
  }
  if (!ctx.plan || !ctx.manifest) {
    throw new Error('implement-review.js: missing approved plan or manifest in args (this segment runs AFTER Gate 1).');
  }

  let fixFindings = null;
  let stage2;
  let stage3;

  for (let cycle = 1; cycle <= MAX_FIX_FIRST_CYCLES + 1; cycle++) {
    progress(`cycle ${cycle}/${MAX_FIX_FIRST_CYCLES + 1}`);

    stage2 = await stageImplement(ctx, fixFindings);
    if (stage2.blocked) {
      return done(ctx, stage2, null, 'escalate', 'Stage 2 sentinel/green-keeper escalation or implementer surprise');
    }

    stage3 = await stageReview(ctx, summarizeDiff(stage2));
    if (stage3.package.verdict === 'go') {
      return done(ctx, stage2, stage3, 'go', null);
    }

    // fix-first → fold the blockers back into Stage 2, up to the cap.
    fixFindings = stage3.package.blockers || [];
    if (cycle === MAX_FIX_FIRST_CYCLES + 1) {
      return done(ctx, stage2, stage3, 'escalate', `fix-first loop hit the ${MAX_FIX_FIRST_CYCLES}-cycle cap`);
    }
  }
  return done(ctx, stage2, stage3, 'escalate', 'unreachable');
}

function summarizeDiff(stage2) {
  return { files_touched: (stage2.impl && stage2.impl.files_touched) || [], green: stage2.green && stage2.green.loop };
}

function done(ctx, stage2, stage3, route, escalationReason) {
  const pkg = stage3 && stage3.package;
  progress(`route=${route}${escalationReason ? ` (${escalationReason})` : ''}`);
  return {
    scope_id: ctx.scope_id,
    risk_tier: Number(ctx.manifest.risk_tier),
    stage2: stage2 && { members: stage2.members, ready_for_review: !stage2.blocked },
    stage3: stage3 && { lenses: stage3.lenses, survivors: stage3.survivors_count },
    pre_gate_package: pkg || null, // verdict · blockers · warns · anchors_to_watch · residual_risk
    route_to: route === 'go' ? 'STAGE 4 handoff → Human Gate 2 (verification.md · merge)' : 'VSC-User (escalation)',
    escalation_reason: escalationReason,
    auto_merged: false, // never — Gate 2 is human.
  };
}

// ── Entry point. Runtime delivers scope_id + approved plan + manifest via `args`. ─
const _args = typeof args !== 'undefined' ? args : globalThis.args;
const _ctx =
  _args && typeof _args === 'object' && !Array.isArray(_args)
    ? {
        scope_id: _args.scope_id || (Array.isArray(_args._) && _args._[0]) || '',
        plan: _args.plan || _args.approved_plan || null,
        manifest: _args.manifest || null,
        predicted_surprises: _args.predicted_surprises || [],
      }
    : {
        scope_id:
          (typeof _args === 'string' && _args.trim()) ||
          (typeof process !== 'undefined' && process.argv && process.argv[2]) ||
          '',
        plan: null,
        manifest: null,
        predicted_surprises: [],
      };

const _hasModule = typeof module !== 'undefined' && module && module.exports;
if (_hasModule) {
  module.exports = { implementReview, runAgent, stage2Members, stage3Lenses };
}

const _requiredByTest = _hasModule && typeof require !== 'undefined' && require.main !== module;
if (!_requiredByTest) {
  implementReview(_ctx)
    .then((pkg) => {
      progress('Stage 2–3 complete — pre-gate package ready for VSC-User.');
      if (typeof globalThis.setResult === 'function') globalThis.setResult(pkg);
      else console.log(JSON.stringify(pkg, null, 2));
    })
    .catch((err) => {
      progress(`Stage 2–3 FAILED: ${err.message}`);
      throw err;
    });
}

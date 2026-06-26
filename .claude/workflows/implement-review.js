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
 * side-channels (schema-change-executor when change_class ⊇ schema; the in-loop
 * test-triage → deep-debugger sub-loop on a red dev-loop; fan-out-implementer, which
 * REPLACES the implementer, when blast_radius is wide & independent). Converges to ONE
 * coherent change — the write phase does not fan out the act of writing.
 *
 * ── Stage 3 · Review ──────────────────────────────────────────────────────
 * Tier-0 = code-review + convention only; Tier-1+ runs the FULL lens set (finding-034
 * recalibrated). Lenses verify-as-they-complete (pipeline; barrier only for wide
 * blast_radius) → severity-scaled, refute-by-default finding-verifier → completeness-critic
 * gates a loop-until-dry at Tier 1+ → review-synthesizer → the pre-gate package. Bounded
 * fix-first loop ×2 → escalate. On 'go', the Stage-4 handoff-assembler is run before the
 * Gate-2 return.
 *
 * ── Runtime model (dynamic-workflows engine) ──────────────────────────────
 * The engine loads this file by reading the pure-literal `export const meta` statically
 * and wrapping the rest of the body in an async function with the workflow hooks injected
 * as parameters: agent · parallel · pipeline · log · phase · budget · workflow · args. The
 * script is SELF-CONTAINED (no import / no Node API) and ends with a top-level `return pkg`.
 * Subagents funnel through the inline `call()` seam over the injected `agent(prompt,
 * {agentType, schema})` primitive (empirically confirmed — C2D-Phase1 probe, finding-034
 * Amendment): a schema-bearing call returns a validated object (replacing the old output
 * coercion + key-assertion); a schema-LESS call returns the member's prose (handoff-assembler).
 * Wide fan-out uses `parallel`; verify-as-you-go uses `pipeline`; both null an async-rejected
 * thunk in its slot (we `.filter(Boolean)`), and every thunk is async so no synchronous throw
 * can crash the run. /code-review and /security-review are SKILLS, not subagents — composed as
 * review lenses (skillLenses()) and surfaced in the package for the operator/runtime to dispatch.
 *
 * Args: `/implement-review PR-6` plus the approved plan + manifest + predicted
 * surprises delivered via the runtime `args` (string or object — parsed defensively below).
 */

export const meta = {
  name: 'implement-review',
  description: 'Per-scope agent team · Stages 2–3 (Implement + Review) — produces the pre-gate package for Gate 2.',
  phases: [
    { title: 'Implement', detail: 'interface-freeze → plan-blind test-author ∥ writer → guarded green loop; converge to one change.' },
    { title: 'Review', detail: 'parallel lenses → severity-scaled refute-by-default verify → loop-until-dry → synthesize → handoff on go.' },
  ],
};

const MAX_FIX_FIRST_CYCLES = 2; // bounded Stage-3 → Stage-2 loop; then escalate.
const MAX_REVIEW_ROUNDS = 3; // loop-until-dry cap (backstop against a stuck critic).
const DRY_STREAK_TO_CONVERGE = 2; // K consecutive rounds with nothing new ⇒ converged.
const RETRY_LIMIT = 2; // bounded retry on a transient agent/validation failure.
const BUDGET_TIGHT_FRACTION = 0.2; // < 20% of the budget remaining ⇒ trim blocker skeptics 3 → 2.
// blocker → 2–3 distinct-angle skeptics; warn → 1; nit → logged unverified (GAP-5 / D6).
const SKEPTIC_ANGLES = ['reproduce', 'reachable', 'documented-exception'];

// Output-shape contracts. Each `required` list is a subset of that member's documented
// "Output" JSON keys (../agents/<name>.md); the engine validates against it. `required`
// = the keys the consuming code reads. Schema-LESS calls (interface-freeze, in-loop
// silent-failure-hunter, the PROSE handoff-assembler) are absent here on purpose (D1).
const SCHEMAS = {
  implementer: { required: ['ready_for_review'] },
  testAuthor: { required: ['tests'] },
  fanOutImplementer: { required: ['ready_for_review'] },
  schemaChangeExecutor: { required: ['escalate'] },
  planAdherenceSentinel: { required: ['verdict'] },
  greenKeeper: { required: ['loop', 'escalate'] },
  testTriage: { required: ['failures'] },
  deepDebugger: { required: ['escalate'] },
  findingVerifier: { required: ['survives'] },
  completenessCritic: { required: ['converged'] },
  reviewSynthesizer: { required: ['verdict'] },
  lens: { required: ['findings'] },
};

// Skill-backed review lenses (composed, not subagents). The runtime/operator dispatches
// these; the synthesizer is told to incorporate their findings. /code-review runs always
// (≥ Tier 0); /security-review only "when the diff warrants it" (finding-034 lens table),
// proxied by a data/privacy surface.
function skillLenses(manifest) {
  const skills = ['/code-review'];
  if (touchesDataSurface(manifest)) skills.push('/security-review');
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
//     only; Tier 1+ runs the FULL code-quality lens set.
function stage3Lenses(tier, manifest) {
  if (Array.isArray(manifest.review_lenses) && manifest.review_lenses.length) {
    return manifest.review_lenses.filter((l) => l in LENS_TO_AGENT).map((l) => LENS_TO_AGENT[l]);
  }
  const set = new Set(['convention-compliance']); // always (≥ Tier 0)
  if (tier >= 1) {
    for (const l of ['test-integrity', 'silent-failure-hunter', 'type-design-analyzer', 'pr-test-analyzer', 'comment-analyzer', 'architect-reviewer']) {
      set.add(l);
    }
  }
  // Factor-gated lenses fire by factor at ANY tier, not by tier threshold.
  if (touchesDataSurface(manifest)) set.add('phi-pii-guardian');
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
function anchorsExposed(manifest) {
  if (Array.isArray(manifest.applicable_anchors) && manifest.applicable_anchors.length) return true;
  const cc = manifest.change_class || [];
  return ['pipeline', 'schema', 'annotation-loader'].some((c) => cc.includes(c));
}

// ── Inlined agent seam (self-contained; no sibling import). ──────────────────
async function withRetry(thunk, who) {
  let lastErr;
  for (let attempt = 1; attempt <= RETRY_LIMIT; attempt++) {
    try {
      return await thunk();
    } catch (err) {
      lastErr = err;
      log(`[implement-review] retry ${attempt}/${RETRY_LIMIT} — ${who}: ${err && err.message ? err.message : err}`);
    }
  }
  throw new Error(
    `implement-review.js: ${who} failed after ${RETRY_LIMIT} attempts: ${lastErr && lastErr.message ? lastErr.message : lastErr}`,
  );
}

/**
 * Invoke a `.claude/agents/<name>.md` subagent. With `opts.schema` the engine returns
 * a schema-validated object; without it, the member's prose (e.g. handoff-assembler).
 */
async function call(agentType, input, opts) {
  const { schema, label } = opts || {};
  const prompt =
    `You are being invoked as the \`${agentType}\` subagent in the per-scope agent team's ` +
    `implement-review workflow. Follow your agent definition exactly and ` +
    (schema
      ? `return ONLY the JSON described in your "Output" section — no prose before or after.`
      : `return the document described in your "Output" section.`) +
    `\n\nINPUT (JSON):\n${JSON.stringify(input, null, 2)}`;
  log(`[implement-review] → ${label || agentType} (${agentType})`);
  return withRetry(() => agent(prompt, schema ? { agentType, schema } : { agentType }), agentType);
}

// ── Budget helpers. `budget.total` is null with no target (default path; probe-confirmed)
// → remaining is Infinity → the exhaustion guard is a no-op and skeptic width stays full. ─
function budgetRemaining() {
  if (!budget || typeof budget.total !== 'number' || typeof budget.spent !== 'function') return Infinity;
  return budget.total - budget.spent();
}
function budgetExhausted() {
  if (!budget || typeof budget.total !== 'number' || budget.total <= 0) return false;
  return budgetRemaining() <= 0;
}
function budgetTight() {
  if (!budget || typeof budget.total !== 'number' || budget.total <= 0 || typeof budget.spent !== 'function') return false;
  return budgetRemaining() / budget.total < BUDGET_TIGHT_FRACTION;
}

// `args` may be a bare scope id, a JSON string (the engine stringifies an object arg),
// or an object. Defensive parse — the C2D-Phase1 probe confirmed string delivery.
function parseArgs(raw) {
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) return raw;
  if (typeof raw === 'string') {
    const s = raw.trim();
    if (!s) return {};
    try {
      const parsed = JSON.parse(s);
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : { scope_id: String(parsed) };
    } catch (_e) {
      return { scope_id: s };
    }
  }
  return {};
}

function greenIsRed(green) {
  if (!green || green.escalate === true) return false; // an escalation is handled separately
  if (green.route && green.route !== 'none') return true;
  const loop = green.loop || {};
  return Object.keys(loop).some((k) => loop[k] === 'fail');
}

/** Stage 2 — converge to one green change under the guards. */
async function stageImplement(ctx, fixFindings) {
  const { manifest, plan, predicted_surprises } = ctx;
  const tier = Number(manifest.risk_tier);
  const members = stage2Members(tier, manifest);
  log(`[implement-review] Stage 2 · tier=${tier} · members=[${members.join(', ')}]`);

  const fanOut = members.includes('fan-out-implementer'); // wide & independent → REPLACES the implementer (D7)

  // Interface-freeze unblocks the plan-blind test-author. Schema-LESS: the implementer's
  // interface-freeze mode emits no `interface_contract` key (D1), so the test-author
  // falls back to the plan — interface-freeze is advisory, not a hard gate. Skipped on
  // the fan-out path, where the implementer is not invoked at all.
  let freeze = null;
  if (!fanOut) {
    freeze = await call('implementer', { manifest, plan, mode: 'interface-freeze', predicted_surprises }, { label: 'interface-freeze' });
  }

  // Plan-blind test-author ∥ the writer. The author is denied the implementation diff by
  // contract; here it receives ONLY the plan + the frozen interface (or the plan).
  const writerThunks = [];
  if (members.includes('test-author')) {
    writerThunks.push(() =>
      call(
        'test-author',
        { plan, interface_contract: (freeze && freeze.interface_contract) || plan, predicted_surprises },
        { schema: SCHEMAS.testAuthor, label: 'test-author (plan-blind)' },
      ),
    );
  }
  const writerIdx = writerThunks.length;
  if (fanOut) {
    // D7: fan-out-implementer REPLACES the single implementer for wide independent
    // mechanical breadth — do NOT also invoke the implementer (two writers collide).
    writerThunks.push(() =>
      call(
        'fan-out-implementer',
        { manifest, plan, predicted_surprises, fix_findings: fixFindings, isolation: 'worktree' },
        { schema: SCHEMAS.fanOutImplementer, label: 'fan-out-implementer' },
      ),
    );
  } else {
    writerThunks.push(() =>
      call('implementer', { manifest, plan, predicted_surprises, fix_findings: fixFindings }, { schema: SCHEMAS.implementer, label: 'implementer' }),
    );
  }
  const writerOut = await parallel(writerThunks);
  const impl = writerOut[writerIdx];
  if (!impl) throw new Error('implement-review.js: the writer (implementer / fan-out-implementer) returned no result');

  // schema side-channel: change_class ⊇ schema → schema-change-executor runs the
  // documented rebuild/re-ingest protocol (it owns the schema files; the implementer is
  // hook-blocked from them).
  let schemaExec = null;
  if (members.includes('schema-change-executor')) {
    schemaExec = await call('schema-change-executor', { manifest, plan, predicted_surprises }, { schema: SCHEMAS.schemaChangeExecutor, label: 'schema-change-executor' });
  }

  // Guards on the produced diff: sentinel (drift) + in-loop silent-failure (no verdict,
  // result discarded → schema-LESS per D1). The sentinel is the guard carrying a verdict.
  const guardThunks = [];
  if (members.includes('plan-adherence-sentinel')) {
    guardThunks.push(() => call('plan-adherence-sentinel', { manifest, plan, predicted_surprises }, { schema: SCHEMAS.planAdherenceSentinel, label: 'sentinel' }));
  }
  if (members.includes('silent-failure-hunter')) {
    guardThunks.push(() => call('silent-failure-hunter', { manifest, mode: 'in-loop' }, { label: 'silent-failure (in-loop)' }));
  }
  const guardOut = await parallel(guardThunks);
  const sentinel = guardOut.find((g) => g && 'verdict' in g);

  // green-keeper holds the dev-loop. On real red the in-loop sub-loop fires (GAP-4):
  // test-triage classifies → deep-debugger (tier ≥ 2, only when triage routes there).
  const green = await call('green-keeper', { manifest }, { schema: SCHEMAS.greenKeeper, label: 'green-keeper' });

  let triage = null;
  let debug = null;
  if (greenIsRed(green) && members.includes('test-triage')) {
    triage = await call('test-triage', { manifest, green }, { schema: SCHEMAS.testTriage, label: 'test-triage' });
    const routesDeep = (triage.failures || []).some((f) => f.route === 'deep-debugger');
    if (routesDeep && members.includes('deep-debugger')) {
      debug = await call('deep-debugger', { manifest, green, triage }, { schema: SCHEMAS.deepDebugger, label: 'deep-debugger' });
    }
  }

  const blocked =
    (sentinel && sentinel.verdict === 'escalate') ||
    Boolean(green && green.escalate) ||
    (debug && debug.escalate === true) ||
    (schemaExec && schemaExec.escalate === true) ||
    impl.ready_for_review === false;

  return { freeze, impl, schemaExec, sentinel, green, triage, debug, blocked, members };
}

/**
 * Severity-scaled, refute-by-default verification of ONE finding (GAP-5 / D6). blocker →
 * 2–3 distinct-angle skeptics in parallel (3, or 2 when the budget is tight); warn → 1.
 * strict-majority survives (> half not-refuted; a 1-1 tie on 2 → KILLED); a crashed
 * skeptic (null) counts as a refutation.
 */
async function verifyOne(finding, fromLens) {
  const blocker = finding.severity === 'blocker';
  const angles = blocker ? (budgetTight() ? SKEPTIC_ANGLES.slice(0, 2) : SKEPTIC_ANGLES) : SKEPTIC_ANGLES.slice(0, 1);
  const skeptics = await parallel(
    angles.map((angle) => () =>
      call('finding-verifier', { finding, from_lens: fromLens, angle }, { schema: SCHEMAS.findingVerifier, label: `verify:${finding.id}:${angle}` }),
    ),
  );
  const notRefuted = skeptics.filter((r) => r && r.survives === true).length;
  return {
    id: finding.id,
    finding,
    lens: fromLens,
    severity: finding.severity,
    survives: notRefuted > angles.length / 2, // strict majority; tie → KILLED (D6)
    skeptic_angles: angles,
    votes: skeptics.filter(Boolean).flatMap((s) => s.votes || []),
  };
}

/**
 * One review round. Verify-as-you-go (PIPELINE) is the default — each lens's findings flow
 * to the verifier as that lens completes (finding-034 "Pipeline, not barrier"). The BARRIER
 * (collect all lenses → dedup across lenses → verify once) is the documented exception for
 * scope with heavy cross-lens overlap, signalled by a wide blast_radius. `seenIds` dedups
 * findings across rounds so a stable finder is verified once. nits are logged unverified;
 * blocker/warn get the severity-scaled refute-by-default verifier.
 */
async function reviewRound(lenses, ctx, diffSummary, seenIds, useBarrier) {
  const { manifest, predicted_surprises } = ctx;
  const lensInput = { manifest, predicted_surprises, diff: diffSummary };

  const verifyFresh = async (fromLens, findings) => {
    const fresh = (findings || []).filter((f) => f && f.severity !== 'nit' && f.id && !seenIds.has(f.id));
    fresh.forEach((f) => seenIds.add(f.id));
    return (await parallel(fresh.map((f) => () => verifyOne(f, fromLens)))).filter(Boolean);
  };

  if (useBarrier) {
    // Barrier: collect every lens, dedup by id across lenses, then verify once.
    const lensOut = (
      await parallel(
        lenses.map((l) => () =>
          call(l, lensInput, { schema: SCHEMAS.lens, label: `lens:${l}` }).then((o) => ({
            lens: l,
            findings: (o && o.findings) || [],
            anchors_to_watch: (o && o.anchors_to_watch) || [],
          })),
        ),
      )
    ).filter(Boolean);
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

  // Pipeline: per lens, stage 1 runs the lens, stage 2 verifies its findings as it lands.
  const piped = (
    await pipeline(lenses, [
      (l) => call(l, lensInput, { schema: SCHEMAS.lens, label: `lens:${l}` }).then((o) => ({ lens: l, out: o || {} })),
      async ({ lens, out }) => {
        const verdicts = await verifyFresh(lens, out.findings || []);
        return { lens, findings: out.findings || [], anchors_to_watch: out.anchors_to_watch || [], verdicts };
      },
    ])
  ).filter(Boolean);
  const lensOut = piped.map(({ lens, findings, anchors_to_watch }) => ({ lens, findings, anchors_to_watch }));
  const verdicts = piped.flatMap((p) => p.verdicts || []);
  return { lensOut, verdicts };
}

/** Stage 3 — fan out lenses, adversarially verify, synthesize the pre-gate package. */
async function stageReview(ctx, diffSummary) {
  const { manifest, predicted_surprises } = ctx;
  const tier = Number(manifest.risk_tier);
  const lenses = stage3Lenses(tier, manifest);
  const skills = skillLenses(manifest);
  const useBarrier = wideAndIndependent(manifest); // heavy cross-lens overlap → dedup before verify
  log(
    `[implement-review] Stage 3 · tier=${tier} · lenses=[${lenses.join(', ')}] · skills=[${skills.join(', ')}] · ${useBarrier ? 'barrier' : 'pipeline'}`,
  );

  // Loop-until-dry at Tier 1+ (finding-034 recalibrated): the completeness-critic gates the
  // loop — keep running rounds until it reports converged, with a dry-streak + round cap as
  // backstops. Tier 0 is a single pass. `seenIds` keeps re-runs from re-verifying stable
  // findings, so a clean diff converges on round 1.
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

    const critic = await call(
      'completeness-critic',
      { manifest, ran_lenses: lenses, verdicts: allVerdicts, predicted_surprises, round },
      { schema: SCHEMAS.completenessCritic, label: `completeness-critic r${round}` },
    );
    if (critic && critic.converged === true) {
      log(`[implement-review] completeness-critic converged at round ${round}`);
      break;
    }
    dryStreak = allVerdicts.length - before === 0 ? dryStreak + 1 : 0;
    if (dryStreak >= DRY_STREAK_TO_CONVERGE) {
      log(`[implement-review] loop-until-dry: ${DRY_STREAK_TO_CONVERGE} consecutive dry rounds — converged`);
      break;
    }
    if (round === MAX_REVIEW_ROUNDS) log(`[implement-review] loop-until-dry hit round cap ${MAX_REVIEW_ROUNDS}`);
  }

  const survivors = allVerdicts.filter((v) => v.survives);

  // Synthesize the pre-gate package for VSC-User.
  const pkg = await call(
    'review-synthesizer',
    { manifest, lens_findings: allLensOut, verdicts: allVerdicts, predicted_surprises, composed_skills: skills },
    { schema: SCHEMAS.reviewSynthesizer, label: 'review-synthesizer' },
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
    log(`[implement-review] cycle ${cycle}/${MAX_FIX_FIRST_CYCLES + 1}`);

    stage2 = await stageImplement(ctx, fixFindings);
    if (stage2.blocked) {
      return done(ctx, stage2, null, 'escalate', 'Stage 2 sentinel/green-keeper/debugger escalation or implementer surprise');
    }

    stage3 = await stageReview(ctx, summarizeDiff(stage2));
    if (stage3.package.verdict === 'go') {
      // GAP-3 / D8: Stage-4 handoff assembly on the 'go' path, before the Gate-2 return.
      // handoff-assembler returns PROSE → call it schema-LESS; store the text on the package.
      const handoff = await call(
        'handoff-assembler',
        {
          pre_gate_package: stage3.package,
          manifest: ctx.manifest,
          predicted_surprises: ctx.predicted_surprises,
          composed_skills: ['/handoff', '/changelog', '/new-finding'],
        },
        { label: 'handoff-assembler' },
      );
      return done(ctx, stage2, stage3, 'go', null, handoff);
    }

    // fix-first → fold the blockers back into Stage 2, up to the cap.
    fixFindings = stage3.package.blockers || [];

    // D5 budget guard: exhausted before a go verdict → escalate (mirror the cap-hit path).
    if (budgetExhausted()) {
      return done(ctx, stage2, stage3, 'escalate', 'budget exhausted before a go verdict');
    }
    if (cycle === MAX_FIX_FIRST_CYCLES + 1) {
      return done(ctx, stage2, stage3, 'escalate', `fix-first loop hit the ${MAX_FIX_FIRST_CYCLES}-cycle cap`);
    }
  }
  return done(ctx, stage2, stage3, 'escalate', 'unreachable');
}

function summarizeDiff(stage2) {
  const impl = stage2.impl || {};
  const files = impl.files_touched || (impl.units || []).flatMap((u) => u.files || []);
  return { files_touched: files, green: stage2.green && stage2.green.loop };
}

function done(ctx, stage2, stage3, route, escalationReason, handoff) {
  const pkg = stage3 && stage3.package;
  // GAP-3: the assembled handoff (PROSE) rides on the package as a STRING — never coerced.
  const preGate = pkg ? { ...pkg, handoff: typeof handoff === 'string' ? handoff : handoff == null ? null : String(handoff) } : null;
  log(`[implement-review] route=${route}${escalationReason ? ` (${escalationReason})` : ''}`);
  return {
    scope_id: ctx.scope_id,
    risk_tier: Number(ctx.manifest.risk_tier),
    stage2: stage2 && { members: stage2.members, ready_for_review: !stage2.blocked },
    stage3: stage3 && { lenses: stage3.lenses, survivors: stage3.survivors_count },
    pre_gate_package: preGate, // verdict · blockers · warns · anchors_to_watch · residual_risk · handoff
    route_to: route === 'go' ? 'STAGE 4 handoff → Human Gate 2 (verification.md · merge)' : 'VSC-User (escalation)',
    escalation_reason: escalationReason,
    auto_merged: false, // never — Gate 2 is human.
  };
}

// ── Entry point. The engine delivers scope_id + approved plan + manifest via `args`. ─
const a = parseArgs(args);
const ctx = {
  scope_id: a.scope_id || (Array.isArray(a._) && a._[0]) || '',
  plan: a.plan || a.approved_plan || null,
  manifest: a.manifest || null,
  predicted_surprises: a.predicted_surprises || [],
};

let pkg;
try {
  pkg = await implementReview(ctx);
  log('[implement-review] Stage 2–3 complete — pre-gate package ready for VSC-User.');
} catch (err) {
  log(`[implement-review] Stage 2–3 FAILED: ${err && err.message ? err.message : err}`);
  throw err;
}
return pkg;

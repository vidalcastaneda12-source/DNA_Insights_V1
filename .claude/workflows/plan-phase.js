/**
 * plan-phase.js — per-scope agent team, Plan phase (Stages 0–1) orchestrator.
 *
 * Deterministic dynamic-workflow script for the team designed in
 * docs/findings/finding-034-agent-team-plan-phase.md. It chains the read-only
 * Plan-phase members defined in ../agents/*.md for ONE numbered ROADMAP scope
 * slot and produces the *pre-gate package* — a synthesized 8-section plan plus
 * the pre-mortem + auditor verdict — that VSC-User then approves (or not) at the
 * independent human plan-approval gate. The workflow NEVER auto-approves; the
 * gate stays human (finding-034).
 *
 * ── How it runs ────────────────────────────────────────────────────────────
 * Saved dynamic workflows live in `.claude/workflows/` and are invoked as a
 * command: `/plan-phase PR-6`. The scope id arrives via the runtime-provided
 * `args`. The model-driven conductor (`/scope-run`, retained as the headless/cron
 * fallback) launches this segment by name and pauses for the human at Gate 1.
 *
 * ── Runtime model (dynamic-workflows engine) ───────────────────────────────
 * The engine loads this file by reading the pure-literal `export const meta`
 * statically and wrapping the rest of the body in an async function with the
 * workflow hooks injected as parameters:
 *   agent · parallel · pipeline · log · phase · budget · workflow · args
 * The script is SELF-CONTAINED (no import / no Node API) and ends with a top-level
 * `return pkg`. Every subagent call funnels through the inline `call()` seam over
 * the injected `agent(prompt, {agentType, schema})` primitive (empirically
 * confirmed — C2D-Phase1 probe, finding-034 Amendment): the `schema` makes the
 * engine return a validated object, replacing the old hand-rolled output coercion
 * and key-assertion. Fan-out uses `parallel([() => call(...)])` — a rejected thunk
 * resolves to null in its slot (we `.filter(Boolean)`); thunks are always async so
 * a synchronous throw can never crash the run. The load model is validated by an
 * AsyncFunction construct-check, exactly how the engine wraps the body.
 *
 * Read-only contract: every member is read-only (Read/Grep/Glob/Bash). This
 * workflow produces a plan, never code and never a commit.
 */

export const meta = {
  name: 'plan-phase',
  description: 'Per-scope agent team · Stages 0–1 (Intake + Plan) — produces the pre-gate plan package for Gate 1.',
  phases: [
    { title: 'Intake', detail: 'scope-dispatcher → the scope manifest; risk_tier sets the depth for every downstream stage.' },
    { title: 'Plan', detail: 'tier-driven planner panel → judges → synthesize → pre-mortem → auditor panel → pre-gate package (bounded revise ×2).' },
  ],
};

// ── Tier → panel shape (finding-034 "Adaptive depth"). The dispatcher's
// risk_tier is the switch; this table is the depth knob it drives. ───────────
const PANEL = {
  0: {
    angles: ['minimal-diff'], // Tier 0 = the single minimal-diff planner (finding-034 / scope-run depth table).
    judge: null, // single candidate — nothing to compare
    premortemLenses: ['general'],
    auditorLenses: ['contract'],
  },
  1: {
    angles: ['minimal-diff', 'gate-backward'],
    judge: 'combined', // one light judge collapses all axes
    premortemLenses: ['general'],
    // Tier-1 auditor PANEL (finding-034 "Adaptive depth — recalibrated for correctness":
    // "auditor panel" at Tier 1). Two distinct-lens auditors → mergeAudits to the
    // strictest verdict, the in-loop analogue of an independent gate.
    auditorLenses: ['contract', 'architecture-fit'],
  },
  2: {
    angles: ['minimal-diff', 'gate-backward', 'risk-first', 'convention-purist'],
    judge: ['correctness', 'locked_decision_fit', 'verification', 'scope_discipline', 'risk'],
    // Standard Tier-2 pre-mortem = 2 skeptics (finding-034 deep_T2 def: "else standard
    // T2 (2 skeptics)"); deep_T2 adds the 3rd distinct lens below.
    premortemLenses: ['anchor-drift', 'schema-assumption'],
    auditorLenses: ['contract', 'architecture-fit'],
    // Tier 2 additionally runs a distinct architect-reviewer over the plan's design fit;
    // its findings (no verdict) fold into mergeAudits' severity→verdict ladder (GAP-6b).
  },
};

// deep_T2 widens the Tier-2 pre-mortem from 2 → 3 distinct-lens skeptics (finding-034
// deep_T2 def: "3 skeptics ... else standard T2 (2 skeptics)"). The 3rd lens is
// hidden-coupling — NOT completeness-critic, which is a Stage-3 review member, not a
// pre-mortem lens (finding-034 §"completeness-critic (Tier 2)").
const DEEP_T2_EXTRA_PREMORTEM_LENS = 'hidden-coupling';
const MAX_REVISE_CYCLES = 2; // bounded loop; then escalate to VSC-User.

// Output-shape contracts. Each `required` list is a subset of that member's documented
// "Output" JSON keys (../agents/<name>.md); the engine validates against it (replacing
// the old coerceJson + requireKeys). `required` = the keys the consuming code reads.
const SCHEMAS = {
  scopeDispatcher: { type: 'object', properties: { scope_id: {}, change_class: {}, risk_tier: {}, reading_list: {} }, required: ['scope_id', 'change_class', 'risk_tier', 'reading_list'], additionalProperties: true },
  planner: { type: 'object', properties: { implementation_plan: {}, verification: {}, confidence: {} }, required: ['implementation_plan', 'verification', 'confidence'], additionalProperties: true },
  planJudges: { type: 'object', properties: { scores: {} }, required: ['scores'], additionalProperties: true },
  planSynthesizer: { type: 'object', properties: { synthesized_plan: {}, divergence: {}, riskiest_assumptions: {} }, required: ['synthesized_plan', 'divergence', 'riskiest_assumptions'], additionalProperties: true },
  planPremortem: { type: 'object', properties: { recommend: {} }, required: ['recommend'], additionalProperties: true },
  planAuditor: { type: 'object', properties: { verdict: {} }, required: ['verdict'], additionalProperties: true },
  architectReviewer: { type: 'object', properties: { findings: {} }, required: ['findings'], additionalProperties: true },
};

// ── Inlined agent seam (self-contained; no sibling import). ──────────────────
const RETRY_LIMIT = 2; // bounded retry on a transient agent/validation failure.

async function withRetry(thunk, who) {
  let lastErr;
  for (let attempt = 1; attempt <= RETRY_LIMIT; attempt++) {
    try {
      return await thunk();
    } catch (err) {
      lastErr = err;
      log(`[plan-phase] retry ${attempt}/${RETRY_LIMIT} — ${who}: ${err && err.message ? err.message : err}`);
    }
  }
  throw new Error(
    `plan-phase.js: ${who} failed after ${RETRY_LIMIT} attempts: ${lastErr && lastErr.message ? lastErr.message : lastErr}`,
  );
}

/**
 * Invoke one `.claude/agents/<name>.md` subagent with a JSON-bearing prompt. With
 * `opts.schema` the engine returns a schema-validated object; without it, prose.
 */
async function call(agentType, input, opts) {
  const { schema, label, isolation } = opts || {};
  const prompt =
    `You are being invoked as the \`${agentType}\` subagent in the per-scope agent ` +
    `team's Plan-phase workflow. Follow your agent definition exactly and ` +
    (schema
      ? `return ONLY the JSON described in your "Output" section — no prose before or after.`
      : `return the document described in your "Output" section.`) +
    `\n\nINPUT (JSON):\n${JSON.stringify(input, null, 2)}`;
  log(`[plan-phase] → ${label || agentType} (${agentType})`);
  const agentOpts = { agentType };
  if (schema) agentOpts.schema = schema;
  if (isolation) agentOpts.isolation = isolation; // isolation:'worktree' → engine worktree directive; NOT probe/harness-exercised (deferred-unverified, D7/suite7). Only the fan-out writer passes it.
  return withRetry(() => agent(prompt, agentOpts), agentType);
}

// ── Budget helpers. `budget.total` is null when no target (the default path; the
// C2D-Phase1 probe confirmed this) → remaining is Infinity → guards are no-ops. ──
function budgetRemaining() {
  if (!budget || typeof budget.total !== 'number' || typeof budget.spent !== 'function') return Infinity;
  return budget.total - budget.spent();
}
function budgetExhausted() {
  if (!budget || typeof budget.total !== 'number' || budget.total <= 0) return false;
  return budgetRemaining() <= 0;
}

// Count-what-you-drop (CLAUDE.md): a parallel fan-out NULLs a rejected thunk and
// `.filter(Boolean)` removes it. When any member is lost, log how many of the attempted
// pool fell out + the pool identities, so a silent partial fan-out is observable.
function logDropped(site, attempted, kept) {
  const n = attempted.length - kept.length;
  if (n > 0) log(`[plan-phase] ${site}: ${n}/${attempted.length} dropped (rejected→null→filtered) — pool=[${attempted.join(', ')}]`);
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

/**
 * Run the full Plan phase for one scope id.
 * @param {string} scopeId e.g. "PR-6".
 */
async function planPhase(scopeId) {
  if (!scopeId) {
    throw new Error('plan-phase.js: no scope id. Invoke as `/plan-phase <SCOPE_ID>` (e.g. PR-6).');
  }

  // ── Stage 0 · Intake — the manifest is the single source of truth. ────────
  const manifest = await call('scope-dispatcher', { scope_id: scopeId }, { schema: SCHEMAS.scopeDispatcher, label: 'Stage 0 · dispatcher' });

  const tier = Number(manifest.risk_tier);
  const panel = PANEL[tier];
  if (!panel) throw new Error(`plan-phase.js: dispatcher returned unknown risk_tier=${manifest.risk_tier}`);
  const deepT2 = tier === 2 && Boolean(manifest.risk_breakdown && manifest.risk_breakdown.deep_T2);
  log(`[plan-phase] tier=${tier}${deepT2 ? ' (deep)' : ''} · angles=[${panel.angles.join(', ')}]`);

  // ── Stage 1 — bounded plan→audit loop. Each cycle re-fans the planners with
  // the prior auditor's findings folded in, per the ×2-then-escalate rule. ───
  let auditorFindings = null;
  let synthesized = null;
  let premortem = null;
  let audit = null;

  for (let cycle = 1; cycle <= MAX_REVISE_CYCLES + 1; cycle++) {
    log(`[plan-phase] Stage 1 · cycle ${cycle}/${MAX_REVISE_CYCLES + 1}`);

    // 1 · Fan out the planners in parallel — diversity by construction.
    const candidates = (
      await parallel(
        panel.angles.map((angle) => () =>
          call('planner', { manifest, angle, revision_findings: auditorFindings }, { schema: SCHEMAS.planner, label: `planner:${angle}` }),
        ),
      )
    ).filter(Boolean);
    logDropped('planners', panel.angles, candidates);
    if (!candidates.length) throw new Error('plan-phase.js: every planner failed to return a candidate');

    // 2 · Judge + 3 · Synthesize. A lone Tier-0 candidate skips the panel.
    if (candidates.length === 1) {
      synthesized = {
        scope_id: scopeId,
        synthesized_plan: candidates[0],
        graft_provenance: { skeleton: panel.angles[0] },
        divergence: [],
        riskiest_assumptions: [candidates[0].riskiest_assumption].filter(Boolean),
        panel_confidence: candidates[0].confidence,
      };
    } else {
      const axes = panel.judge === 'combined' ? ['combined'] : panel.judge;
      const scorecards = (
        await parallel(axes.map((axis) => () => call('plan-judges', { manifest, candidates, axis }, { schema: SCHEMAS.planJudges, label: `judge:${axis}` })))
      ).filter(Boolean);
      logDropped('judges', axes, scorecards);
      synthesized = await call('plan-synthesizer', { manifest, candidates, scorecards }, { schema: SCHEMAS.planSynthesizer, label: 'synthesizer' });
    }

    // 4 · Pre-mortem (fires at every tier). Tier 2 runs distinct-lens skeptics
    // in parallel and merges; deep_T2 adds the 3rd distinct-lens skeptic (2 → 3).
    const lenses = deepT2 ? [...panel.premortemLenses, DEEP_T2_EXTRA_PREMORTEM_LENS] : panel.premortemLenses;
    const premortems = (
      await parallel(
        lenses.map((lens) => () =>
          call('plan-premortem', { manifest, synthesized_plan: synthesized.synthesized_plan, lens }, { schema: SCHEMAS.planPremortem, label: `premortem:${lens}` }),
        ),
      )
    ).filter(Boolean);
    logDropped('premortem', lenses, premortems);
    premortem = mergePremortems(premortems, scopeId);

    // 5 · Audit — independent contract grade, consuming the pre-mortem. Tier 2 adds
    // the architecture-fit auditor lens AND a distinct architect-reviewer (GAP-6b);
    // mergeAudits folds the whole pool through one severity→verdict ladder.
    const auditThunks = panel.auditorLenses.map((lens) => () =>
      call('plan-auditor', { manifest, synthesized_plan: synthesized.synthesized_plan, premortem, lens }, { schema: SCHEMAS.planAuditor, label: `auditor:${lens}` }),
    );
    if (tier === 2) {
      auditThunks.push(() =>
        call(
          'architect-reviewer',
          { manifest, synthesized_plan: synthesized.synthesized_plan, premortem, lens: 'architecture-fit' },
          { schema: SCHEMAS.architectReviewer, label: 'architect-reviewer:plan' },
        ),
      );
    }
    const auditorIds = tier === 2 ? [...panel.auditorLenses, 'architect-reviewer'] : [...panel.auditorLenses];
    const audits = (await parallel(auditThunks)).filter(Boolean);
    logDropped('auditors', auditorIds, audits);
    audit = mergeAudits(audits, scopeId);

    if (audit.verdict === 'ready' || audit.verdict === 'escalate') break;

    // verdict === 'revise' → fold findings back and re-fan, up to the cap.
    auditorFindings = audit.findings || [];

    // D5 budget guard: exhausted before a ready verdict → stamp escalate (mirror the
    // cap-hit path below). With no budget target, budgetExhausted() is false → unchanged.
    if (budgetExhausted()) {
      audit.verdict = 'escalate';
      audit.escalation_reason = 'budget exhausted before a ready verdict';
      break;
    }
    if (cycle === MAX_REVISE_CYCLES + 1 || cycle > MAX_REVISE_CYCLES) {
      audit.verdict = 'escalate';
      audit.escalation_reason = `revise loop hit the ${MAX_REVISE_CYCLES}-cycle cap without a ready verdict`;
      break;
    }
  }

  // ── The pre-gate package. Routing is advisory; the human gate is the gate. ─
  const route = audit.verdict === 'ready' ? 'human-plan-approval-gate (VSC-User)' : 'VSC-User (escalation)';
  log(`[plan-phase] verdict=${audit.verdict} · premortem=${premortem.recommend} → ${route}`);

  return {
    scope_id: scopeId,
    risk_tier: tier,
    deep_T2: deepT2,
    manifest,
    plan: synthesized.synthesized_plan,
    divergence: synthesized.divergence,
    riskiest_assumptions: synthesized.riskiest_assumptions,
    premortem,
    audit,
    route_to: route,
    // The Plan phase ENDS at a human decision — never an auto-approval.
    auto_approved: false,
  };
}

/** Merge N pre-mortems → strictest recommendation wins (probe-first > revise > proceed). */
function mergePremortems(premortems, scopeId) {
  // Fail-closed: an empty pre-mortem pool (every skeptic rejected → nulled → filtered) must
  // NOT collapse to the permissive default 'proceed'. Reduce to the conservative recommend.
  if (!premortems.length) {
    return {
      scope_id: scopeId,
      recommend: 'probe-first',
      predicted_surprises: [],
      anchors_at_risk: [],
      escalation_reason: 'every pre-mortem skeptic failed to return — failing closed to probe-first',
    };
  }
  const order = { proceed: 0, revise: 1, 'probe-first': 2 };
  let recommend = 'proceed';
  const predicted = [];
  const anchors = new Set();
  for (const pm of premortems) {
    if ((order[pm.recommend] ?? 0) > order[recommend]) recommend = pm.recommend;
    for (const s of pm.predicted_surprises || []) predicted.push({ lens: pm.lens, ...s });
    for (const a of pm.anchors_at_risk || []) anchors.add(a);
  }
  return { scope_id: scopeId, recommend, predicted_surprises: predicted, anchors_at_risk: [...anchors] };
}

/** nit < warn < blocker. */
function severityRank(sev) {
  const r = { nit: 0, warn: 1, blocker: 2 };
  return r[sev] ?? 0;
}

/**
 * Merge N audits → strictest verdict wins (escalate > revise > ready), over ONE
 * severity→verdict ladder across the whole findings pool. This generalizes the merge
 * so it also handles the Tier-2 architect-reviewer, which emits `findings` but NO
 * top-level `verdict` (GAP-6b): a surviving blocker — from any contributor — forces at
 * least a revise, so a blocker-level architecture finding can never silently pass.
 */
function mergeAudits(audits, scopeId) {
  // Fail-closed: with NO verdict-bearing auditor in the pool (every plan-auditor rejected/crashed
  // → nulled → filtered, leaving at most the verdict-LESS architect-reviewer), the plan is
  // UNAUDITED — it must NOT collapse to the permissive default 'ready'. An unaudited plan escalates.
  const verdictful = audits.filter((a) => a && typeof a.verdict === 'string');
  if (!verdictful.length) {
    return {
      scope_id: scopeId,
      verdict: 'escalate',
      findings: [],
      escalation_reason: 'no plan-auditor returned a verdict (all crashed) — unaudited plan, failing closed to escalate',
    };
  }
  const order = { ready: 0, revise: 1, escalate: 2 };
  let verdict = 'ready';
  const findings = [];
  for (const a of audits) {
    if (a && typeof a.verdict === 'string' && (order[a.verdict] ?? 0) > order[verdict]) verdict = a.verdict;
    for (const f of (a && a.findings) || []) findings.push({ lens: a && a.lens, ...f });
  }
  const maxSev = findings.reduce((m, f) => (severityRank(f.severity) > severityRank(m) ? f.severity : m), 'nit');
  if (severityRank(maxSev) >= severityRank('blocker') && order[verdict] < order.revise) verdict = 'revise';
  return { scope_id: scopeId, verdict, findings };
}

// ── Entry point. The engine passes the user's args via `args`. ───────────────
const a = parseArgs(args);
const scopeId = a.scope_id || (Array.isArray(a._) && a._[0]) || '';

let pkg;
try {
  pkg = await planPhase(scopeId);
  log('[plan-phase] Plan phase complete — pre-gate package ready for VSC-User.');
} catch (err) {
  log(`[plan-phase] Plan phase FAILED: ${err && err.message ? err.message : err}`);
  throw err;
}
return pkg;

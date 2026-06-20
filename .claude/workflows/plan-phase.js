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
 * Saved dynamic workflows live in `.claude/workflows/` (project, shared with
 * clones) and are invoked as a command: `/plan-phase PR-6`. The scope id arrives
 * via the runtime-provided global `args`. The workflow is intentionally OPT-IN —
 * it is NOT wired into settings.json; VSC-User triggers a per-scope run. The
 * members remain usable standalone via the Task/Agent tool.
 *
 * ── One undocumented dependency, isolated on purpose ───────────────────────
 * The dynamic-workflows runtime executes JS that orchestrates subagents, but the
 * exact subagent-invocation primitive is not part of Claude Code's *public*
 * authoring API (confirmed via claude-code-guide against
 * https://code.claude.com/docs/en/workflows.md). Every subagent call in this
 * file therefore funnels through the single `runAgent()` helper below. If a given
 * runtime exposes the primitive under a different name/shape, adjust that ONE
 * function and the whole chain follows — the orchestration logic itself
 * (tier-driven fan-out, parallel awaits, output validation, the bounded revise
 * loop) is runtime-agnostic. The helper tries the known conventions and throws a
 * loud, actionable error if none resolve, rather than silently no-op'ing.
 *
 * Read-only contract: every member is read-only (Read/Grep/Glob/Bash). This
 * workflow produces a plan, never code and never a commit.
 */

'use strict';

// ── Tier → panel shape (finding-034 "Adaptive depth"). The dispatcher's
// risk_tier is the switch; this table is the depth knob it drives. ───────────
const PANEL = {
  0: {
    angles: ['general'],
    judge: null, // single candidate — nothing to compare
    premortemLenses: ['general'],
    auditorLenses: ['contract'],
  },
  1: {
    angles: ['minimal-diff', 'gate-backward'],
    judge: 'combined', // one light judge collapses all axes
    premortemLenses: ['general'],
    auditorLenses: ['contract'],
  },
  2: {
    angles: ['minimal-diff', 'gate-backward', 'risk-first', 'convention-purist'],
    judge: ['correctness', 'locked_decision_fit', 'verification', 'scope_discipline', 'risk'],
    premortemLenses: ['anchor-drift', 'schema-assumption', 'hidden-coupling'],
    auditorLenses: ['contract', 'architecture-fit'],
  },
};

// deep-T2 widens the pre-mortem skeptic panel; finding-034 §"Risk-tier scoring".
const DEEP_T2_EXTRA_LENS = 'completeness-critic';
const MAX_REVISE_CYCLES = 2; // bounded loop; then escalate to VSC-User.

/**
 * Invoke one `.claude/agents/<name>.md` subagent with a JSON-bearing prompt and
 * return its parsed JSON output. THE one runtime-coupled call — see the header.
 *
 * @param {string} name   agent name, matching the `name:` frontmatter of an
 *                         ../agents/<name>.md file.
 * @param {object} input  structured input; serialized into the prompt. Members
 *                         document their own input shape in their "Inputs" block.
 * @param {string} role   short label used only for progress lines.
 * @returns {Promise<object>} the member's parsed JSON "Output" object.
 */
async function runAgent(name, input, role) {
  const prompt =
    `You are being invoked as the \`${name}\` subagent in the per-scope agent ` +
    `team's Plan-phase workflow. Follow your agent definition exactly and return ` +
    `ONLY the JSON described in your "Output" section — no prose before or after.\n\n` +
    `INPUT (JSON):\n${JSON.stringify(input, null, 2)}`;

  progress(`→ ${role || name} (${name})`);

  // The dynamic-workflows runtime exposes a subagent primitive in the script's
  // global scope; its public name is undocumented, so probe the known shapes.
  // Each is expected to resolve to the agent's final text. Keep this the ONLY
  // place that knows the primitive's name.
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
      `plan-phase.js: no subagent-invocation primitive found in the workflow ` +
        `runtime global scope (tried runAgent/invokeSubagent/task/agent). The ` +
        `dynamic-workflows JS authoring API is not publicly documented — inspect ` +
        `a generated workflow under ~/.claude/projects/<session>/ to find the ` +
        `real primitive, then wire it into runAgent() in this file (the only ` +
        `place that needs to change).`,
    );
  }

  return coerceJson(raw, name);
}

/** Parse a member's output to JSON, tolerating a ```json fenced block. */
function coerceJson(raw, name) {
  if (raw && typeof raw === 'object') return raw; // already parsed by the runtime
  const text = String(raw == null ? '' : raw);
  const fenced = text.match(/```(?:json[c]?)?\s*([\s\S]*?)```/i);
  const body = (fenced ? fenced[1] : text).trim();
  try {
    return JSON.parse(body);
  } catch (err) {
    throw new Error(
      `plan-phase.js: ${name} did not return parseable JSON (the member contract ` +
        `is "return only this JSON"). First 200 chars: ${body.slice(0, 200)}`,
    );
  }
}

/** Assert a member output carries the keys the next stage consumes. */
function requireKeys(obj, keys, who) {
  const missing = keys.filter((k) => !(k in (obj || {})));
  if (missing.length) {
    throw new Error(`plan-phase.js: ${who} output missing required keys: ${missing.join(', ')}`);
  }
  return obj;
}

function progress(msg) {
  // Per-step progress so the wall-clock window is observable (CLAUDE.md
  // performance convention). The runtime may also surface a progress sink; this
  // stays useful regardless.
  if (typeof globalThis.emitProgress === 'function') globalThis.emitProgress(msg);
  else console.log(`[plan-phase] ${msg}`);
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
  const manifest = requireKeys(
    await runAgent('scope-dispatcher', { scope_id: scopeId }, 'Stage 0 · dispatcher'),
    ['scope_id', 'change_class', 'risk_tier', 'reading_list'],
    'scope-dispatcher',
  );

  const tier = Number(manifest.risk_tier);
  const panel = PANEL[tier];
  if (!panel) throw new Error(`plan-phase.js: dispatcher returned unknown risk_tier=${manifest.risk_tier}`);
  const deepT2 = tier === 2 && Boolean(manifest.risk_breakdown && manifest.risk_breakdown.deep_T2);
  progress(`tier=${tier}${deepT2 ? ' (deep)' : ''} · angles=[${panel.angles.join(', ')}]`);

  // ── Stage 1 — bounded plan→audit loop. Each cycle re-fans the planners with
  // the prior auditor's findings folded in, per the ×2-then-escalate rule. ───
  let auditorFindings = null;
  let synthesized = null;
  let premortem = null;
  let audit = null;

  for (let cycle = 1; cycle <= MAX_REVISE_CYCLES + 1; cycle++) {
    progress(`Stage 1 · cycle ${cycle}/${MAX_REVISE_CYCLES + 1}`);

    // 1 · Fan out the planners in parallel — diversity by construction.
    const candidates = await Promise.all(
      panel.angles.map((angle) =>
        runAgent(
          'planner',
          { manifest, angle, revision_findings: auditorFindings },
          `planner:${angle}`,
        ).then((p) => requireKeys(p, ['implementation_plan', 'verification', 'confidence'], `planner:${angle}`)),
      ),
    );

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
      const scorecards = await Promise.all(
        axes.map((axis) => runAgent('plan-judges', { manifest, candidates, axis }, `judge:${axis}`)),
      );
      synthesized = requireKeys(
        await runAgent(
          'plan-synthesizer',
          { manifest, candidates, scorecards },
          'synthesizer',
        ),
        ['synthesized_plan', 'divergence', 'riskiest_assumptions'],
        'plan-synthesizer',
      );
    }

    // 4 · Pre-mortem (fires at every tier). Tier 2 runs distinct-lens skeptics
    // in parallel and merges; deep-T2 adds the completeness critic.
    const lenses = deepT2 ? [...panel.premortemLenses, DEEP_T2_EXTRA_LENS] : panel.premortemLenses;
    const premortems = await Promise.all(
      lenses.map((lens) =>
        runAgent(
          'plan-premortem',
          { manifest, synthesized_plan: synthesized.synthesized_plan, lens },
          `premortem:${lens}`,
        ),
      ),
    );
    premortem = mergePremortems(premortems, scopeId);

    // 5 · Audit — independent contract grade, consuming the pre-mortem. Tier 2
    // adds the architecture-fit lens; merge to the strictest verdict.
    const audits = await Promise.all(
      panel.auditorLenses.map((lens) =>
        runAgent(
          'plan-auditor',
          { manifest, synthesized_plan: synthesized.synthesized_plan, premortem, lens },
          `auditor:${lens}`,
        ),
      ),
    );
    audit = mergeAudits(audits, scopeId);

    if (audit.verdict === 'ready' || audit.verdict === 'escalate') break;

    // verdict === 'revise' → fold findings back and re-fan, up to the cap.
    auditorFindings = audit.findings || [];
    if (cycle === MAX_REVISE_CYCLES + 1 || cycle > MAX_REVISE_CYCLES) {
      audit.verdict = 'escalate';
      audit.escalation_reason = `revise loop hit the ${MAX_REVISE_CYCLES}-cycle cap without a ready verdict`;
      break;
    }
  }

  // ── The pre-gate package. Routing is advisory; the human gate is the gate. ─
  const route =
    audit.verdict === 'ready'
      ? 'human-plan-approval-gate (VSC-User)'
      : 'VSC-User (escalation)';
  progress(`verdict=${audit.verdict} · premortem=${premortem.recommend} → ${route}`);

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

/** Merge N audits → strictest verdict wins (escalate > revise > ready). */
function mergeAudits(audits, scopeId) {
  const order = { ready: 0, revise: 1, escalate: 2 };
  let verdict = 'ready';
  const findings = [];
  for (const a of audits) {
    if ((order[a.verdict] ?? 0) > order[verdict]) verdict = a.verdict;
    for (const f of a.findings || []) findings.push({ lens: a.lens, ...f });
  }
  // A surviving blocker forces at least a revise regardless of the headline verdict.
  if (verdict === 'ready' && findings.some((f) => f.severity === 'blocker')) verdict = 'revise';
  return { scope_id: scopeId, verdict, findings };
}

// ── Entry point. The runtime passes the user's args via the global `args`. ───
// `/plan-phase PR-6` → scopeId = "PR-6". Falls back to args.scope_id if the
// runtime delivers a structured object instead of a bare string.
const _args = typeof args !== 'undefined' ? args : globalThis.args;
const _scopeId =
  (typeof _args === 'string'
    ? _args.trim()
    : (_args && (_args.scope_id || (Array.isArray(_args._) && _args._[0]))) || '') ||
  // Fallback so the workflow is also runnable/testable as `node plan-phase.js PR-6`.
  (typeof process !== 'undefined' && process.argv && process.argv[2]) ||
  '';

// Export for unit tests under a CommonJS loader; harmless/skipped in a runtime
// that has no `module` (the dynamic-workflows sandbox may not be CommonJS).
const _hasModule = typeof module !== 'undefined' && module && module.exports;
if (_hasModule) module.exports = { planPhase, runAgent, PANEL };

// Auto-run when executed as a workflow, NOT when require()'d by a test.
const _requiredByTest = _hasModule && typeof require !== 'undefined' && require.main !== module;
if (!_requiredByTest) {
  planPhase(_scopeId)
    .then((pkg) => {
      progress('Plan phase complete — pre-gate package ready for VSC-User.');
      if (typeof globalThis.setResult === 'function') globalThis.setResult(pkg);
      else console.log(JSON.stringify(pkg, null, 2));
    })
    .catch((err) => {
      progress(`Plan phase FAILED: ${err.message}`);
      throw err;
    });
}

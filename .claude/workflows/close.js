/**
 * close.js — per-scope agent team, Stage 5 (Close).
 *
 * The final segment of the PR lifecycle designed in
 * docs/findings/finding-034-agent-team-plan-phase.md. It runs AFTER Human Gate 2
 * (VSC-User ran verification.md, confirmed the anchors on real data, and MERGED).
 * It is the team's last act and the only stage that writes durable docs, so it is
 * the most carefully gated.
 *
 *   plan-phase.js          → Gate 1 (approve plan)
 *   implement-review.js    → Gate 2 (verification.md · merge)
 *   close.js (THIS)        → post-merge re-lock + backlog
 *
 * Two members:
 *   • knowledge-curator — re-locks the anchors VSC-User CONFIRMED at the gate
 *     (CLAUDE.md / verification.md / the finding's bedrock table), flips the ROADMAP
 *     slot, cross-links — under supersession, HUMAN-CONFIRMED numbers only, via a
 *     REVIEWABLE change, never a silent push to main. It writes only the gate's
 *     numbers, never the regression-hunter's prediction; a missing confirmed number
 *     is an escalation, not a guess. This closes the anchor loop:
 *     predict (S1) → flag (S3) → confirm (gate) → record (S5).
 *   • repo-sweep — whole-repo staleness → a ranked backlog for the next item
 *     (detect, never fix; non-blocking).
 *
 * ── Runtime model (dynamic-workflows engine) ──────────────────────────────────
 * This script runs under the Claude Code dynamic-workflows engine. The engine loads
 * it by reading the pure-literal `export const meta` statically and wrapping the rest
 * of the body in an async function with the workflow hooks injected as parameters:
 *   agent · parallel · pipeline · log · phase · budget · workflow · args
 * The script is therefore SELF-CONTAINED — no `import`/no Node API — and ends with a
 * top-level `return pkg`. Subagents are invoked through the injected `agent(prompt,
 * {agentType, schema})` primitive (empirically confirmed — see the C2D-Phase1 probe,
 * finding-034 Amendment): a schema-bearing call returns a validated object (so the
 * old hand-rolled JSON coercion is gone); a schema-less call returns the member's
 * prose. Every `parallel`/`pipeline` thunk is async (`() => call(...)`): a rejected
 * promise resolves to null in its slot (we `.filter(Boolean)`), while a synchronous
 * throw would crash the run. The load model is validated by an AsyncFunction
 * construct-check (the harness mirrors exactly how the engine loads the file).
 *
 * Args: `/close PR-6` plus the confirmed gate anchors via the runtime `args`
 *   { scope_id: "PR-6", confirmed_anchors: [ {name, value, src} ], merged_sha: "…" }
 * `args` may arrive as a string (JSON, or a bare scope id) or an object — parsed below.
 */

export const meta = {
  name: 'close',
  description: 'Per-scope agent team · Stage 5 (Close) — post-merge anchor re-lock + ranked backlog.',
  phases: [
    {
      title: 'Close',
      detail:
        'knowledge-curator re-locks gate-confirmed anchors (reviewable change) ∥ repo-sweep files the backlog.',
    },
  ],
};

// ── Inlined agent seam (self-contained; no sibling import). Every subagent call
// funnels through call() → withRetry() → the injected agent() primitive. ────────
const RETRY_LIMIT = 2; // bounded retry on a transient agent/validation failure.

async function withRetry(thunk, who) {
  let lastErr;
  for (let attempt = 1; attempt <= RETRY_LIMIT; attempt++) {
    try {
      return await thunk();
    } catch (err) {
      lastErr = err;
      log(`[close] retry ${attempt}/${RETRY_LIMIT} — ${who}: ${err && err.message ? err.message : err}`);
    }
  }
  throw new Error(
    `close.js: ${who} failed after ${RETRY_LIMIT} attempts: ${lastErr && lastErr.message ? lastErr.message : lastErr}`,
  );
}

/**
 * Invoke a `.claude/agents/<name>.md` subagent. With `opts.schema` the engine returns
 * a schema-validated object; without it, the member's prose text is returned.
 */
async function call(agentType, input, opts) {
  const { schema, label } = opts || {};
  const prompt =
    `You are being invoked as the \`${agentType}\` subagent in the per-scope agent team's ` +
    `close workflow. Follow your agent definition exactly and ` +
    (schema
      ? `return ONLY the JSON described in your "Output" section — no prose before or after.`
      : `return the document described in your "Output" section.`) +
    `\n\nINPUT (JSON):\n${JSON.stringify(input, null, 2)}`;
  log(`[close] → ${label || agentType} (${agentType})`);
  return withRetry(() => agent(prompt, schema ? { agentType, schema } : { agentType }), agentType);
}

// Output-shape contracts. Each `required` list is a subset of that member's documented
// "Output" JSON keys (.claude/agents/<name>.md); the engine validates against it.
const SCHEMAS = {
  repoSweep: { required: ['fruit'] },
  knowledgeCurator: { required: ['relocks', 'cross_check_passed'] },
};

// `args` may be a bare scope id, a JSON string (the engine stringifies an object arg),
// or an object. Defensive parse — the C2D-Phase1 probe confirmed the string delivery.
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
 * Run Stage 5 for one merged scope item.
 * @param {object} ctx { scope_id, confirmed_anchors, merged_sha }
 */
async function close(ctx) {
  if (!ctx.scope_id) {
    throw new Error('close.js: no scope id. Invoke as `/close <SCOPE_ID>` with confirmed gate anchors in args.');
  }
  // The curator writes ONLY human-confirmed numbers. No confirmed set ⇒ nothing to
  // re-lock safely ⇒ refuse rather than guess (the Stage-5 guardrail).
  if (!Array.isArray(ctx.confirmed_anchors)) {
    throw new Error(
      'close.js: confirmed_anchors missing. Stage 5 re-locks ONLY the numbers VSC-User ' +
        'confirmed at the gate — never a prediction. Pass args.confirmed_anchors (may be []).',
    );
  }

  // repo-sweep is independent detection; the curator needs the confirmed numbers. Run
  // both concurrently — the sweep does not gate the re-lock. An empty confirmed set is
  // a deliberate no-op (nothing to re-lock), never a curator invocation.
  const [sweep, relock] = await parallel([
    () =>
      call(
        'repo-sweep',
        { scope_id: ctx.scope_id, mode: 'post-merge-triage', merged_sha: ctx.merged_sha },
        { schema: SCHEMAS.repoSweep, label: 'repo-sweep' },
      ),
    () =>
      ctx.confirmed_anchors.length
        ? call(
            'knowledge-curator',
            { scope_id: ctx.scope_id, confirmed_anchors: ctx.confirmed_anchors, merged_sha: ctx.merged_sha },
            { schema: SCHEMAS.knowledgeCurator, label: 'knowledge-curator' },
          )
        : Promise.resolve({ relocks: [], roadmap_flip: null, cross_check_passed: true, escalations: ['no confirmed anchors to re-lock'] }),
  ]);

  // null = the concurrent call failed (parallel nulls an async rejection). The curator
  // failing is a hard stop for the re-lock; the sweep failing only thins the backlog.
  const sweepOut = sweep || { fruit: [], maybe: [], skipped_count: 0 };
  const relockOut = relock || { relocks: [], roadmap_flip: null, cross_check_passed: false, escalations: ['knowledge-curator returned no result'] };

  const escalations = (relockOut.escalations || []).concat(
    relockOut.cross_check_passed === false ? ['curator cross-check FAILED — re-lock did not match the gate set'] : [],
  );

  log(`[close] re-locked ${(relockOut.relocks || []).length} anchor(s); backlog ${(sweepOut.fruit || []).length} item(s)`);

  return {
    scope_id: ctx.scope_id,
    relock: {
      relocks: relockOut.relocks || [],
      roadmap_flip: relockOut.roadmap_flip || null,
      cross_links: relockOut.cross_links || [],
      cross_check_passed: relockOut.cross_check_passed !== false,
      // The re-lock lands as a REVIEWABLE change (fast-follow doc PR or folded into the
      // scope PR) — never a direct push to main. The curator returns the diff/PR ref.
      reviewable_change: true,
    },
    backlog: { fruit: sweepOut.fruit || [], maybe: sweepOut.maybe || [], skipped_count: sweepOut.skipped_count || 0 },
    escalations,
    // The anchor loop is closed: predict → flag → confirm → record.
    anchor_loop: 'predict(S1) → flag(S3) → confirm(gate) → record(S5)',
  };
}

// ── Entry point. The engine delivers scope_id + confirmed_anchors via `args`. ────
const a = parseArgs(args);
const ctx = {
  scope_id: a.scope_id || (Array.isArray(a._) && a._[0]) || '',
  confirmed_anchors: a.confirmed_anchors, // intentionally undefined-preserving (see refuse-guard)
  merged_sha: a.merged_sha || null,
};

let pkg;
try {
  pkg = await close(ctx);
  log('[close] Stage 5 complete — anchor loop closed; backlog filed.');
} catch (err) {
  log(`[close] Stage 5 FAILED: ${err && err.message ? err.message : err}`);
  throw err;
}
return pkg;

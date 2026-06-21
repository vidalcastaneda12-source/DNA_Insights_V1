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
 * Runtime caveat identical to plan-phase.js / implement-review.js: the subagent
 * primitive is undocumented and isolated behind the single runAgent() helper.
 *
 * Args: `/close PR-6` plus the confirmed gate anchors via the runtime global `args`
 * (args.confirmed_anchors), e.g.
 *   { scope_id: "PR-6", confirmed_anchors: [ {name, value, src} ], merged_sha: "…" }
 */

'use strict';

/** THE one runtime-coupled call — see header. Invoke a subagent, return parsed JSON. */
async function runAgent(name, input, role) {
  const prompt =
    `You are being invoked as the \`${name}\` subagent in the per-scope agent team's ` +
    `close workflow. Follow your agent definition exactly and return ONLY the JSON ` +
    `described in your "Output" section — no prose before or after.\n\n` +
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
      `close.js: no subagent-invocation primitive found in the workflow runtime global ` +
        `scope (tried runAgent/invokeSubagent/task/agent). The dynamic-workflows JS ` +
        `authoring API is not publicly documented — inspect a generated workflow under ` +
        `~/.claude/projects/<session>/ to find the real primitive, then wire it into ` +
        `runAgent() in this file (the only place to change).`,
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
      `close.js: ${name} did not return parseable JSON (members must "return only this ` +
        `JSON"). First 200 chars: ${body.slice(0, 200)}`,
    );
  }
}

function progress(msg) {
  if (typeof globalThis.emitProgress === 'function') globalThis.emitProgress(msg);
  else console.log(`[close] ${msg}`);
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

  // repo-sweep is independent detection; the curator needs the confirmed numbers.
  // Run both concurrently — the sweep does not gate the re-lock.
  const [sweep, relock] = await Promise.all([
    runAgent('repo-sweep', { scope_id: ctx.scope_id, mode: 'post-merge-triage', merged_sha: ctx.merged_sha }, 'repo-sweep'),
    ctx.confirmed_anchors.length
      ? runAgent(
          'knowledge-curator',
          { scope_id: ctx.scope_id, confirmed_anchors: ctx.confirmed_anchors, merged_sha: ctx.merged_sha },
          'knowledge-curator',
        )
      : Promise.resolve({ relocks: [], roadmap_flip: null, cross_check_passed: true, escalations: ['no confirmed anchors to re-lock'] }),
  ]);

  const escalations = (relock.escalations || []).concat(
    relock.cross_check_passed === false ? ['curator cross-check FAILED — re-lock did not match the gate set'] : [],
  );

  progress(`re-locked ${(relock.relocks || []).length} anchor(s); backlog ${(sweep.fruit || []).length} item(s)`);

  return {
    scope_id: ctx.scope_id,
    relock: {
      relocks: relock.relocks || [],
      roadmap_flip: relock.roadmap_flip || null,
      cross_links: relock.cross_links || [],
      cross_check_passed: relock.cross_check_passed !== false,
      // The re-lock lands as a REVIEWABLE change (fast-follow doc PR or folded into the
      // scope PR) — never a direct push to main. The curator returns the diff/PR ref.
      reviewable_change: true,
    },
    backlog: { fruit: sweep.fruit || [], maybe: sweep.maybe || [], skipped_count: sweep.skipped_count || 0 },
    escalations,
    // The anchor loop is closed: predict → flag → confirm → record.
    anchor_loop: 'predict(S1) → flag(S3) → confirm(gate) → record(S5)',
  };
}

// ── Entry point. Runtime delivers scope_id + confirmed_anchors via `args`. ───
const _args = typeof args !== 'undefined' ? args : globalThis.args;
const _ctx =
  _args && typeof _args === 'object' && !Array.isArray(_args)
    ? {
        scope_id: _args.scope_id || (Array.isArray(_args._) && _args._[0]) || '',
        confirmed_anchors: _args.confirmed_anchors, // intentionally undefined-preserving (see guard)
        merged_sha: _args.merged_sha || null,
      }
    : {
        scope_id:
          (typeof _args === 'string' && _args.trim()) ||
          (typeof process !== 'undefined' && process.argv && process.argv[2]) ||
          '',
        confirmed_anchors: undefined,
        merged_sha: null,
      };

const _hasModule = typeof module !== 'undefined' && module && module.exports;
if (_hasModule) module.exports = { close, runAgent };

const _requiredByTest = _hasModule && typeof require !== 'undefined' && require.main !== module;
if (!_requiredByTest) {
  close(_ctx)
    .then((pkg) => {
      progress('Stage 5 complete — anchor loop closed; backlog filed.');
      if (typeof globalThis.setResult === 'function') globalThis.setResult(pkg);
      else console.log(JSON.stringify(pkg, null, 2));
    })
    .catch((err) => {
      progress(`Stage 5 FAILED: ${err.message}`);
      throw err;
    });
}

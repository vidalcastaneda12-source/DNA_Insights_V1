export const meta = {
  name: 'c2d-d7-probe',
  description:
    'C2D Phase 2 D7: live-engine RUN semantics for the four trigger-gated Stage-2 writers (agentType resolution + StructuredOutput schema validation), fanned through parallel().',
  phases: [{ title: 'Probe', detail: 'parallel agentType-resolution + schema-validation for the 4 writers' }],
}
phase('Probe')
// The four trigger-gated Stage-2 writers the C2D-Phase1 harness covered only on SYNTHETIC
// manifests (finding-034 "C2D-Phase1 residual risk" D7). This exercises their LIVE-ENGINE run
// semantics: the engine resolves each agentType and validates a schema-bearing call (the post-PR-1
// shape), fanned through parallel() to also exercise the fail-closed fan-out seam.
const WRITERS = ['schema-change-executor', 'fan-out-implementer', 'test-triage', 'deep-debugger']
const SCHEMA = {
  type: 'object',
  properties: { ok: {}, who: {} },
  required: ['ok', 'who'],
  additionalProperties: true,
}
const results = await parallel(
  WRITERS.map((who) => () =>
    agent(
      `RUNTIME PROBE — you are invoked ONLY to confirm the engine resolves your agentType and ` +
        `validates a StructuredOutput call. Ignore your normal ${who} duties; read no files; ` +
        `analyze nothing. Respond ONLY by calling the StructuredOutput tool with exactly this ` +
        `object and nothing else: {"ok": true, "who": "${who}"}.`,
      { agentType: who, label: `d7-${who}`, schema: SCHEMA },
    ).then(
      (r) => ({ who, resolved: true, returned: r }),
      (e) => ({ who, resolved: false, error: String(e && e.message ? e.message : e) }),
    ),
  ),
)
return {
  probe: 'C2D-Phase2-D7-trigger-gated-writers',
  writers: WRITERS,
  parallel_fanout_ok: Array.isArray(results) && results.length === WRITERS.length,
  all_resolved: results.every((r) => r && r.resolved === true),
  results,
}

/*
RESULT — run wf_f3e8d649-a1a (2026-06-28; 4 agents, 4 tool_uses, ~74k subagent tokens, 5.2s):

{
  "probe": "C2D-Phase2-D7-trigger-gated-writers",
  "writers": ["schema-change-executor","fan-out-implementer","test-triage","deep-debugger"],
  "parallel_fanout_ok": true,
  "all_resolved": true,
  "results": [
    {"who":"schema-change-executor","resolved":true,"returned":{"ok":"true","who":"schema-change-executor"}},
    {"who":"fan-out-implementer","resolved":true,"returned":{"ok":"true","who":"fan-out-implementer"}},
    {"who":"test-triage","resolved":true,"returned":{"ok":"true","who":"test-triage"}},
    {"who":"deep-debugger","resolved":true,"returned":{"ok":"true","who":"deep-debugger"}}
  ]
}

All four trigger-gated writers RESOLVED on the real engine and returned a StructuredOutput-validated
object (no 400), confirming the post-PR-1 schema shape is accepted for these agentTypes and the
parallel() fan-out seam runs them concurrently. (`ok` came back as the string "true" — the permissive
{} property schema accepts the agent's literal return, the same behaviour PR 1's smoke confirmed.)
This closes the D7 residual: the harness proved the TRIGGER wiring on synthetic manifests; this proves
the LIVE-ENGINE RUN semantics the harness could not. It does NOT run each writer's full real work (a
probe, by design) — it validates resolution + schema-validation, the gap finding-034 named.
*/

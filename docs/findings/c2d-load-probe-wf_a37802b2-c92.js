export const meta = {
  name: 'c2d-load-probe',
  description: 'C2D-Phase1: confirm the Workflow engine load model before the bulk port',
  phases: [
    { title: 'Load', detail: 'nested phases literal + header-comment bait + top-level await/return' },
    { title: 'Exercise', detail: 'agent+schema, parallel null-on-throw, pipeline, budget, args' },
  ],
}
// Meta-extractor bait: this comment literally contains  export const meta = { name: 'NOT-REAL' }
// to confirm the loader does not false-match a commented occurrence of the dialect head.
log('c2d-probe: load ok — meta parsed, body running under top-level await')
phase('Exercise')

const dispatched = await agent(
  'RUNTIME PROBE — ignore your normal scope-dispatcher duties. Do not read any files. Return exactly the object {"ok": true, "who": "scope-dispatcher"}.',
  {
    agentType: 'scope-dispatcher',
    label: 'probe-agentType-resolution',
    schema: {
      type: 'object',
      properties: { ok: { type: 'boolean' }, who: { type: 'string' } },
      required: ['ok', 'who'],
    },
  },
)

const parResult = await parallel([
  () => Promise.resolve('A'),
  () => Promise.reject(new Error('async rejection — expect null in this slot')),
  () => Promise.resolve('C'),
])

const pipeResult = await pipeline([1, 2, 3], (x) => x * 10, (y) => y + 1)

return {
  probe: 'C2D-Phase1-load-model',
  args_seen: typeof args !== 'undefined' ? args : null,
  load_ok: true,
  agentType_resolved: dispatched,
  parallel_null_on_throw: parResult,
  pipeline_result: pipeResult,
  budget_total: budget && budget.total,
  budget_spent: budget && typeof budget.spent === 'function' ? budget.spent() : null,
}

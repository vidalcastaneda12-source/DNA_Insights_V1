// Suite 11 — fail-closed (rounds 2-3). Coverage backfill for the NEW fail-closed branches the
// completeness-critic flagged as untested after round 1 (HEAD f9ca7f4): the close-segment
// curator cross-check guard, the two Stage-2 writer/sentinel null-guards, the regression-hunter
// safety-lens drop, the BARRIER-mode (wide/independent) lens-drop, the prose-string sentinel
// guard, and the architect-reviewer severity->verdict adapter. Same posture as Suite 10:
// an infra crash (a member returning null) or a malformed return (a prose string) is NOT a
// clean signal and must reduce to RETAIN/ESCALATE/INCOMPLETE — never a silent pass.
//
// from: .claude/agents/knowledge-curator.md (Output `cross_check_passed` + `escalations`;
//       "Cross-check ... a mismatch is an escalation"; success = "anchor loop closed");
//       .claude/agents/test-author.md + plan-adherence-sentinel.md (Stage-2 plan-blind oracle /
//       write-phase monitor — a crashed slot blocks Stage 2); .claude/agents/phi-pii-guardian.md
//       + regression-hunter.md (factor-gated SAFETY lenses — phi-pii on any data/external/config
//       surface; regression-hunter whenever |applicable_anchors| >= 1 — README §"Adaptive depth");
//       .claude/agents/silent-failure-hunter.md (the in-loop guard-pool member, VERDICTLESS_MEMBERS);
//       .claude/agents/architect-reviewer.md + plan-auditor.md (architect is verdict-LESS by
//       contract — emits findings with severity blocker|warn|nit; the merged plan-audit verdict
//       is ready|revise|escalate via mergeAudits' findings-severity ladder); .claude/agents/
//       README.md §"Adaptive depth" (lens-gating by factor; Tier-2 architect-reviewer) +
//       §Usage(b) (close ends at "anchor loop closed"); suite7 (the wide&independent blast_radius
//       fixture shape); round-2 fix list (items 1-7) + round-3 fixes (Item-6 RETARGET to the
//       scanned plan-adherence-sentinel slot; Item-8 verifier-degraded force-escalate isolation;
//       Item-4 gate-spec clarification) per the Stage-3 test-integrity / pr-test-analyzer mutation
//       findings. In-repo engine-semantics substantiation (the C2D-Phase1-* plan files cited by
//       _harness.mjs are session-local, NOT committed): docs/findings/c2d-load-probe-wf_a37802b2-c92.js
//       (the committed load-probe — null-on-async-reject / propagate-on-sync-throw / variadic
//       pipeline(items, ...stages)) + docs/findings/finding-034-agent-team-plan-phase.md (probe appendix).
//
// INTERFACE-LEVEL ONLY: every assertion reads the documented package fields
// (`result.escalations`, `result.audit.verdict`, `result.stage2.ready_for_review`, `route_to`),
// the recorded call graph, and `log()` output — never the workflow's private logic. Per the
// round-2 contract NO assertion pins the exact `escalation_reason` TEXT (an implementer is
// concurrently enriching it); the disposition/route MARKERS (`escalat` / `INCOMPLETE` / `barrier`)
// are verdict-level signals, asserted the same way Suite 10 / Suite 4 assert them.
//
// CRASH MODEL: a crashed member is injected as a stub resolving `null` (the engine's parallel()
// also yields a null slot on async rejection); the harness schema-guard (fix #5) SKIPS null
// returns, so a null models a CRASH the workflow must handle. A malformed (prose-string) return
// into a SCHEMA-BEARING slot (e.g. plan-adherence-sentinel, Item 6) IS recorded as a schema
// violation by the harness — but that is a harness artifact separate from the workflow's own
// `'verdict' in g` typeof-guard, which Item 6 asserts via result.error (a thrown body would land
// there), NOT via schemaViolations; so Item 6 deliberately does not assert on schemaViolations.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PLAN_PHASE, IMPLEMENT_REVIEW, CLOSE, loadWorkflow, countOf, deepFindString } from './_harness.mjs';

// `escalat` appears in a string VALUE (route_to / escalation_reason) when the workflow escalates —
// the same route-level marker Suite 10 / Suite 4 read (NOT the exact reason text).
const escalates = (result) => deepFindString(result, 'escalat');
const logHits = (logs, who, signal) => (logs || []).some((l) => who.test(l) && signal.test(l));

function oneFinding(severity) {
  // The shared lens finding shape (architect-reviewer.md / the *-guardian lenses).
  return {
    id: 'find-1',
    severity,
    where: 'backend/src/genome/stub.py:1',
    claim: 'stub claim',
    evidence: 'stub evidence',
    refutable_claim: 'stub refutable claim',
    suggested_fix: 'stub fix',
    confidence: 0.5,
  };
}

// The "wide & independent" blast_radius that selects the breadth path (suite7's fixture shape:
// large imports_touched + explicit wide/independent/parallelizable flags). It fires
// fan-out-implementer in Stage 2 AND selects the BARRIER lens fan-out in Stage 3.
const WIDE_INDEPENDENT = {
  imports_touched: Array.from({ length: 25 }, (_, i) => `mod_${i}`),
  tests_covering: Array.from({ length: 10 }, (_, i) => `t_${i}`),
  independent: true,
  wide: true,
  parallelizable: true,
};

const CONFIRMED = [{ anchor: 'gnomad_matches', value: 3054426, confirmed_by: 'gate' }];

// A complete knowledge-curator return (curator.md Output) with one field tunable — so the ONLY
// difference from the happy default is the injected field, and the escalation we observe is the
// CLOSE workflow's response to it, not a planted `escalations` entry.
function curatorReturn(over = {}) {
  return { scope_id: 'C2D-Phase1', relocks: [], roadmap_flip: 'PR [ ] -> [x]', cross_links: [], cross_check_passed: true, escalations: [], ...over };
}
const escalationCount = (result) => (result && Array.isArray(result.escalations) ? result.escalations.length : -1);

// ===========================================================================
// Item 1 — close: a curator that did NOT pass its cross-check (or crashed to null) must NOT
// take the success "anchor loop closed" path; it surfaces Stage-5 INCOMPLETE/escalate.
// (knowledge-curator.md: "Cross-check ... a mismatch is an escalation"; the curator writes only
// human-confirmed numbers — an unconfirmed re-lock is not done.)
// ===========================================================================

// from: plan §3 (close: PRESERVE the confirmed-anchors guard) + knowledge-curator.md cross-check;
//       this round's fix #1. The harness default sets cross_check_passed:true — here we inject false.
test('close: knowledge-curator cross_check_passed:false => Stage-5 INCOMPLETE/escalate, NOT the success "anchor loop closed" path', async () => {
  const { result, logs, calls, error } = await loadWorkflow(CLOSE, {
    confirmedAnchors: CONFIRMED,
    returns: { 'knowledge-curator': () => curatorReturn({ cross_check_passed: false }) }, // escalations:[] => the close workflow must add its own
  });
  assert.equal(error, null, 'close errored: ' + (error && error.message));
  assert.ok(countOf(calls, 'knowledge-curator') >= 1, 'non-vacuous: the curator was invoked (confirmed anchors present)');
  // The escalation is the CLOSE workflow's, not the injected curator's (which carried escalations:[]).
  assert.ok(escalationCount(result) >= 1, 'a failed curator cross-check must surface >=1 escalation on the close package — an unconfirmed re-lock is not "done"');
  assert.ok(logHits(logs, /\[close\]/i, /(INCOMPLETE|escalat)/i), 'close must log() the Stage-5 INCOMPLETE/escalate disposition so the unconfirmed re-lock is observable');
  // The success "anchor loop closed" terminal must NOT be claimed when the cross-check failed.
  assert.equal(logHits(logs, /\[close\]/i, /anchor loop closed/i), false, 'close must NOT log the success "anchor loop closed" completion when the curator cross-check failed (fail closed)');
});

// from: knowledge-curator.md ("If a confirmed number is missing, escalate; do not guess") +
//       Suite 10 crash model; this round's fix #1 (the "separately, null curator" sub-case).
test('close: a NULL knowledge-curator (crashed writer) => Stage-5 INCOMPLETE/escalate, NOT success', async () => {
  const { result, logs, calls, error } = await loadWorkflow(CLOSE, {
    confirmedAnchors: CONFIRMED,
    returns: { 'knowledge-curator': () => null },
  });
  assert.equal(error, null, 'close errored: ' + (error && error.message));
  assert.ok(countOf(calls, 'knowledge-curator') >= 1, 'non-vacuous: the curator slot was invoked');
  assert.ok(escalationCount(result) >= 1, 'a crashed (null) curator must surface >=1 escalation — a crash is not a confirmed re-lock');
  assert.ok(logHits(logs, /\[close\]/i, /(INCOMPLETE|escalat)/i), 'close must log() the Stage-5 INCOMPLETE/escalate disposition on a crashed curator');
  assert.equal(logHits(logs, /\[close\]/i, /anchor loop closed/i), false, 'a crashed curator must NOT yield the success "anchor loop closed" completion');
});

// CONTROL (discrimination): the happy curator DOES take the success path and raises no escalation —
// proving the two assertions above distinguish success from failure, not always-true.
// from: knowledge-curator.md success terminal ("anchor loop closed") + harness default.
test('close CONTROL: a passing curator takes the success "anchor loop closed" path with zero escalations', async () => {
  const { result, logs, error } = await loadWorkflow(CLOSE, { confirmedAnchors: CONFIRMED });
  assert.equal(error, null, 'close errored: ' + (error && error.message));
  assert.equal(escalationCount(result), 0, 'the happy close path raises no escalation');
  assert.ok(logHits(logs, /\[close\]/i, /anchor loop closed/i), 'the happy close path logs the success "anchor loop closed" completion');
  assert.equal(logHits(logs, /\[close\]/i, /INCOMPLETE/i), false, 'the happy close path does not log Stage-5 INCOMPLETE');
});

// ===========================================================================
// Item 2 — Stage-2 plan-blind test-author writer slot returns null (the blind oracle crashed):
// Stage 2 must BLOCK and ESCALATE, never proceed to a clean go-handoff with blind tests unverified.
// ===========================================================================

// from: test-author.md (the plan-blind §5 oracle — "tests start red; the implementer drives green");
//       README §Stage 2; this round's fix #2.
test('implement-review: a NULL test-author (plan-blind oracle crashed) blocks Stage 2 and escalates (no clean go-handoff)', async () => {
  const { result, calls, error } = await loadWorkflow(IMPLEMENT_REVIEW, {
    tier: 2,
    deepT2: true,
    synthVerdict: 'go', // even on an otherwise-go path the Stage-2 block must override
    returns: { 'test-author': () => null },
  });
  assert.equal(error, null, 'implement-review errored: ' + (error && error.message));
  assert.ok(countOf(calls, 'test-author') >= 1, 'non-vacuous: the test-author slot was invoked');
  assert.ok(escalates(result), 'a crashed plan-blind test-author must ESCALATE (route_to a VSC-User escalation) — blind tests cannot be assumed green when the oracle crashed');
  assert.equal(countOf(calls, 'handoff-assembler'), 0, 'a blocked Stage 2 must NOT reach the Stage-4 go-handoff');
  assert.equal(result && result.stage2 && result.stage2.ready_for_review, false, 'Stage 2 must report ready_for_review:false (blocked) when the blind oracle crashed');
});

// ===========================================================================
// Item 3 — Stage-2 plan-adherence-sentinel slot returns null (the write-phase monitor crashed):
// Stage 2 must BLOCK and ESCALATE — a missing drift verdict is not "on-rails".
// ===========================================================================

// from: plan-adherence-sentinel.md (verdict on-rails|escalate; "drift is not worked around");
//       README §Stage 2; this round's fix #3.
test('implement-review: a NULL plan-adherence-sentinel (no usable verdict) blocks Stage 2 and escalates', async () => {
  const { result, calls, error } = await loadWorkflow(IMPLEMENT_REVIEW, {
    tier: 2,
    deepT2: true,
    synthVerdict: 'go',
    returns: { 'plan-adherence-sentinel': () => null },
  });
  assert.equal(error, null, 'implement-review errored: ' + (error && error.message));
  assert.ok(countOf(calls, 'plan-adherence-sentinel') >= 1, 'non-vacuous: the sentinel slot was invoked');
  assert.ok(escalates(result), 'a crashed sentinel must ESCALATE — a missing drift verdict must not be read as on-rails (fail closed)');
  assert.equal(countOf(calls, 'handoff-assembler'), 0, 'a blocked Stage 2 (crashed sentinel) must NOT reach the Stage-4 go-handoff');
  assert.equal(result && result.stage2 && result.stage2.ready_for_review, false, 'Stage 2 must report ready_for_review:false when the sentinel produced no usable verdict');
});

// ===========================================================================
// Item 4 — factor-gated SAFETY-lens drop (PIPELINE mode), parametrized over BOTH gated safety
// lenses: a null phi-pii-guardian (data/privacy surface) AND a null regression-hunter
// (|applicable_anchors| >= 1). Each must escalate the review and log() the drop. Round 1 only
// covered phi-pii-guardian; regression-hunter is the gap.
//
// GATE NOTE (round-3 nit fix): the ACTUAL gate in this harness is the explicit `reviewLenses`
// override — `stage3Lenses()` takes the override branch whenever `manifest.review_lenses` is
// present (and makeManifest always sets it), so `changeClass`/`applicableAnchors` here have NO
// mechanical effect (verified: changeClass:['pipeline'] WITHOUT reviewLenses invokes neither lens).
// They are kept only as `productionContext` — documenting the real-world factor that gates each
// lens in production (phi-pii on a data/external/config surface; regression-hunter on anchors>=1).
// The `countOf(calls, lens) >= 1` assertion is what proves the lens was actually invoked.
// ===========================================================================
const SAFETY_LENS_CASES = [
  { lens: 'phi-pii-guardian', why: 'data/external/config surface', productionContext: { changeClass: ['cli', 'annotation'] } },
  {
    lens: 'regression-hunter',
    why: '|applicable_anchors| >= 1',
    productionContext: { changeClass: ['cli', 'annotation'], applicableAnchors: [{ name: 'gnomad_matches', value: 3054426, src: 'CLAUDE.md:obs-4' }] },
  },
];
for (const { lens, why, productionContext } of SAFETY_LENS_CASES) {
  // from: phi-pii-guardian.md / regression-hunter.md (factor-gated SAFETY lenses); Suite 10 case (b)
  //       parametrized to BOTH lenses; this round's fix #4.
  test(`implement-review [pipeline]: a dropped (null) SAFETY lens ${lens} (production gate: ${why}; harness gate: explicit reviewLenses) escalates the review and log()s the drop`, async () => {
    const { result, logs, calls, error } = await loadWorkflow(IMPLEMENT_REVIEW, {
      tier: 2,
      deepT2: true,
      reviewLenses: ['convention-compliance', lens], // the ACTUAL gate in this harness
      returns: { [lens]: () => null },
      ...productionContext, // documents the production factor; no mechanical effect under the override branch
    });
    assert.equal(error, null, 'implement-review errored: ' + (error && error.message));
    assert.ok(countOf(calls, lens) >= 1, `non-vacuous: ${lens} must be gated ON (via reviewLenses) and invoked, else the drop is untested`);
    assert.ok(escalates(result), `a dropped (null) factor-gated SAFETY lens (${lens}) must escalate — a safety lens cannot be assumed-clean when it crashed`);
    assert.ok(
      logHits(logs, new RegExp(lens, 'i'), /(drop|null|escalat|crash|fail|degrad)/i),
      `the dropped safety lens (${lens}) must be log()-ged (which lens, and that it was dropped) so the gap is observable`,
    );
  });
}

// ===========================================================================
// Item 5 — BARRIER-mode lens-drop. The lens fan-out has two modes; Suite 10 / Item 4 exercised
// the PIPELINE mode. A wide/independent blast_radius selects the BARRIER mode — a dropped safety
// lens must STILL escalate + log there too (fail-closed holds in BOTH fan-out modes).
// ===========================================================================

// from: README §"Adaptive depth" (the breadth path) + suite7's wide&independent fixture; this
//       round's fix #5. The barrier-vs-pipeline mode log is the discriminator that makes this
//       case distinct from the Item-4 / Suite-10 pipeline-mode drop.
test('implement-review [barrier]: a wide/independent blast_radius selects BARRIER fan-out; a dropped SAFETY lens still escalates + logs', async () => {
  const { result, logs, calls, error } = await loadWorkflow(IMPLEMENT_REVIEW, {
    tier: 2,
    deepT2: true,
    changeClass: ['cli', 'annotation'],
    blastRadius: WIDE_INDEPENDENT,
    reviewLenses: ['convention-compliance', 'phi-pii-guardian'],
    returns: { 'phi-pii-guardian': () => null },
  });
  assert.equal(error, null, 'implement-review errored: ' + (error && error.message));
  assert.ok(countOf(calls, 'phi-pii-guardian') >= 1, 'non-vacuous: the gated safety lens was invoked under the wide/independent path');
  // DISTINCT-FROM-PIPELINE proof: the wide/independent blast_radius must select the BARRIER mode.
  assert.ok(logHits(logs, /Stage 3/i, /barrier/i), 'a wide/independent blast_radius must select the BARRIER lens fan-out (the log names the mode) — not the pipeline mode Suite 10 exercised');
  assert.ok(escalates(result), 'a dropped (null) safety lens must escalate in BARRIER mode too — the fail-closed guard cannot be mode-specific');
  assert.ok(logHits(logs, /phi-pii-guardian/i, /(drop|null|escalat|crash|fail|degrad)/i), 'the dropped safety lens must be log()-ged in barrier mode');
});

// ===========================================================================
// Item 6 — prose-string sentinel guard. A guard-pool member returns a PROSE STRING (a TRUTHY
// non-object). The workflow's guard-pool extraction does `'verdict' in g`; without a
// `typeof g === 'object'` guard that throws `Cannot use 'in' operator … in <prose>`. The slot the
// scan actually reads is the plan-adherence-sentinel slot, so the prose must be injected THERE.
//
// ROUND-3 RETARGET (test-integrity mutation finding — round 2 was VACUOUS): round 2 injected the
// prose into silent-failure-hunter, but that slot is read by INDEX and never reaches a `'verdict'
// in g` scan, so the run stayed error===null with OR without the guard (proven by reverting to the
// unguarded pool-scan AND dropping the typeof guard — neither reddened). The discriminating
// injection is into plan-adherence-sentinel, where the probe shows the REAL workflow holds
// (error===null) but a regressed one THROWS. Note a prose sentinel return ALSO carries no usable
// verdict, so the workflow correctly FAIL-CLOSES to escalate (the guard lets it REACH that decision
// instead of crashing) — distinct from Item 3's NULL sentinel, where the `g &&` short-circuit (null
// is falsy) already handles it; only a TRUTHY non-object exercises the `typeof === 'object'` clause.
// ===========================================================================

// from: plan-adherence-sentinel.md (the slot the workflow's `'verdict' in g` scan reads) + the
//       harness loadWorkflow try/catch (a thrown workflow body -> result.error) substantiated by
//       docs/findings/c2d-load-probe-wf_a37802b2-c92.js; this round's fix #6 (RETARGETED in round 3).
test('implement-review: a PROSE-STRING plan-adherence-sentinel return does NOT crash the `\'verdict\' in g` scan (typeof guard); it resolves to a graceful fail-closed escalate', async () => {
  const { result, calls, error } = await loadWorkflow(IMPLEMENT_REVIEW, {
    tier: 2,
    deepT2: true,
    synthVerdict: 'go',
    // Inject the prose into the SCANNED slot. A TRUTHY non-object is what exercises the
    // `typeof === 'object'` clause guarding `'verdict' in g` (null alone is handled by `g &&`).
    returns: { 'plan-adherence-sentinel': () => 'PROSE: a sentinel narrative, not an object' },
  });
  // PRIMARY (discriminating): reverting to the unguarded pool-scan (`guardOut.find(g => g &&
  // 'verdict' in g)`) or dropping `typeof === 'object'` makes `'verdict' in <prose>` THROW; the
  // harness captures that into result.error, so THIS line reddens. (Mutation-verified round 3.)
  assert.equal(error, null, 'a prose (truthy non-object) sentinel return must NOT crash the `\'verdict\' in g` scan — the `typeof === \'object\'` guard must hold: ' + (error && error.message));
  assert.ok(result && typeof result === 'object', 'the workflow must still return its package (the scan resolved, did not throw)');
  assert.ok(countOf(calls, 'plan-adherence-sentinel') >= 1, 'non-vacuous: the prose actually reached the scanned sentinel slot');
  // A prose sentinel return has no usable verdict, so the workflow fail-closes to escalate (never a
  // silent go) — the guard enabled a graceful decision rather than a crash.
  assert.ok(escalates(result), 'a sentinel return with no usable verdict must fail closed to escalate (not a silent go) — the guard enabled a graceful decision, not a crash');
  assert.equal(countOf(calls, 'handoff-assembler'), 0, 'the prose (unusable-verdict) sentinel must NOT reach a clean Stage-4 go-handoff');
});

// ===========================================================================
// Item 7 — EF-3 architect-reviewer severity->verdict adapter. architect-reviewer is verdict-LESS by
// contract (emits only findings with severity blocker|warn|nit). A BLOCKER architect finding must
// move the MERGED plan-audit verdict OFF `ready` (to revise/escalate) via mergeAudits' findings-
// severity ladder — DISTINCT from the verdict-less-CRASH (null) fail-closed path already tested.
// ===========================================================================

// from: architect-reviewer.md (verdict-LESS; findings severity blocker|warn|nit) + plan-auditor.md
//       (merged verdict ready|revise|escalate) + suite2 ("derive the verdict in mergeAudits");
//       README §"Adaptive depth" (Tier-2 distinct architect-reviewer); this round's fix #7.
test('plan-phase: an architect-reviewer BLOCKER finding (verdict-less member) moves the merged plan-audit verdict OFF ready', async () => {
  // CONTROL: auditor ready + premortem proceed + NO architect finding => merged verdict ready.
  // This isolates the architect blocker finding as the ONLY thing that can move the verdict.
  const ctrl = await loadWorkflow(PLAN_PHASE, { tier: 2, deepT2: false, auditorVerdict: 'ready', premortemRecommend: 'proceed' });
  assert.equal(ctrl.error, null, 'plan-phase (control) errored: ' + (ctrl.error && ctrl.error.message));
  assert.ok(countOf(ctrl.calls, 'architect-reviewer') >= 1, 'non-vacuous: the Tier-2 architect-reviewer ran (else the adapter is untested)');
  assert.equal(ctrl.result && ctrl.result.audit && ctrl.result.audit.verdict, 'ready', 'control: with no architect blocker, the merged audit verdict is ready');

  // ADAPTER: same inputs + a BLOCKER architect finding => the severity ladder moves the verdict off ready.
  const withBlocker = await loadWorkflow(PLAN_PHASE, {
    tier: 2,
    deepT2: false,
    auditorVerdict: 'ready',
    premortemRecommend: 'proceed',
    architectFindings: [oneFinding('blocker')],
  });
  assert.equal(withBlocker.error, null, 'plan-phase (architect blocker) errored: ' + (withBlocker.error && withBlocker.error.message));
  assert.ok(countOf(withBlocker.calls, 'architect-reviewer') >= 1, 'non-vacuous: the architect-reviewer ran and carried the blocker finding');
  const verdict = withBlocker.result && withBlocker.result.audit && withBlocker.result.audit.verdict;
  assert.notEqual(verdict, 'ready', 'an architect-reviewer BLOCKER finding must force the merged audit verdict OFF ready (mergeAudits findings-severity ladder), even though architect-reviewer emits no verdict of its own');
  assert.ok(['revise', 'escalate'].includes(verdict), `the moved verdict must be a real ladder value ('revise'|'escalate'), got ${JSON.stringify(verdict)}`);
});

// ===========================================================================
// Item 8 (round 3) — discriminating verifier_degraded -> force_escalate. Suite 10 case (a) does NOT
// ISOLATE this guard: with a killed/empty finding (survives=false, votes=[]) the harness
// deriveSynthVerdict standing-survivor fallback routes to fix-first and the ×2 cap escalates anyway,
// so `retained || escalated` passes WITH or WITHOUT the guard. The guard's real job — overriding a
// synthesizer 'go' on a crashed-verifier blocker (refute-by-default: a crash is not a refutation) —
// is never exercised there. Forcing the SYNTHESIZER verdict to 'go' bypasses the stub fallback, so
// the ONLY thing that can move the route off 'go' is the verifier_degraded -> forceEscalate guard.
// ===========================================================================

// from: finding-verifier.md (refute-by-default — a null skeptic is NOT a refutation) +
//       review-synthesizer.md (the go/fix-first verdict the guard must override); this round's fix #2.
test('implement-review: a CRASHED finding-verifier (null) on a blocker OVERRIDES a synthesizer go => force-escalate (no handoff)', async () => {
  const { result, calls, error } = await loadWorkflow(IMPLEMENT_REVIEW, {
    tier: 2,
    deepT2: true,
    reviewLenses: ['convention-compliance'],
    lensFindings: { 'convention-compliance': [oneFinding('blocker')] },
    findingSeverity: 'blocker',
    synthVerdict: 'go', // bypass the stub deriveSynthVerdict fallback so ONLY the guard can move the route off go
    returns: { 'finding-verifier': () => null }, // every skeptic crashes -> degraded verification
  });
  assert.equal(error, null, 'implement-review errored: ' + (error && error.message));
  assert.ok(countOf(calls, 'finding-verifier') >= 1, 'non-vacuous: the blocker was actually sent to the (crashing) verifier');
  // The degraded-verifier guard must OVERRIDE the synthesizer go and force escalate. Removing the
  // verifier_degraded -> forceEscalate lines lets synthVerdict:'go' route straight to the handoff,
  // reddening BOTH assertions below. (Mutation-verified round 3.)
  assert.ok(escalates(result), 'a crashed verifier on a blocker must force-escalate even when the synthesizer said go (refute-by-default: a crash is not a refutation)');
  assert.equal(countOf(calls, 'handoff-assembler'), 0, 'a degraded-verifier force-escalate must NOT reach the Stage-4 go-handoff');
});

// CONTROL (isolates the guard as the cause): same synthVerdict:'go', but a HEALTHY verifier that
// refutes the blocker -> with no verifier degradation the synthesizer go DOES reach the handoff.
// This proves the escalate above is caused by the verifier CRASH, not by synthVerdict or the blocker.
test('implement-review CONTROL: synthesizer go + a HEALTHY verifier reaches the go-handoff (no force-escalate)', async () => {
  const { result, calls, error } = await loadWorkflow(IMPLEMENT_REVIEW, {
    tier: 2,
    deepT2: true,
    reviewLenses: ['convention-compliance'],
    lensFindings: { 'convention-compliance': [oneFinding('blocker')] },
    findingSeverity: 'blocker',
    synthVerdict: 'go',
    verifierDefaultRefuted: true, // healthy verifier refutes the blocker -> killed -> go, no degradation
  });
  assert.equal(error, null, 'implement-review errored: ' + (error && error.message));
  assert.equal(escalates(result), false, 'with a healthy verifier and synth go, there is no degradation to force-escalate');
  assert.ok(countOf(calls, 'handoff-assembler') >= 1, 'synth go + healthy verifier reaches the Stage-4 go-handoff — proving the force-escalate case is caused by the verifier crash, not by synthVerdict');
});

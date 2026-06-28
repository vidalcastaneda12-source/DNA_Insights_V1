---
type: decision
status: active
actors: [VSC-User, ClaudeCodeDevelopment]
date: 2026-06-27
supersedes: []
superseded_by: []
---
# Finding 041 — Campaign orchestrator (`genome.campaign`, Sub Project B2 Phase 2)

## Status

**Active (2026-06-27).** Adopted at Gate 1 for Sub Project B2, Phase 2 — the capstone of the
`/scope-run` enhancement sub-projects and the largest of them (a brand-new module). This is the
durable provenance anchor for the `genome.campaign` core, the `genome campaign` sub-app, the
`/campaign` skill, the campaign's reuse of the `scope_split` append-only ROADMAP managed block,
and the ledger row `DEC-0120`. Delivered in **two PRs**:

- **PR 1 (this) — the DB-free campaign core + advisory CLI.** The state machine, the
  supersession ledger, the adaptive re-validation reducers, the append-only JSONL persistence, the
  ROADMAP reflection, and the `start / dry-run / status / resume / cancel / write-roadmap` CLI.
  **Ships with NO live launch** — it sequences, tracks, tees up, and reflects, but never runs a
  sub-scope and never crosses a human gate.
- **PR 2 — the live-launch wiring** (deferred): drive the readied sub-scopes through the
  model-driven `/scope-run` conductor and record the human-gate events back onto the ledger. The
  reducers (`advance_on_merge`, `apply_revalidation`) are present + unit-tested in PR 1; only the
  conductor wiring is deferred.

The synthesized plan artifact is transient (plans get pruned — see `DEC-0084`); this finding is
where the design rationale lives.

## Related findings

- [`finding-039`](finding-039-scope-split-smart-cut.md) (Sub Project B2 Phase 1 — the scope-split
  smart-cut detector) — Phase 2 is the **consumer** of Phase 1: it sequences the sub-scopes a
  non-atomic `propose_split` cut proposes. It reuses `SplitResult` / `SubScope` / `propose_split` /
  `append_roadmap_block` / `MAX_RESPLIT_DEPTH` / `make_coupling_builder` / the coupling engines
  directly (no re-implementation). finding-039's LIFECYCLE note already named this: "Phase 2
  `genome.campaign` is an insert-then-flip supersession, never an in-place edit".
- [`finding-040`](finding-040-cross-run-learning-calibration.md) (Sub Project C1 — calibration) and
  [`finding-038`](finding-038-fast-follow-drain-loop.md) (Sub Project B — fast-follow) — the
  campaign is the **fourth instance** of their shared DB-free-core + JSON-seam + hard-coded
  `data/...` path + fail-closed-reducer shape; `persistence.py` mirrors `calibration.persistence`
  /`fast_follow.persistence` (`open("a")`, never-truncate, empty-on-absent, malformed-raises).
- [`finding-037`](finding-037-agentic-verify-merge-gate.md) (Sub Project A — the verify-merge gate)
  — the campaign's **Gate 2** (`implementing → merged`) is exactly the `/verify-and-merge` event;
  the same fail-closed-reducer discipline carries here (an undecidable transition raises, never a
  fabricated advance).
- [`finding-034`](finding-034-agent-team-plan-phase.md) (the per-scope agent team / `/scope-run`)
  — the PR-2 live launch targets that team's conductor. The model-driven→engine-primary reversal
  (finding-034 Amendment / `DEC-0099`) is why PR 2 wires to the **model-driven** `/scope-run`
  conductor now, with the engine-primary path deferred to **Sub Project C2+D Phase 2**.

## Context

`/scope-run` runs one ROADMAP scope at a time, and B2 Phase 1 added a detector that decides whether
a scope is really several independently-shippable sub-scopes. But nothing **sequences** those
sub-scopes: a human has to remember the order, run each `/scope-run` by hand, and track which have
merged across many sessions. Phase 2 adds the orchestrator — a persistent, resumable campaign that
seeds the cut, tees up the dependency-free head, and tracks each sub-scope through the two human
gates to `merged` (or off-ramps it to `moot` / `ejected`).

The governing constraint is the same fail-closed posture as its siblings, applied to a new
surface: the orchestrator must **never** cross a human gate on its own (plan approval; merge
verification stay human), must **never** clobber the hand-authored ROADMAP, and must survive being
stopped and resumed in a later session without ever presenting a torn state.

## The finding itself

### D1 — Supersession-as-runtime-state (the locked-#7 application)

The campaign applies **locked decision #7 (supersession over update)** to its *own* runtime state.
Every status change is an INSERT of a new immutable `SubScopeState` (`record_seq` = prior max + 1,
`supersedes` = the prior active record's seq) appended to a per-campaign JSONL ledger; the prior
record's bytes are never rewritten. The **current view** is *derived* — `reduce_current` projects
the ledger to the highest-`record_seq` record per `sub_scope_id` — not a stored `is_active` flag.
`CampaignState.__post_init__` rejects any torn >1-active view at construction. This is the
row-grain supersession mechanism of decision #7, realized in a markdown-free substrate. (`DEC-0120`
anchor.)

### D2 — The `CampaignStatus` state machine + the symmetric gate guard

`pending → ready → planning → implementing → merged` is the non-terminal path; `moot`
(re-validated away) and `ejected` (re-split past the cap, or cancelled) are the terminal
off-ramps. `LEGAL_TRANSITIONS` is the closed legal-edge map (terminals map to the empty set);
`transition` rejects any edge not in it. The two human-gate crossings — `planning → implementing`
(Gate 1, plan approval) and `implementing → merged` (Gate 2, `/verify-and-merge`) — are in
`GATE_CROSSINGS` and require `external_event=True`; the campaign's autonomous reducers
(`tee_up`, `cancel_campaign`, `apply_revalidation`) can never emit one. **Gate-1 refinement A made
this symmetric** — both gates are external-gated, not just the merge gate — so the orchestrator
provably sequences and tees up but crosses neither gate itself.

### D3 — DB-free core + thin persistence shell

`model.py` + `state_machine.py` are pure (no I/O); all persistence is isolated in `persistence.py`
(hard-coded `Path("data/campaign")`, no `genome.db`, no `get_settings`), and all user-facing
concerns in `cli.py` / `formatter.py`. The DB-free / no-settings guarantee is carried by the
package-local clean-subprocess test (`test_campaign_no_db_import.py`), mirroring the verified
`scope_split` / `fast_follow` / `calibration` precedents.

### D4 — Adaptive re-validation, capped at one re-split

`apply_revalidation` re-dispatches a `ready` sub-scope immediately before it runs:
`still_needed → planning`, `moot → moot` (resolving the dependency for dependents), `changed →`
stays `ready` with a fresh `manifest_snapshot` (a content-only supersession), `grown →` re-split
into shell-supplied children at `resplit_depth + 1`. The re-split is capped at **one level**
(`MAX_RESPLIT_DEPTH`, reused from `scope_split`): at the cap, or with no children produced, the
sub-scope is **ejected with a loud, human-readable escalation note** (Gate-1 refinement B — eject
fails loud, the note visible in `format_campaign_status` and the ROADMAP block). Re-validation is a
`ready`-stage gate: every verdict requires a `ready` current record, so a stale `changed` verdict
can never resurrect a terminal sub-scope (the `_supersede` fast path is fenced by that
precondition — see the Stage-3 fold-ins below).

### D5 — ROADMAP reflection reuses the single B2-SUBSCOPES writer

The campaign reflects its live state into the **same** `<!-- B2-SUBSCOPES:BEGIN/END -->` managed
region that `scope-split write-roadmap` owns, through the **reused**
`scope_split.roadmap_writer.append_roadmap_block` (a clobber-guarded region-replace). The campaign
adds only a block-body renderer (`format_campaign_roadmap_block`), not a second writer or a second
region. Once `campaign start` runs, the campaign is the authoritative writer of that region (a late
`scope-split write-roadmap` would revert the live statuses to bare proposed slots — recoverable by
the next `campaign write-roadmap`).

### D6 — Engine-targeting currency (the deferred-launch decision)

PR 2's live launch targets the **model-driven `/scope-run` conductor**, per the
finding-034 / `DEC-0099` model-driven→engine-primary reversal: the engine-primary deterministic-JS
path is deferred to **Sub Project C2+D Phase 2**, so wiring the campaign to the engine now would
target a surface that is itself mid-migration. PR 1 ships **no** launch at all — the reducers and
persistence are complete and tested; only the conductor wiring is deferred — which keeps the
core's DB-free correctness independent of the still-moving engine boundary.

## Stage-3 review fold-ins (PR 1)

The per-scope team's Stage-3 read-only review surfaced two correctness gaps in the first cut, both
folded into PR 1 (each with a regression test):

- **`start` re-run guard** — `campaign start` on an already-seeded campaign would have appended a
  second `0..N-1` `record_seq` run, tearing the append-only monotonic-seq invariant. It now fails
  closed (a clean `BadParameter` pointing at `status` / `resume` / `cancel`).
- **Re-validation `ready` precondition** — the `changed` branch builds via `_supersede` (bypassing
  `transition`'s legality guard), so without a precondition a stale `changed` verdict could
  resurrect a terminal (e.g. `merged`) sub-scope. `apply_revalidation` now rejects any non-`ready`
  current record (D4).

## Consequences / follow-ups

- **PR 2** wires `advance_on_merge` / `apply_revalidation` from the `/scope-run` conductor and
  records the human-gate events onto the ledger; it ticks the ROADMAP "Sub Project B2 — Phase 2"
  slot and deferred-followup item 7. Until then the CLI is advisory only.
- **Engine-primary launch** is **Sub Project C2+D Phase 2** (D6), gated behind the Python-CLI
  reversal-gate.
- **`apply_revalidation` type-tightening** (binding each `RevalidationDecision` to its legal kwargs
  via `@overload` or a discriminated union) is a deferred design-quality nit — the runtime
  precondition (D4) already closes the correctness hole.
- This is a **design / DB-free** finding: it locks **no** real-data anchors and has no bedrock
  anchor table — the campaign core is pure-Python and touches neither DB. Its regression signal is
  the campaign test suite (DB-free, deterministic), not a tolerance-banded real-data number.

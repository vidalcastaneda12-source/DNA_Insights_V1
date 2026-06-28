---
type: decision
status: active
actors: [VSC-User, ClaudeCodeDevelopment]
date: 2026-06-28
supersedes: []
superseded_by: []
---
# Finding 041 — Campaign orchestrator (`genome.campaign`, Sub Project B2 Phase 2)

## Status

**Active (2026-06-27).** Adopted at Gate 1 for Sub Project B2, Phase 2 — the capstone of the
`/scope-run` enhancement sub-projects and the largest of them (a brand-new module). This is the
durable provenance anchor for the `genome.campaign` core, the `genome campaign` sub-app, the
`/campaign` skill, the campaign's reuse of the `scope_split` append-only ROADMAP managed block,
and the ledger rows `DEC-0120` / `DEC-0121`. Delivered in **two PRs**:

- **PR 1 (this) — the DB-free campaign core + advisory CLI.** The state machine, the
  supersession ledger, the adaptive re-validation reducers, the append-only JSONL persistence, the
  ROADMAP reflection, and the `start / dry-run / status / resume / cancel / write-roadmap` CLI.
  **Ships with NO live launch** — it sequences, tracks, tees up, and reflects, but never runs a
  sub-scope and never crosses a human gate.
- **PR 2 — the live-launch wiring** (delivered, `DEC-0121`): drives the readied sub-scopes through
  the model-driven `/scope-run` conductor and records the human-gate events back onto the ledger.
  The reducers (`advance_on_merge`, `apply_revalidation`) were present + unit-tested in PR 1; PR 2
  CLI-wires them as the `revalidate` / `approve-plan` / `record-merge` / `show` commands plus the
  new `/campaign-run` conductor. See "PR 2 — live-launch as-built".

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

## PR 2 — live-launch as-built

PR 2 (`DEC-0121`) is the capstone completing Sub Project B2: it CLI-wires the PR-1 reducers as
human-gate-event-recording commands and adds a model-driven conductor. The DB-free core
(`model.py` / `state_machine.py` / `persistence.py` / `formatter.py`) stays **byte-frozen** — every
new line is in `cli.py` plus the skill markdown, and the `test_campaign_no_db_import.py` guard still
holds.

### The four `genome campaign` commands

- **`revalidate --sub-scope --decision {still_needed|moot|changed|grown}`** — autonomous (no verdict
  is a gate crossing). It dispatches `apply_revalidation` (`still_needed → planning`, `moot → moot`,
  `changed →` a fresh `manifest_snapshot`, `grown →` re-split into shell-supplied children), then
  **bundles `tee_up`** over the post-verdict history in one append, so a `moot` unblocks its
  dependents and a `grown` readies its deps-free children. `changed` / `grown` REQUIRE `--manifest`
  (absent → clean `BadParameter`); `grown` re-runs `propose_split` and feeds `result.sub_scopes` as
  the children (atomic / no-resplit → the core's eject-loud fires).
- **`approve-plan --sub-scope --approved`** — Gate 1. Maps `--approved` straight to
  `transition(..., IMPLEMENTING, external_event=approved)`. The **core is the single enforcer**:
  `planning → implementing ∈ GATE_CROSSINGS`, so a missing `--approved` (→ `external_event=False`)
  makes the core refuse → clean `BadParameter`, no write.
- **`record-merge --sub-scope --merged`** — Gate 2. Reuses `advance_on_merge` (sets
  `external_event=True` internally and tees up the next dependent in one atomic batch); journals the
  merge `/verify-and-merge` already performed under its typed token.
- **`show --sub-scope [--json]`** — read-only; dumps the active record's `manifest_snapshot` plus
  status / deps / origin. `--json` is the conductor's machine seam (GAP-A, below). No write, no
  ROADMAP touch.

Every event flows through one `_apply_event` helper: load the ledger fresh → build records (any core
`ValueError` → `typer.BadParameter`) → append → re-derive → reflect ROADMAP. The `BadParameter` is
raised **before** the append, so a rejected command leaves the ledger **byte-unchanged** — the
no-autonomous-gate guarantee, observable on disk.

### DECISION 1 — separate verb-commands; each gate command requires an explicit flag

The four are separate verb-commands, not one `advance --event`. The gate flag is **type-local** (a
gate command carries `--approved` / `--merged` in its own signature; the autonomous `revalidate` has
no such flag), so there is no shared dispatch branch to get wrong and the audit trail is one
command = one recorded event. A gate command **without** its confirmation flag rejects with a clean
`BadParameter` and never writes — a gate is never crossed autonomously.

### DECISION 2 — a new `/campaign-run` model-driven conductor (not an extension of `/campaign`)

The live loop is a **new** `.claude/commands/campaign-run.md` skill, paralleling `/scope-run`, not an
extension of the advisory `/campaign`. It is a markdown procedure riding the Task tool (no new
Python): `resume` (fresh from disk) → `revalidate` the next-ready sub-scope → on `still_needed` run
`/scope-run` Stage 1 → STOP at Gate 1 and present the plan → on the human's explicit approval
`approve-plan --approved` → `/scope-run` Stages 2–4 → STOP at Gate 2 = `/verify-and-merge` (its typed
token) → on the ACTUAL merge `record-merge --merged` → `/scope-run` Stage 5 close → loop. Its
headline invariant: **the flag is the HUMAN's act** — present gate evidence and STOP; never supply
`--approved` / `--merged` itself; the CLI rejection is the backstop. It targets the model-driven
conductor per `DEC-0099`; the engine-primary path stays deferred to Sub Project C2+D Phase 2 (D6).

### The Gate-2 enforcement asymmetry (GAP-C)

The two gates are enforced in **different layers**. `approve-plan` lets the CORE enforce: it passes
`external_event=approved` straight through and `transition` rejects a non-external
`planning → implementing`. But `advance_on_merge` **hard-codes `external_event=True` internally**, so
the core cannot distinguish an operator-confirmed merge from an autonomous one — therefore the CLI
`if not merged: raise BadParameter` is the **SOLE** structural enforcer of Gate 2. This asymmetry is
called out in the command's docstring + an inline comment, and the headline test proves it
(`record-merge` without `--merged` → exit ≠ 0, ledger byte-unchanged).

### The manifest handoff (GAP-A)

A campaign sub-scope has a `manifest_snapshot` (the scope_split mini-manifest) but **no ROADMAP
slot**. The conductor bridges that gap by feeding the sub-scope's `manifest_snapshot` — read via
`genome campaign show --sub-scope <id> --json` — to `/scope-run` as its Stage-0 manifest. The
placeholder `<origin>-sN` stays the campaign key; minting a real `PR-N` id for the `/scope-run` run
remains the human's micro-gate call (finding-039).

### Fold dispositions

- **Folded (CLI-boundary guard)** — the `grown` path rejects (clean `BadParameter`, no write) a
  re-split child whose `sub_scope_id` collides with an active member, or whose `depends_on` names an
  id outside `{existing ids} ∪ {sibling new child ids}` — the one footgun live `grown` introduces (a
  dangling dep would make `_deps_satisfied` block forever, violating fail-loud). The pure core is
  untouched.
- **Deferred** — `apply_revalidation` decision↔kwargs type-tightening (`@overload` / discriminated
  union; the runtime `ready` precondition of D4 already closes the correctness hole), `from_json`
  `resplit_depth` validation, and the `SubScopeStateJSON` `Literal` / bounds tightening — all
  cosmetic on the frozen core.

### Gate-1 authorization — as taken

VSC-User selected the **plain `--approved` flag** (the recommended option): Gate 1 is authorized by
the explicit `--approved` flag now, and a Gate-1 **fail-closed token core** (mirroring Sub Project
A's `verify_gate`) is **DEFERRED**. The flag-without-token approach suffices because the core already
refuses the crossing absent `external_event`; the token core is future hardening, not a correctness
gap.

## Consequences / follow-ups

- **PR 2 (delivered, `DEC-0121`)** CLI-wired `advance_on_merge` / `apply_revalidation` (and
  `transition` for Gate 1) as the `revalidate` / `approve-plan` / `record-merge` / `show` commands,
  driven from the new `/campaign-run` conductor, recording the human-gate events onto the ledger; it
  ticked the ROADMAP "Sub Project B2 — Phase 2" slot and deferred-followup item 7. See "PR 2 —
  live-launch as-built" above.
- **Engine-primary launch** is **Sub Project C2+D Phase 2** (D6), gated behind the Python-CLI
  reversal-gate.
- **`apply_revalidation` type-tightening** (binding each `RevalidationDecision` to its legal kwargs
  via `@overload` or a discriminated union) is a deferred design-quality nit — the runtime
  precondition (D4) already closes the correctness hole.
- This is a **design / DB-free** finding: it locks **no** real-data anchors and has no bedrock
  anchor table — the campaign core is pure-Python and touches neither DB. Its regression signal is
  the campaign test suite (DB-free, deterministic), not a tolerance-banded real-data number.

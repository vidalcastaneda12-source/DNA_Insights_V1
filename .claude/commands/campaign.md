Run the campaign orchestrator (Sub Project B2 Phase 2, `finding-041`) over a non-atomic
scope-split: drive an ordered, dependency-aware set of sub-scopes through `/scope-run` as a
persistent, multi-session campaign. Argument: a scope id (e.g. `PR-6`) whose dispatcher manifest
you have (for `start` / `dry-run`), or a campaign id to `status` / `resume` / `cancel` /
`write-roadmap` an existing campaign.

This is the runner half of B2 (the detector is `/scope-split`, `finding-039`): once the smart-cut
check proposes that `PR-X` is really several ordered sub-scopes, a campaign **sequences** them,
**tracks** their state across sessions, and **tees up** the next one — so you do not hand-track
which is ready or re-derive state every session. The whole decidable state machine lives in the
DB-free, unit-tested `genome.campaign` core (an append-only insert-then-flip ledger under locked
decision #7); the skill is faithful plumbing.

## Three invariants (read first)

1. **Never cross a human gate.** The campaign sequences and tees up, but it crosses **neither**
   gate on its own: Gate 1 (plan approval, `planning → implementing`) and Gate 2
   (`/verify-and-merge`, `implementing → merged`) are both external-event-gated in the state
   machine. The campaign is advisory at the human boundary, always.
2. **PR 1 does not auto-launch.** This phase plans, tracks, dry-runs, resumes, and reflects —
   it does **not** launch `/scope-run` for you, and the CLI does not yet record gate events as a
   sub-scope is driven (that live wiring is PR 2). `resume` *names* the next ready sub-scope; you
   run it manually through `/scope-run` and its two gates.
3. **Never hand-edit ROADMAP.** State is reflected only through the reused, clobber-guarded
   `append_roadmap_block` into the existing `<!-- B2-SUBSCOPES:BEGIN/END -->` managed region —
   `start` and `write-roadmap` are the only commands that touch it, and only that region. That
   region is shared with `scope-split write-roadmap` (one region, one writer): once a campaign has
   `start`ed, the campaign is the authoritative writer of that block — a later `scope-split
   write-roadmap` would revert the live statuses to bare proposed slots (recover with
   `campaign write-roadmap`).

## Steps

1. **Start a campaign.** Take the Stage-0 `scope-dispatcher` manifest JSON for the scope and run
   `genome campaign start --manifest - --engine static` (feeding the manifest on stdin). If the
   smart-cut reducer reports the scope atomic, it echoes the atomic sentinel and creates nothing
   (an atomic scope is not a campaign — plan it as one PR). Otherwise it seeds the append-only
   ledger (`data/campaign/<scope_id>.jsonl`), tees up the deps-free head to `ready`, and reflects
   the live state to the ROADMAP managed block. Real `PR-N` slot ids stay the human's call at the
   micro-gate — the campaign keys on the stable placeholder `<origin>-sN`.
2. **Preview without committing.** `genome campaign dry-run --manifest - --engine static` prints
   `would run N sub-scopes in order: <id1> -> …` and creates nothing (no ledger, no ROADMAP).
3. **Resume / track (multi-session).** `genome campaign status --campaign <scope_id>` renders the
   current view (one line per sub-scope: status, deps, origin, plus the escalation note on any
   ejected one — never a silent drop). `genome campaign resume --campaign <scope_id>` names the
   next ready sub-scope to run via `/scope-run`, or reports the campaign done / blocked.
4. **Re-validate before each sub-scope runs (model-driven).** Before launching the next ready
   sub-scope, re-dispatch it and decide: **still needed** (run it), **moot** (skip → it resolves
   its dependents), **changed** (re-propose with a fresh manifest snapshot), or **grown**
   (re-split, capped at one level — past the cap it ejects + escalates to you). The *decision* is
   yours (the re-dispatch is I/O); the pure transition is the core's. (In PR 1 these reducers are
   built and tested but not yet wired to a CLI driver — they land live in PR 2.)
5. **Reflect / cancel.** `genome campaign write-roadmap --campaign <scope_id>` re-reflects the
   current state into the managed block (idempotent — a no-op when already current).
   `genome campaign cancel --campaign <scope_id>` ejects every active sub-scope as appended
   terminal records (append-only; it never deletes the ledger), and the campaign reloads cleanly.

## Done when

Every sub-scope is `merged` / `moot` / `ejected` (`status` shows the campaign done). The ledger is
the audit trail (every transition is a superseding record, prior bytes untouched); the ROADMAP
managed block is the human-readable reflection.

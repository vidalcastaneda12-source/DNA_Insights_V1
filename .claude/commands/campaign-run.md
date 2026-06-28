Drive a live campaign (Sub Project B2 Phase 2, `finding-041`) end to end: walk a non-atomic
scope-split's ordered sub-scopes through `/scope-run` and the two human gates, **recording each
gate event in the campaign ledger as the human crosses it**. Argument: a campaign id (e.g. `PR-6`)
whose campaign you have already `start`ed (via `/campaign` or `genome campaign start`).

This is the **conductor** half of B2 made live (PR 2). Where `/campaign` *names* the next ready
sub-scope and `/scope-run` runs *one* scope, `/campaign-run` is the model-driven loop that ties
them together: it re-reads the persisted ledger every step (multi-session resumable — no in-memory
carryover), feeds each sub-scope's stored mini-manifest to `/scope-run`, **stops at both human
gates**, and journals the operator's approval / merge with `genome campaign approve-plan` /
`record-merge` once the operator has actually crossed the gate. The decidable state machine lives in
the DB-free, unit-tested `genome.campaign` core (an append-only insert-then-flip ledger, locked
decision #7); this skill is faithful plumbing that rides the Task tool, **not** new Python (there is
no `genome campaign run`).

## Three invariants (read first)

1. **The flag is the HUMAN's act.** Present the gate evidence and **STOP**. **NEVER** supply
   `--approved` / `--merged` yourself — those flags *record* a crossing the operator already made
   (Gate 1: they approved the plan; Gate 2: `/verify-and-merge` actually merged). The CLI rejection
   is the backstop: `approve-plan` without `--approved` and `record-merge` without `--merged` both
   refuse with a clean error and **no ledger write**, so an accidental autonomous crossing fails
   closed. You drive the loop; you do not cross the gates.
2. **Re-read the ledger every step.** Every command (`status` / `revalidate` / `approve-plan` /
   `record-merge` / `show`) loads the campaign fresh from `data/campaign/<id>.jsonl`. Never carry a
   sub-scope's status in your head across a gate — `genome campaign status --campaign <id>` is the
   single source of truth, and the loop is resumable across sessions because of it.
3. **Never hand-edit ROADMAP / never re-launch a merged sub-scope.** ROADMAP is reflected only by
   the campaign commands into the `<!-- B2-SUBSCOPES:BEGIN/END -->` managed region (`record-merge`
   reflects the merge; a stray `scope-split write-roadmap` would revert live statuses — recover with
   `campaign write-roadmap`). A sub-scope that is already `merged` / `moot` / `ejected` is terminal;
   the loop skips it.

## The loop

Resume the campaign, then for the next ready sub-scope walk the gates, recording each as it is
crossed. Repeat until `genome campaign status` shows the campaign done.

1. **Resume.** `genome campaign resume --campaign <id>` names the next ready sub-scope `<sub>`
   (or reports the campaign done / blocked — then stop). `genome campaign status --campaign <id>`
   shows the full current view.
2. **Re-validate before it runs (the decision is the human's).** Re-dispatch `<sub>` and decide:
   **still needed** / **moot** / **changed** / **grown** (§4 of `/campaign`). Record it with
   `genome campaign revalidate --campaign <id> --sub-scope <sub> --decision <d>` (`changed` / `grown`
   also need `--manifest -`). This is the campaign's own **autonomous sequencing** decision, never a
   human gate: `still_needed` moves `<sub>` `ready → planning`; `moot` / `grown` resolve or carve it
   and bundle the tee-up that readies its dependents. (If the verdict is anything but `still_needed`,
   loop back to step 1 — the sub-scope was skipped, re-split, or ejected.)
3. **Hand the manifest to `/scope-run` (GAP-A).** Read the sub-scope's stored mini-manifest with
   `genome campaign show --campaign <id> --sub-scope <sub> --json` and feed its `manifest_snapshot`
   to `/scope-run` as the Stage-0 manifest — the sub-scope is **already dispatched** (the scope-split
   carved it), so you skip `/scope-run`'s own Stage-0 dispatch and start at the plan. The campaign
   keys on the placeholder `<origin>-sN`; **minting a real `PR-N` id** for the `/scope-run` run stays
   the operator's micro-gate call (`finding-039`) — do not invent one.
4. **`/scope-run` Stage 1 (plan).** Run the plan stage for `<sub>` on its handed-in manifest. At the
   end `/scope-run` **STOPS at Human Gate 1** and presents the synthesized plan. **Stop here too.**
5. **🚦 Gate 1 — plan approval.** Present the plan evidence to the operator. **On their explicit
   approval — and only then —** record it: `genome campaign approve-plan --campaign <id>
   --sub-scope <sub> --approved` (`<sub>` goes `planning → implementing`). Without the operator's
   approval, do nothing; the core refuses an `--approved`-less crossing anyway.
6. **`/scope-run` Stages 2–4 (implement → review → handoff).** Resume `/scope-run --from stage2` for
   `<sub>`. It runs implement, review fan-out, and handoff, then **STOPS at Human Gate 2**.
7. **🚦 Gate 2 — verify-and-merge.** The merge is `/verify-and-merge` (its own typed-token gate,
   Sub Project A / `finding-037`): the operator runs it (or the evidence-gated path), and the squash
   merge happens **there**, under that token. **Only after the branch is actually merged** do you
   journal it: `genome campaign record-merge --campaign <id> --sub-scope <sub> --merged`
   (`<sub>` goes `implementing → merged` and the core tees up any now-unblocked dependent in the same
   write). `record-merge` does **not** merge anything — it records that `/verify-and-merge` already
   did. Without `--merged` it refuses with no write (the sole structural Gate-2 backstop, GAP-C).
8. **`/scope-run` Stage 5 (close).** Resume `/scope-run --from stage5` for `<sub>` to re-lock the
   operator-confirmed anchors and flip docs. Then **loop to step 1** for the next ready sub-scope.

## Resume mapping (mid-campaign, any session)

`genome campaign status --campaign <id>` is the resume pointer. Map each sub-scope's campaign status
to where `/scope-run` picks up:

- `ready` → re-validate (step 2), then **`/scope-run` Stage 1** (plan) on the shown manifest.
- `planning` → Gate 1 not yet recorded → **`/scope-run` Stage 1** (re-present the plan; the operator
  approves → `approve-plan --approved`).
- `implementing` → Gate 1 recorded, Gate 2 not → **`/scope-run --from stage2`** (coarse resume
  pointer; pick up at implement and run to Gate 2 = `/verify-and-merge` → `record-merge --merged`).
- `merged` / `moot` / `ejected` → terminal; skip to the next sub-scope (an `ejected` one carries an
  escalation note in `status` — surface it, it is never a silent drop).

## Done when

`genome campaign status --campaign <id>` shows every sub-scope `merged` / `moot` / `ejected` (the
campaign is done). The ledger is the audit trail — every gate crossing is a superseding record with
the operator's approval / merge journaled, prior bytes untouched — and the ROADMAP managed block is
the human-readable reflection of the terminal state.

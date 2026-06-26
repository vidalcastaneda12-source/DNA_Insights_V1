# Sub Project A — deferred follow-ups

Non-blocking backlog from the PR #102 close repo-sweep (2026-06-25). The agentic verify-gate
shipped clean; these are housekeeping / polish items to pick off as convenient. None gates any
other work. Durable rationale for the scope lives in [`finding-037`](../findings/finding-037-agentic-verify-merge-gate.md).

- [x] **`.pytest-tmp/` gitignore** — done in this PR (#103). Generated `app.db`/`genome.duckdb`
  test artifacts can no longer be staged by a stray `git add -A` (privacy decision #9).

- [x] **Design-doc status / prune** — **pruned** (the `DEC-0084` prune-implemented-plans
  precedent; `finding-037` holds the durable rationale). `docs/plans/sub-project-A-agentic-verify-gate.md`
  was deleted in the 2026-06-26 repo-sweep cleanup PR rather than have its stale
  *"ready for an implementation plan"* status line linger past Sub A's ship (PR #102).

- [ ] **Independence-framing polish** (low leverage) — finishing the evidence-gated reconcile in the
  spots the PR left on the old framing: `finding-034`'s stage table + its two Mermaid "HUMAN GATE 2"
  labels, and `CLAUDE.md` line ~20 (the VSC-User actor one-liner). They still read "VSC-User runs
  verification.md, merges" with no evidence-gated note. The `finding-034` amendment + `CLAUDE.md`
  line 52 already cover it for a careful reader — this is annotation tidy-up to match.

- [ ] **Per-PR-history ledger backfill** (pre-existing, *not* introduced by Sub A) — `MEMORY.md`'s
  per-PR retrospective rows stop at PR #93 (`DEC-0084`); PRs #94, #96–#102 have none. Append the
  rows + bump the "Declared complete: PRs … #93" footnote. (Sub A's own *decisions* are already in
  the ledger as `DEC-0087`–`DEC-0090`; this is only the separate per-PR index.)

- [ ] **`change_class` is a trusted input** (acknowledged in `finding-037`) — the DB-free verify-gate
  CLI can't re-derive `change_class` from the diff, so a mis-declared class is caught only by the
  human merge-token review. A stronger check (compare the declared class against
  `git diff --name-only`) would belong to a later hardening pass (Sub Project D / observability),
  not the core gate.

**Now unblocked (not a chore — a signal):** Sub Projects **B** (fast-follow loop), **C1**, and
**C2+D** all depended on A's gate model, which has now shipped. Any of them is ready for its own
`/scope-run` whenever you want it.

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

- [x] **Independence-framing polish** — **done (2026-06-26 Wave-1 docs sweep).** `finding-034`'s
  Gate-2 stage-table row + its two Mermaid "HUMAN GATE 2" labels (plus the sibling compact "GATE 2"
  + ASCII gate-2 labels, for consistency) now carry the owner-approved evidence-gated alternative
  (Claude runs `genome.verify_gate`, presents raw evidence, takes a typed approval, squash-merges;
  `finding-037`). The `CLAUDE.md` line-20 half was already done; this PR completed the `finding-034`
  half.

- [x] **Per-PR-history ledger backfill** (pre-existing, *not* introduced by Sub A) — **done
  (2026-06-26 Wave-1 docs sweep).** Note: by the time this ran, PR #113 had already backfilled
  #94–#112 (`DEC-0100 … DEC-0117`, footnote advanced to "PRs #19 … #112"), so the residual was a
  single row — this PR appended `DEC-0118` for the now-merged #113 and bumped the footnote to
  "PRs #19 … #113" (82 commits). Sub A's own *decisions* remain `DEC-0087`–`DEC-0090`.

- [ ] **`change_class` is a trusted input** (acknowledged in `finding-037`) — the DB-free verify-gate
  CLI can't re-derive `change_class` from the diff, so a mis-declared class is caught only by the
  human merge-token review. A stronger check (compare the declared class against
  `git diff --name-only`) would belong to a later hardening pass (Sub Project D / observability),
  not the core gate.

**Now unblocked (not a chore — a signal):** Sub Projects **B** (fast-follow loop), **C1**, and
**C2+D** all depended on A's gate model, which has now shipped. Any of them is ready for its own
`/scope-run` whenever you want it.

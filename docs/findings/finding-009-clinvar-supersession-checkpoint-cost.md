---
type: observation
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-05-17
supersedes: []
superseded_by: []
---
# Finding 009 — ClinVar supersession checkpoint cost

## Context

1. ClinVar release `2026_05_10` was loaded into `genome.duckdb` via the
   annotation supersession workflow during sub-phase 5.2 verification.
   Source file hash prefix `61e2b1fd3123bdc4`, size 439,003,062 bytes.
   Result: 8,978,989 active rows in `clinvar_annotations`, spanning
   4,523,355 distinct `variation_id` and 2,645,685 distinct non-NULL
   `rsid`.

2. First-time load completed in 280 seconds. Re-running the same loader
   against the same source version with `--force` (the supersession
   path: insert the new active set, then UPDATE the prior active set to
   `is_active=FALSE` with `superseded_by=<new_id>`) took 1,699 seconds —
   roughly 28 minutes. This is the first time the supersession pattern,
   locked in CLAUDE.md decision #7, has shown real friction at scale.

3. The locked workflow is identical across every annotation source:
   INSERT new rows as active, UPDATE prior active rows to deactivate
   them and link `superseded_by`. The cost was negligible for the small
   curated sources (5.1a PharmGKB, 5.1b CPIC). ClinVar at ~9M rows is
   the first source large enough to make the UPDATE side dominant.

## Observation

4. Cost breakdown for the same-version `--force` re-run (1,699s total):
   * ~300s — chunked INSERT of the new active set (8.97M rows in batches
     of 250K via PyArrow Table registration). Consistent with the 280s
     first-load wall-clock; bulk-load is not the regression.
   * ~1,400s — UPDATE of the prior active set (8.97M rows) plus
     DuckDB's post-commit checkpoint flushing several GB of MVCC working
     set into the main `.duckdb` file.

   *Correction — see item #15.* The 1,400s figure attributed to
   "UPDATE plus post-commit checkpoint" turns out to be mostly the
   UPDATE itself; the post-commit CHECKPOINT is effectively a no-op
   because DuckDB's COMMIT already flushes synchronously. The original
   framing is preserved here as part of the audit trail; item #15
   carries the corrected per-phase decomposition from the post-PR #41
   measurements.

5. Mechanism. DuckDB uses MVCC. An UPDATE creates a new row version,
   marks the old version with the transaction's commit ID, and keeps
   both alive in memory until COMMIT. For an 8.97M-row table the
   in-memory working set roughly doubles during the UPDATE transaction.
   On COMMIT, DuckDB schedules a checkpoint that flushes the doubled set
   to the main file, prunes the old versions, and compacts free space.
   The checkpoint is single-threaded and disk-bound; on the verification
   machine it accounted for the majority of the ~23-minute window after
   the chunked INSERT finished.

   *Correction — see item #15.* DuckDB's COMMIT flushes the dirty
   pages synchronously: the MVCC settle-up happens inside the COMMIT
   call itself, not after it returns. An explicit `CHECKPOINT` issued
   after `conn.commit()` therefore measures a no-op (~1-6 ms on the
   verification machine), not the actual flush. The dominant cost in
   the ~1,400s window is the UPDATE statement; the COMMIT contributes
   ~270s of flush time on its own and the explicit CHECKPOINT
   contributes essentially nothing.

6. The 280s first-load number is misleading as a baseline: there is no
   prior active set to deactivate. The 1,699s re-run number is the true
   steady-state cost of a same-version refresh and is what will recur on
   every weekly ClinVar pull.

## Implication

7. The supersession pattern itself remains correct (CLAUDE.md #7, locked).
   It preserves the audit trail and the `superseded_by` chain that future
   provenance queries depend on. The friction is in the UPDATE-plus-
   checkpoint step, not in the pattern.

8. ~28 minutes violates the "~30s for major operations" target now
   formalized in CLAUDE.md. The exemption in that target covers
   explicit long-running operations gated behind named subcommands
   (Beagle full-genome imputation at ~30 min) with per-chromosome
   progress instrumentation. A weekly ClinVar refresh is not in that
   category: it is a routine maintenance command users will run
   regularly, and the UPDATE+checkpoint window emits no structlog
   progress until COMMIT returns. Both the wall-clock cost and the
   silence are out of contract.

9. ClinVar releases weekly. A user who runs
   `genome annotate refresh --source clinvar` on a fresh release will see
   ~28 minutes of apparent stall: the chunked INSERT emits per-batch
   structlog progress, but the UPDATE+checkpoint emits nothing until COMMIT
   returns. Adding progress output across that window is the minimum
   responsiveness fix even if total wall-clock does not change.

10. Sub-phase 5.5 (gnomAD filtered) will exercise the same supersession
    path against a larger row count even after the
    (user ∪ ClinVar ∪ GWAS ∪ PGS) intersection filter mandated by
    CLAUDE.md "Things never to do." If 5.5 lands without addressing the
    UPDATE+checkpoint cost, the regression will be larger. The mitigations
    in the Follow-up section should be evaluated and (if landed) verified
    against the ClinVar re-run before 5.5 begins.

## Follow-up

11. **Deferred — explicit `CHECKPOINT` after COMMIT.** DuckDB schedules a
    checkpoint automatically when the WAL grows past `checkpoint_threshold`
    (default 16 MB), but the timing is opaque from the loader's
    perspective. Issuing an explicit `CHECKPOINT` after the supersession
    COMMIT moves the flush inside the loader's measured wall-clock window
    and makes it observable via the structlog timer that already wraps
    COMMIT. This does not reduce total wall-clock; it makes the cost
    legible and lets the loader emit a "checkpoint settling" log between
    the COMMIT return and the next user-visible step. Status: low-risk,
    recommend landing as a measurement step before any algorithmic change.
    Cost: a few lines in `supersession.py` plus a unit test.

12. **Considered and rejected — DELETE+INSERT instead of UPDATE+INSERT.**
    Replacing the `UPDATE ... SET is_active=FALSE` with a DELETE of the
    prior active set would not beat UPDATE+INSERT. Two reasons:
    * DuckDB DELETE incurs the same MVCC overhead as UPDATE — both
      produce a new tombstone version and require the same post-commit
      checkpoint to settle. The wall-clock would be comparable.
    * The supersession contract requires preserving the prior rows with
      `is_active=FALSE` for provenance queries and a `deactivated_at`
      audit-timing column. DELETE loses both. Reconstructing them from
      `annotation_source_versions` history would require a JOIN per
      provenance query and lose the per-row deactivation timestamp.
    Status: rejected. The audit cost outweighs any wall-clock improvement
    that may or may not materialize.

13. **Open question — chunked UPDATE.** The UPDATE currently runs as one
    statement: `UPDATE clinvar_annotations SET is_active=FALSE,
    superseded_by=... WHERE is_active=TRUE AND <prior version match>`.
    Splitting this into ~500K-row batches with intermediate COMMITs would
    amortize the checkpoint cost across smaller transactions, at the cost
    of breaking the atomicity of the supersession step — a process killed
    mid-refresh would leave the table in a mixed state where some prior
    rows are deactivated and some are still active alongside the new set.
    This trade is worth evaluating before 5.5 but should not land without
    an explicit CLAUDE.md-level decision relaxing the supersession
    atomicity contract. Status: open; needs design discussion before 5.5.

    *Update — post-PR #41 / #15 measurements.* The corrected
    decomposition makes the UPDATE statement itself the dominant
    phase (~17-19 min of the ~28 min wall-clock), not the COMMIT or
    the CHECKPOINT as item #5's original mechanism description
    suggested. Chunked UPDATE is therefore worth revisiting because
    it would target the actual hot phase, but the `--skip-if-same-version`
    flag (#14, shipped in PR #41) already handles the dominant
    same-version pain case: users who re-run against an unchanged
    release pass the flag and the supersession path short-circuits
    entirely. Chunked UPDATE would help genuine new-release pulls
    where every refresh has a real prior set to deactivate — still
    worth the design discussion, but no longer the only lever.

14. **Action items before sub-phase 5.5 (gnomAD filtered) begins:**
    * ~~Re-run ClinVar refresh with explicit `CHECKPOINT` (item 11)
      and capture the new breakdown.~~ **Shipped in PR #41.**
      `commit_and_checkpoint` issues `conn.commit()` followed by an
      explicit `CHECKPOINT`, bracketed by start/complete structlog
      events with `duration_ms`. (Post-shipping the measurement
      revealed the explicit CHECKPOINT is effectively a no-op; see
      item #15 for the corrected breakdown.)
    * ~~Add wall-clock progress logging between COMMIT and the
      explicit CHECKPOINT so users see what is happening across the
      ~23-minute window.~~ **Shipped in PR #41.** Per-phase
      structlog events (`supersession_update_start` /
      `supersession_update_complete`, `supersession_commit_start` /
      `supersession_commit_complete`, `supersession_checkpoint_start`
      / `supersession_checkpoint_complete`) make every phase
      observable from the log stream alone. (Item #16 closed the
      coverage gap that was preventing `_start` / `_complete` from
      firing on the `--force` path.)
    * Decide on item 13 (chunked UPDATE vs. atomicity) — design
      discussion in planning chat before implementation. **Still
      open**; see item #13's update for the post-correction framing.
    * ~~Decide whether to add a `--skip-if-same-version` short-circuit
      that aborts a `--force` re-run when `annotation_source_versions`
      already has a matching row, since a true same-version refresh
      writes no new information.~~ **Shipped in PR #41** as
      `--skip-if-same-version`; off by default, gated on matching
      `(version, source_file_hash)` against the currently-active row.
    * **Shipped in this PR.** Unify the `--force` and non-`--force`
      paths in every Phase-5 loader so both route through
      `deactivate_prior_versions` and the per-phase events fire on
      both paths. See item #16.

## Post-PR #41 measurement correction

15. **Corrected cost decomposition (post-PR #41 measurements).** The
    same-version `--force` ClinVar re-run against the existing
    `2026_05_10` release was re-measured with PR #41's per-phase
    events in place. The previously-attributed split ("~300s INSERT,
    ~1,400s UPDATE+checkpoint" from items #4 / #5) was directionally
    right but mislocated the dominant phase. The corrected
    breakdown, with all phases now observable via structlog events:

    * **~7-9s** — HEAD + (cache-hit) download. The audited HEAD
      request resolves the version label; the body is already on
      disk from the prior run so `download_to_cache` short-circuits.
    * **~17-19 min (1,020-1,140s)** — supersession UPDATE statement.
      Single `UPDATE clinvar_annotations SET is_active=FALSE,
      superseded_by=? WHERE is_active=TRUE [AND source_version_id <
      ?]` across ~9M rows. This is the dominant phase and is what
      item #4 originally folded into "UPDATE plus post-commit
      checkpoint." On the PR #41 measurement run this phase emitted
      no `supersession_update_start` / `supersession_update_complete`
      events because the `--force` path bypassed the helper that
      emits them (see item #16). With this PR's unified deactivate
      path, both modes now emit the events and the duration is
      visible in the log stream.
    * **~240s (~4 min)** — chunked INSERT of the new active set
      (~9M rows in 250K-row PyArrow chunks). Per-chunk progress is
      visible via `clinvar.bulk_insert.chunk` events.
    * **~270s (~4.5 min)** — `conn.commit()`. DuckDB's COMMIT
      flushes the combined UPDATE + INSERT dirty pages
      synchronously; the call does not return until the flush is
      done. Visible via `supersession_commit_start` /
      `supersession_commit_complete`.
    * **~1-6 ms** — explicit `CHECKPOINT` (PR #41 #11). Effectively
      a no-op because the preceding COMMIT already flushed every
      dirty page. The event still fires, so the measurement is
      durable — it just measures nothing.

    Total: ~28 min, dominated (~65%) by the UPDATE statement, not
    by the COMMIT and not by an asynchronous checkpoint after COMMIT
    as item #5's original mechanism description suggested.

16. **Coverage gap discovered and closed.** PR #41 added
    `supersession_update_start` and `supersession_update_complete`
    events inside `deactivate_prior_versions`. Real-data verification
    of the post-PR-#41 ClinVar `--force` re-run showed those events
    never fired on the `--force` path. Investigation revealed that
    every Phase-5 loader had a `_deactivate_for_refresh` shaped like
    `if not force: deactivate_prior_versions(...); else: <inline
    UPDATE>`: the non-force branch went through the shared helper
    and got the events; the force branch ran its own inline
    `UPDATE ... WHERE is_active = TRUE` and emitted nothing.

    Because the inline UPDATE is the dominant phase (item #15), the
    bypass meant the most expensive ~17-19 min of a `--force`
    re-run was un-instrumented despite PR #41 advertising
    "supersession observability."

    This PR unifies the path in all five loaders (ClinVar,
    PharmGKB, CPIC, GWAS Catalog, PGS Catalog). The change:

    * `deactivate_prior_versions` gained a keyword-only
      `force_all_active: bool = False` parameter. Default mode
      (the existing behavior) deactivates rows with
      `source_version_id < new_source_version_id AND is_active =
      TRUE`. Force mode deactivates rows with `is_active = TRUE`
      (no version filter), preserving the same-version `--force`
      semantics that the inline UPDATE provided.
    * Each loader's `_deactivate_for_refresh` is now a one-line
      pass-through that calls `deactivate_prior_versions(...,
      force_all_active=force)`. The wrapper stays so each loader
      has a clean per-source seam (and so loader tests have a
      stable patch surface).
    * Both `supersession_update_start` and
      `supersession_update_complete` carry `force_all_active` in
      their payloads, so a log reader can tell the two modes apart.

    The supersession atomicity contract (CLAUDE.md #7) is
    unchanged: the UPDATE remains a single statement in a single
    transaction with the chunked INSERT. The change is structural
    (one code path instead of two) plus instrumentation (events
    fire on both paths).

## Resolution

17. **Refactor shipped in PR #43.** Items #13 ("chunked UPDATE vs
    atomicity") and the broader question of whether the mass UPDATE
    was load-bearing at all were resolved structurally rather than
    by tuning the UPDATE. PR #43 replaced per-row `is_active` /
    `superseded_by` flips on the five Phase-5 annotation tables
    with a single-row version pointer in a new `annotation_sources`
    table (the version-pointer supersession pattern). The
    ~17-19 min UPDATE phase that dominated the cost decomposition
    (item #15) disappears entirely: a refresh now INSERTs the new
    rowset under a fresh `source_version_id` and UPSERTs the
    one-row pointer; there is no mass UPDATE to wrap. Atomicity
    (CLAUDE.md #7) is preserved by the single-row UPSERT.

    Measured same-version ClinVar `--force` refresh against the
    existing `2026_05_10` release: **4 m 56 s** end-to-end (down
    from 1,699 s / ~28 min on the per-row path). Inside the new
    window, the chunked INSERT of the new rowset and the
    `commit_and_checkpoint` flush dominate; there is no
    UPDATE-phase line item to attribute time to. PR #41's
    `supersession_update_*` events are no longer emitted on the
    supersession path (no UPDATE happens) and have been replaced by
    `supersession_version_flip` carrying prior + new
    `source_version_id` and per-version row counts.

    The pattern itself is documented in
    [finding-010](finding-010-version-pointer-supersession-pattern.md);
    that finding carries the design rationale, the readers-side
    reasoning, the implication for sub-phase 5.5 (gnomAD filtered,
    which now no longer needs to pay a ClinVar-scale UPDATE cost),
    and the follow-up items that survive into the new pattern
    (PharmGKB/CPIC `already_current` cosmetic cleanup, the HEAD-
    request-failure version-label fallback risk under the new
    pattern, and the orphan-rows cleanup procedure for prior
    versions left behind in the per-source table).

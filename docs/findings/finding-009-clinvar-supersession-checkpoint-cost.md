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

5. Mechanism. DuckDB uses MVCC. An UPDATE creates a new row version,
   marks the old version with the transaction's commit ID, and keeps
   both alive in memory until COMMIT. For an 8.97M-row table the
   in-memory working set roughly doubles during the UPDATE transaction.
   On COMMIT, DuckDB schedules a checkpoint that flushes the doubled set
   to the main file, prunes the old versions, and compacts free space.
   The checkpoint is single-threaded and disk-bound; on the verification
   machine it accounted for the majority of the ~23-minute window after
   the chunked INSERT finished.

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

14. **Action items before sub-phase 5.5 (gnomAD filtered) begins:**
    * Re-run ClinVar refresh with explicit `CHECKPOINT` (item 11) and
      capture the new breakdown.
    * Add wall-clock progress logging between COMMIT and the explicit
      CHECKPOINT so users see what is happening across the ~23-minute
      window.
    * Decide on item 13 (chunked UPDATE vs. atomicity) — design
      discussion in planning chat before implementation.
    * Decide whether to add a `--skip-if-same-version` short-circuit
      that aborts a `--force` re-run when `annotation_source_versions`
      already has a matching row, since a true same-version refresh
      writes no new information.

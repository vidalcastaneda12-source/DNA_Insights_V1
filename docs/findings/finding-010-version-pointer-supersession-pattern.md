# Finding 010 — Version-pointer supersession for evolving sources

## Context

1. PR #43 replaced per-row `is_active` / `superseded_by` flips on the
   five Phase-5 annotation tables (`clinvar_annotations`,
   `gwas_catalog_associations`, `pharmgkb_annotations`,
   `cpic_guidelines`, `pgs_catalog_scores`) with a single-row pointer
   in a new `annotation_sources` table. One row per `source_db`; the
   `current_source_version_id` column names the version that is
   "current" right now. A refresh inserts the new rowset under a fresh
   `source_version_id`, then UPSERTs the pointer.

2. The trigger was finding-009 #15. ClinVar's same-version `--force`
   refresh was 1,699 s end-to-end and the corrected per-phase
   decomposition placed ~17-19 min of that in a single
   `UPDATE clinvar_annotations SET is_active=FALSE,
   superseded_by=? WHERE is_active=TRUE` statement against ~9M rows.
   Finding-009 ##11-14 mitigated the *observability* of that window
   (explicit `CHECKPOINT`, per-phase structlog events,
   `--skip-if-same-version` short-circuit) but left the dominant
   ~17-19 min UPDATE itself unchanged. Finding-009 #13's chunked-UPDATE
   proposal was held open behind an explicit CLAUDE.md-level decision
   on relaxing supersession atomicity — the path forward was unclear
   while atomicity was framed as a per-row contract.

3. After PR #43 the same `--force` re-run against the existing ClinVar
   `2026_05_10` release measured **4 m 56 s** end-to-end. The ~17-19 min
   UPDATE phase disappears entirely: there is no mass UPDATE to wrap.

## Observation

4. Per-row `is_active` was protecting an atomicity contract whose only
   consumer was the supersession write path itself. No reader of the
   annotation tables genuinely required per-row flags — every
   downstream consumer (loaders, the
   `variant_annotations_index` refresh, view-layer joins) asks the
   logically equivalent question "which rows belong to the current
   release of source X?" The per-row implementation answered that by
   tagging each row with its lifecycle state. The version-pointer
   implementation answers it by tagging each row with its
   `source_version_id` (already present) and naming the current
   `source_version_id` in a single side-table row.

5. The atomicity contract is preserved by construction. CLAUDE.md
   decision #7 requires that readers never see a torn state — partway
   through a refresh, the user-visible "current" rowset must be
   either entirely the old release or entirely the new release, never
   a mix. Per-row supersession satisfied that by wrapping a mass
   UPDATE in the same transaction as the chunked INSERT. The
   version-pointer satisfies it by deferring the flip to a single-row
   UPSERT against `annotation_sources` that runs *after* the new
   rowset has fully landed: until the UPSERT commits, every reader's
   join to `annotation_sources.current_source_version_id` still
   resolves to the prior version's id, so the prior rowset is the
   "current" one. The moment the UPSERT commits, every reader's join
   resolves to the new id. Atomicity is now a property of a one-row
   write rather than a mass UPDATE — strictly stronger, because the
   one-row write is unconditionally fast and cannot partially fail
   across rows.

6. Audit semantics are preserved at the version grain rather than the
   row grain. "Which version is current?" lives in
   `annotation_sources`. "What versions have we ever loaded for this
   source?" lives in `annotation_source_versions` (the registry was
   already there; PR #43 dropped its `is_current` column and the
   `UNIQUE (source_db, version)` constraint because identity is the
   `source_version_id` alone — a `--force` re-load against an
   unchanged upstream allocates a fresh `source_version_id` rather
   than reusing the prior one). "What rows belong to a given
   version?" is answered by filtering the annotation table directly
   on `source_version_id`, which all five tables already carry. The
   prior rowset stays in the per-source table indefinitely, keyed by
   the older `source_version_id`; a future history-aware reader can
   reconstruct any prior state by walking
   `annotation_source_versions.ingested_at` and filtering rows on
   the corresponding `source_version_id`.

7. The reader-side cost is a single small join. Where the per-row
   model filtered with `WHERE is_active`, the version-pointer model
   joins `annotation_sources AS s ON s.source_db = ? AND
   s.current_source_version_id = t.source_version_id`. The join is
   against a one-row-per-source table; DuckDB resolves it to a
   constant filter. The runtime cost is negligible compared to the
   per-row supersession's write cost.

## Implication

8. Project convention going forward: **the version-pointer pattern is
   the canonical supersession mechanism for any source whose unit of
   supersession is "an entire dataset replaces the prior dataset" —
   i.e. evolving reference sources that publish periodic releases.**
   New supersedable sources should add a row to `annotation_sources`
   (or a parallel `{kind}_sources` table where the source category
   warrants its own registry) and route their loaders through
   `genome.annotate.supersession.flip_to_new_version`. They should
   not add `is_active` / `superseded_by` columns to their per-source
   table.

9. Per-row supersession remains appropriate where the supersession
   grain is the row itself rather than an entire source dataset.
   That covers `genotype_calls` (per-`(variant_id, source)` re-ingestion
   of the same chip; the active row is the latest call for that
   pair, not the latest release of an external source), and the
   aspirational supersession on `insights` / `evidence` / `derived_*`
   tables (where individual rows get re-derived as new evidence
   accumulates, and the active row is the latest version of that
   *one* finding rather than of an entire dataset). Those tables
   should keep per-row flags; the version-pointer rule applies to
   *source-grain* supersession only.

10. CLAUDE.md decision #7 is reworded to reflect the dual model: the
    atomicity contract is the same (readers see exactly one current
    state, never a torn one), but the mechanism is grain-specific —
    version-pointer for source-grain supersession, per-row for
    row-grain supersession. The semantic invariant is unchanged.

11. The five Phase-5 annotation tables are now the canonical
    examples of the new pattern. Sub-phase 5.5 (gnomAD filtered) and
    later annotation loaders inherit the pattern at scaffold time:
    `flip_to_new_version` accepts any table whose name is in
    `_SUPERSESSION_TABLES`, and a new loader registers by adding
    itself to that whitelist plus calling `flip_to_new_version` at
    the end of its supersession transaction. The gnomAD load — which
    finding-009 #10 flagged as the next pressure point — will not
    pay the ClinVar-scale UPDATE cost because there is no mass
    UPDATE to pay it on.

## Follow-up

12. **PharmGKB / CPIC `already_current=True` cosmetic cleanup.** The
    `was_already_current=True` short-circuit on
    `--skip-if-same-version` (finding-009 #14) is wired through every
    loader and returns a `RefreshResult` with the matching
    `source_version_id`. The CLI's per-loader summary still prints
    "loaded N rows" for PharmGKB and CPIC even when the short-circuit
    fired, because the summary template doesn't branch on
    `was_already_current`. Cosmetic only — the `record_count` returned
    is correct and the database state is unchanged — but the printed
    text is misleading. Worth one cleanup pass.

13. **HEAD-request-failure version-label fallback behavior.** During
    PR #43 verification, exercising the ClinVar HEAD request with the
    upstream NCBI host transiently unreachable revealed that the
    fallback path (`clinvar.version.last_modified_missing`) silently
    paints today's UTC date as the version label and proceeds. With
    the version-pointer model this is a noisier failure than under
    the old per-row model: a same-version `--force` against an
    unchanged upstream that hits the fallback would allocate a fresh
    `source_version_id` carrying today's date, flip the pointer to
    it, and orphan the prior rowset under a date label one day
    older — all because the HEAD failed. Same-day re-runs are
    indistinguishable from the previous load by version label alone;
    the `source_file_hash` would catch it, but the loader doesn't
    consult it on the version path. Worth its own finding eventually
    and probably a refusal-to-fallback policy aligned with GWAS
    Catalog's "propagate the error" stance.

14. **Orphan rows under superseded `source_version_id`s.** PR #43
    leaves prior-version rowsets in the per-source table indefinitely,
    keyed by their old `source_version_id`. Disk is cheap and the rows
    enable history queries; the supersession contract does not require
    deletion. But over a year of weekly ClinVar refreshes the
    `clinvar_annotations` table will accumulate ~52 × 9M = ~470M
    superseded rows. A periodic cleanup procedure that deletes rows
    whose `source_version_id` is older than the second-most-recent
    version per source (keeping the prior version for diff queries)
    would bound the table size. Not urgent — disk hasn't been the
    pressure point — but worth a runbook entry once the first
    same-source-multiple-versions diff query lands.

15. **Cross-source generalization opportunity.** The pattern as
    implemented is generic: a `_sources(source_db PK,
    current_source_version_id)` registry plus a
    `flip_to_new_version` helper. If a future non-annotation source
    needs similar source-grain supersession (e.g. a curated rule
    bundle for tier mapping, a versioned PharmCAT release bundle),
    it can either reuse `annotation_sources` if the "source" framing
    fits or instantiate a parallel `{kind}_sources` table. The
    helper module already gates `table` against a whitelist
    (`_SUPERSESSION_TABLES`); adding a parallel whitelist for a new
    source category is a small addition.

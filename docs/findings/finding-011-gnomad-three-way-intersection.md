# Finding 011 — gnomAD filter is three-way at PR B; PGS extension is a Phase 6 follow-up

## Context

1. CLAUDE.md "Things never to do" #3 mandates that gnomAD bulk loads
   must be filtered to the
   ``(user ∪ ClinVar ∪ GWAS ∪ PGS)`` intersection rather than the full
   release — the v4.1.1 sites-only VCFs are several gigabytes per
   chromosome and a personal-use local DB has no room for the full
   corpus. Sub-phase 5.5 ships the gnomAD filtered AF loader as the
   first concrete implementation of that rule.

2. The four-way intersection presumes a per-variant table for every
   component. At PR B the four components map to:

   * ``user`` — distinct ``(chrom, pos_grch38)`` in
     ``variants_master``. Present since Phase 2.
   * ``clinvar`` — distinct ``(chrom, pos_grch38)`` in
     ``clinvar_annotations`` under the currently-active source-version.
     Present since Phase 5.2.
   * ``gwas`` — distinct ``(chrom, pos_grch38)`` in
     ``gwas_catalog_associations`` under the currently-active
     source-version. Present since Phase 5.3.
   * ``pgs`` — distinct ``(chrom, pos_grch38)`` in
     ``pgs_score_weights`` under the currently-active PGS
     source-version. **Not present.** Phase 5.4 loaded score-level
     metadata only (``pgs_catalog_scores``); the per-variant weights
     table is Phase 6 work.

3. PR B's filter is therefore the three-way union
   ``(user ∪ clinvar ∪ gwas)``. Skipping the absent PGS leg keeps the
   loader implementable now; deferring the PGS coverage extension to
   a Phase 6 follow-up (gated on ``pgs_score_weights``) keeps CLAUDE.md
   "Things never to do" #3's intent intact (the full four-way
   intersection still bounds the eventual on-disk footprint).

## Observation

4. Implementing the three-way filter exactly mirrors the four-way
   shape from the SQL side: each leg is a ``SELECT DISTINCT chrom,
   pos_grch38 FROM <table> WHERE ...`` joined through ``UNION``. The
   only difference is whether the PGS leg is present. The
   ``annotation_sources`` pointer table (finding-010) already supplies
   the "currently-active source-version" filter that ClinVar / GWAS
   need; the same join shape will extend to the PGS leg without
   restructure once ``pgs_score_weights`` exists.

5. The three-way filter under-covers the eventual four-way set by
   exactly the rows that PGS introduces but neither ClinVar, GWAS, nor
   the user variants supply. Real-data verification at PR B will
   record the four composition counts (``user``, ``clinvar``,
   ``gwas``, ``union_total``) so the PGS extension can compare its
   added coverage against the same baseline rather than re-discovering
   what was already present.

6. The PGS extension is structurally an APPEND, not a refresh. It
   computes the set of PGS-component ``(chrom, pos_grch38)`` not
   already present in ``gnomad_frequencies`` under the active
   source-version, streams those positions out of gnomAD's remote
   VCFs, and inserts the new rows under the same active
   ``source_version_id``. No new ``annotation_source_versions`` row is
   allocated; the version pointer does not flip. The ``record_count``
   on the active version row is incremented to reflect the additional
   rows, and the loader emits a ``gnomad.coverage_extended`` event so
   the audit trail records the append.

   The CLI surface for the extension is a new flag ``--extend-pgs`` on
   ``genome annotate refresh --source gnomad`` (or a sibling
   subcommand, TBD at implementation time). The flag is rejected
   when no active gnomAD source-version exists (no version to extend);
   the operator must run a full ``genome annotate refresh --source
   gnomad`` first.

## Implication

7. The three-way filter at PR B is a temporary under-coverage of the
   four-way rule, not a permanent departure. CLAUDE.md "Things never
   to do" #3's wording stays as four-way; the gap between text and
   implementation is documented here and tracked as a Phase 6
   follow-up. Future sessions reading the runbook + this finding will
   see the under-coverage and the bounded plan to close it.

8. PR B's drift identifiers stay durable through the PGS extension.
   The PGS-extension APPEND increases ``rows_loaded``,
   ``distinct_variants_per_chrom``,
   ``filter_set_composition.union_total``, and the per-population
   presence counts; it does **not** re-derive the same numbers under a
   new ``source_version_id``. A real-data verification of the extension
   compares the new totals against PR B's locked baseline and reports
   ``pgs_extension_delta_rows`` as a new event field.

9. The four-way filter rule will become the literal SQL in
   ``_build_filter_set`` once the extension lands and
   ``pgs_score_weights`` exists. At that point the implementation
   matches the CLAUDE.md wording exactly, and this finding becomes
   historical. Until then, the loader's docstring + this finding + the
   runbook's gnomAD section all carry the explicit "three-way at PR B"
   note so the under-coverage is visible to every reader of the code
   or docs.

10. The PGS extension is gated on Phase 6 ``pgs_score_weights``
    landing — it is a Phase 6 follow-up, not a Phase 5 sub-phase. The
    ROADMAP carries it under Phase 6's ``pgs_score_weights``-gated
    follow-ups; its verification is not applicable until
    ``pgs_score_weights`` exists.

## Follow-up

11. **PGS-extension implementation triggers.** The APPEND requires the
    active ``pgs_score_weights`` row set to exist under a non-NULL
    ``annotation_sources`` pointer for ``pgs_catalog`` (the existing
    pointer flips to a version row whose record_count covers
    per-variant weights, not just score metadata). The Phase 6
    PGS-weights loader needs to flip the same pointer; if Phase 6
    keeps the metadata loader's pointer separate, the extension will
    instead look at a parallel ``pgs_score_weights`` source-version
    pointer. The decision lives at Phase 6 scaffold time.

12. **Drift sentinel for the extension.** When it lands, the PR's
    verification step should compare the added rows to the PGS
    coverage *not already present* in the three-way intersection. A
    delta of zero indicates either (a) every PGS-component position is
    already in the union (unlikely but possible), or (b) the extension
    is mis-wired. Loud-fail on a zero delta surfaces (b) as a
    regression signal before the operator trusts the extended
    coverage.

13. **Optional: drop the three-way under-coverage note from CLAUDE.md
    once the extension verifies.** CLAUDE.md "Things never to do" #3
    already reads as four-way; no edit is required when the extension
    lands. This finding can be marked as resolved (or moved to an
    archived sub-section) at that point so future sessions don't waste
    time re-reading the historical gap.

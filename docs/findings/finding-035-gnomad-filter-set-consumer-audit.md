# Finding 035 — gnomAD filter-set consumer audit: ClinVar/GWAS-only rows are loaded but never read

## Context

1. The gnomAD loader filters the upstream sites-only VCFs to the three-way
   union of distinct `(chrom, pos_grch38)` positions present in
   `(user variants ∪ active ClinVar ∪ active GWAS)` — see
   [`finding-011`](finding-011-gnomad-three-way-intersection.md) and
   `genome.annotate.filter_set.build_filter_set(strategy="three_way")`. On the
   user's real corpus the union is ~5.13M positions, of which only ~0.94M are
   the user's own variants; ClinVar alone contributes ~3.9M (≈76% of the set).
   See `docs/runbooks/annotations.md` (filter-set composition).

2. The full-genome gnomAD load is the slowest routine operation in the app
   (~14.6 h, finding-012). The dominant cost is streaming and parsing the
   remote BGZF blocks overlapping those ~5.13M coalesced positions. If the
   ClinVar/GWAS-only legs (the ~76%) are never actually read by any consumer,
   narrowing the filter to user-only positions would cut the loaded row set
   (and proportionally the transfer/wall-clock) ~4–5× — the single biggest
   available speed lever, on top of the per-chromosome parallelization shipped
   in this PR.

3. During the parallelization PR (the `--jobs` process-pool loader), VSC-User
   chose **"investigate, decide later"** for the filter scope: keep the
   three-way set for now, ship parallelization, and separately determine
   whether the ClinVar/GWAS-only rows are consumed. This finding is that
   investigation. The narrowing itself is **out of scope** for the
   parallelization PR and remains gated on this finding + a VSC-User decision,
   because it reverses finding-011's deliberate three-way choice and touches
   CLAUDE.md "Things never to do" #3.

## Observation

4. A full audit of every reader of `gnomad_frequencies` across `backend/src/`
   found exactly **two** consumers, plus internal load-bookkeeping queries.
   Both consumers **INNER JOIN** `gnomad_frequencies` to `variants_master`:

   * **`genome.annotate.index_refresh.refresh_index`** (the
     `variant_annotations_index` rollup) — joins on **full coordinates**
     `(chrom, pos_grch38, ref_allele, alt_allele)`:

     ```sql
     FROM gnomad_frequencies gn
     JOIN annotation_sources gn_src
       ON gn_src.source_db = 'gnomad'
      AND gn_src.current_source_version_id = gn.source_version_id
     JOIN variants_master vm
       ON vm.chrom = gn.chrom AND vm.pos_grch38 = gn.pos_grch38
      AND vm.ref_allele = gn.ref_allele AND vm.alt_allele = gn.alt_allele
     GROUP BY vm.variant_id
     ```

   * **`genome.annotate.loaders.gnomad._summarize_run`** (the post-load drift
     summary) — its `match_rate` and AF-bucket queries both INNER JOIN on
     `(chrom, pos_grch38)`; the overlap query even starts `FROM variants_master`.
     These are load diagnostics, not user-facing reads.

5. The remaining `gnomad_frequencies` reads are internal bookkeeping that never
   touch `variants_master` and never surface non-user rows: `_next_freq_id`
   (max-id allocation), `_populated_chroms` (resume tracking), and the
   total / per-chrom / per-population `COUNT(*)` summaries in `_summarize_run`.

6. No FastAPI endpoint, insights/evidence builder, derived table, QC pipeline,
   or Phase-6 stub reads `gnomad_frequencies` independently of `variants_master`.

## Conclusion

7. **No consumer reads a `gnomad_frequencies` row whose `(chrom, pos_grch38)` is
   not in `variants_master`.** Every join is an INNER JOIN to the user's
   variants, so the ClinVar/GWAS-only legs (~76% of loaded rows) are loaded and
   then silently discarded at read time. They are, today, dead weight: they cost
   ~4–5× the load time and disk for zero consumed data.

8. **Therefore narrowing the gnomAD filter to `strategy="user_only"` would not
   lose any currently-consumed data.** The `user_only` strategy already exists
   in `filter_set.py` (it is dbSNP's filter), so the change is a one-line
   strategy swap in `genome.annotate.loaders.gnomad._build_filter_set` plus a
   re-lock of the runbook's filter-set composition and `rows_loaded` drift
   numbers.

## Decision (deferred to VSC-User)

9. Narrowing is **recommended on the evidence** but **not taken here**, because
   it reverses finding-011's deliberate three-way design and intersects
   CLAUDE.md "Things never to do" #3 (which mandates filtering *down to at most*
   `(user ∪ ClinVar ∪ GWAS ∪ PGS)` — user-only is a strict subset, so it does
   not violate the letter, but it does discard a deliberate data-availability
   hedge). The hedge's only plausible future use is annotating ClinVar/GWAS
   positions the user does not yet carry; nothing consumes it today and no
   roadmap item requires it.

10. If VSC-User approves narrowing, it is a small, self-contained follow-up PR:
    swap `_build_filter_set` to `strategy="user_only"`, re-run the load (now
    ~4–5× less data, compounding with `--jobs`), and re-lock the runbook drift
    identifiers. If declined, the three-way set stays and parallelization is the
    speed mechanism. Either way the parallelization PR stands on its own.

11. Related: when PGS per-variant weights land (finding-011 Phase-6 follow-up),
    the four-way extension is moot if the set has been narrowed to user-only;
    if not narrowed, the PGS leg appends as finding-011 describes. The two
    decisions should be made together.

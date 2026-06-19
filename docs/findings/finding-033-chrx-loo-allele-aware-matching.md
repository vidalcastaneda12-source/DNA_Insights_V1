# chrX LOO harness: position-only matching manufactures cell-collapse

## Status

Fixed in PR 5a (a measurement-layer correction to the chrX non-PAR LOO harness
built in [`finding-031`](finding-031-chrx-nonpar-dosage-confidence-qc.md)). The
import dosage-confidence gate and the chrX M3 reload are **unchanged** — they
were independently verified correct (the gate kept 93,606 chrX calls; non-PAR
91,085). This finding corrects only how the LOO *report* pairs re-imputed
dosages with held-out truth.

## Context

The finding-031 LOO harness holds out the user's own typed male non-PAR anchors
in 5 disjoint folds, re-imputes each masked fold against the native non-PAR
panel subset, and measures the precision of the gate-kept set
(`DS >= 0.9`) against the held-out truth, stratified by (MAF bin × dosage-
confidence bin). The PASS bar (finding-031 §"Verification") is **≥ 95% overall
precision with no high-confidence MAF×conf cell collapsing**.

## Problem: a high-confidence cell reads 0% concordance

On the first authoritative real-data run (`genome imputation chrx-loo 3`, run
#0003) the headline is fine — concordance@threshold **0.9782** (6,969 / 7,124),
comfortably above the 95% bar. But the stratified table showed non-rare-MAF
(≥ 0.01) `dconf ≥ 0.9` cells reading **0% concordance**, which trips the
finding-031 "no high-confidence cell collapsing" sub-criterion and would
read as a gate failure.

Investigation (read-only, run #0003) found this is a **harness measurement
artifact, not a gate failure**. The gate's true precision is **≥ 97.82%**; the
artifact only ever *depressed* the reported number.

## Root cause: matching by genomic position alone

`read_imputed_calls` (`chrx_loo.py`, the `if pos not in mask_positions or pos
not in anchors_truth` guard) paired each masked anchor with the re-imputed
output **by genomic position only**. At a position where the panel/output
carries a *different* co-located variant than the user's typed SNV — a different
SNV, or (predominantly) an indel sharing the coordinate — the harness scored the
user's typed-SNV truth against that unrelated record's dosage. 100% of the
affected anchors have target-allele ≠ output-record-allele.

Two distinct sub-cases, both miscounted as misses:

* **Co-located different record present.** Smoking gun at pos 31,456,836 (typed
  SNV `A/G`, user truth `0`/ref):

  ```
  31456836  A    G     DS=0      ← the typed SNV re-imputes CORRECTLY to ref (concordant)
  31456836  ATG  A     DS=0.99   ← a co-located DELETION, scored as a "miss" vs the SNV truth
  ```

  The deletion's `DS=0.99` lands in the `dconf ≥ 0.9` gate-kept set and, scored
  against the SNV's `truth=0`, is a false "miss" — the 0% cell. The typed SNV
  itself re-imputes correctly (`DS=0` → hom-ref, concordant) but is gate-dropped
  (`DS < 0.9`), so it never offsets the spurious miss.

* **Typed SNV absent from the output.** At pos 9,771,470 (`A/C`), 22,743,228
  (`C/G`), 125,010,550 (`C/A`) the typed SNV is **absent** from the panel output
  entirely — only a *different* co-located SNV is present. These anchors are not
  imputable against this panel, yet position-only matching scored them against
  the unrelated SNV and counted them as misses.

The same position-only flaw also skewed the **MAF binning**: `INFO/AF` was read
from whichever co-located record cyvcf2 happened to yield at the position, so an
affected anchor could be binned by an unrelated variant's frequency. This is why
an external replication counted ~414 position-only-affected anchors while the
harness's stratified table flagged ~25 in specific MAF cells — the same artifact,
differing only in which MAF cell each multi-variant position landed in.

## Fix: allele-aware matching (measurement layer only)

1. `read_haploid_anchors` now returns `pos -> (ref, alt, truth)` — it already
   parsed the typed SNV's `ref`/`alt`; it now carries them alongside the truth.
2. `read_imputed_calls` matches each masked anchor to the single output record
   whose `(POS, REF, ALT)` equals the **typed SNV's**, and evaluates only that
   record's `DS` (and reads `INFO/AF` for the MAF bin from that *same* record).
   Co-located records whose `(ref, alt)` differ — a different SNV, an indel — are
   skipped, never scored against this anchor's truth. The non-PAR panel is
   biallelic-split (locked decision #3), so the match is exact: same REF and a
   single-element ALT list.
3. A masked anchor whose typed SNV is **absent** from the output (no matching
   `(POS, REF, ALT)` record) is **not imputable**: it is excluded from the
   concordance entirely — neither concordant nor a miss — and counted in a new
   `n_anchors_not_in_panel` tally surfaced in `REPORT.json`, the CLI summary, and
   the `imputation.chrx_loo.*` structlog lines, so the exclusion is visible rather
   than silent.

Because both `DS` and `AF` now come from the typed SNV's own record, the
spurious high-confidence misses disappear and the MAF binning is correct — the
gate-kept cells populate with correctly-matched variants (non-zero concordance)
or are legitimately sparse, and the "no high-confidence cell collapsing"
criterion becomes evaluable on real data.

## Consequence for the finding-031 gate verdict

The chrX reload **substantively passes** the finding-031 bar; this artifact was
the one blemish on an otherwise clean report. The gate's true precision is
**≥ 97.82%** (the artifact only depressed it). After this fix, re-running
`genome imputation chrx-loo 3` on the reloaded corpus is expected to show: the
non-rare-MAF cells populated with correctly-matched variants (non-zero
concordance) or legitimately sparse, the "no high-confidence cell collapsing"
criterion passing, headline concordance **≥ 97.82%** (the artifact only
depressed it), and a non-zero `n_anchors_not_in_panel` accounting for the
typed-SNV-absent positions. Lock those numbers into CLAUDE.md observation #3's
chrX bullet once captured.

## Tests

`test_imputation_chrx_loo.py`:

* `test_read_imputed_calls_scores_only_matching_allele_record` — the smoking-gun
  regression: a position carrying the typed SNV **plus** a co-located deletion
  (`DS=0.99`) and a co-located different SNV is scored only against the matching
  `(ref, alt)` record; the indel/other SNV are never counted, so no spurious 0%
  cell forms.
* `test_read_imputed_calls_typed_snv_absent_is_not_in_panel` — an anchor whose
  typed `(ref, alt)` is absent from the output (only a different co-located SNV
  present) is excluded from the concordance and counted in `n_not_in_panel`.
* `test_report_threads_not_in_panel_count` — the not-in-panel count rides through
  `compute_loo_report` into the report + `to_dict`.
* The existing `read_haploid_anchors` / `write_masked_target` /
  `read_imputed_calls` / partition / scoring / report tests stay green (migrated
  to the `(ref, alt, truth)` anchor shape and `FoldCalls` return).

## Out of scope (unchanged, verified correct)

- The import dosage-confidence gate (`ingest.py`, `_variant_quality`) — verified
  correct (93,606 chrX kept); not touched.
- The chrX M3-physical imputation and the FK-safe reload
  ([`finding-029`](finding-029-chrx-imputation-m1.md),
  [`finding-032`](finding-032-imputed-supersession-discrepancies-fk.md)).
- The `count_haploid_gts` prepare-time perf wart
  ([`finding-030`](finding-030-prepare-chrx-haploid-count-perf.md)).
- The consensus "prefer an imputed hom over a chip het at male non-PAR"
  refinement (the residual `male_nonpar_het_anomaly = 1` chip miscall,
  finding-031) — a separate consensus-layer question.

---
type: both
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-06-19
supersedes: []
superseded_by: []
---
# chrX non-PAR QC: dosage-confidence + LOO replace the dead DR² gate

## Status

Built in PR 5a (the QC layer on top of the M3-physical chrX imputation,
[`finding-029`](finding-029-chrx-imputation-m1.md)). The dosage-confidence import
gate and the 5-fold LOO harness are implemented and unit-tested; the
first-authoritative real-data numbers were captured on the authoritative gate run
(**run_0002**) and are locked in "Verification / PASS bar" below and CLAUDE.md
real-data observation #3: non-PAR kept **90,999** (87,578 ≥0.99 / 3,421 in
[0.9,0.99)), 5-fold LOO precision **0.985550**, `male_nonpar_het_anomaly` **1**.
The imputation-derived values are tolerance-banded (Beagle is non-deterministic;
run-to-run band ≈0.985–0.986).

## Context

PR 5a imputes chrX as three physical regions (PAR1 / non-PAR / PAR2), each
against its native panel subset; the male non-PAR leg is exported haploid, imputed
haploid, and re-diploidized for storage (R1). The imputation **works** — but it
was blocked at import by a structurally invalid QC metric.

## Problem: `INFO/DR2` is structurally dead for single-sample male non-PAR

Beagle's `INFO/DR2` is a **cross-sample** dosage-r² estimator: it estimates the
squared correlation between the (unobserved) true alt dosage and the imputed
dosage **across the cohort of imputed samples**. The imputation pipeline here is
**single-sample**, and in the **hemizygous non-PAR** region a male carries exactly
one allele — there is no within-sample heterozygosity and no across-sample dosage
variance. With no variance to correlate against, the estimator collapses to
`0.00` for **every** non-PAR marker — imputed *and* genotyped alike.

Measured on run #0003 (a mechanism-illustration run; the locked production values are in Status / the PASS bar below, captured on run_0002): **2,710,620 / 2,710,620** imputed non-PAR sites and
**20,501 / 20,501** typed non-PAR anchors all at `DR2=0.00`. The diploid PAR1 leg
of the *same run* emits a normal DR² distribution (11,505 nonzero, max 1.00), so
this is not a run-wide failure — it is intrinsic to single-sample male non-PAR.
Three single-sample probes confirmed it is **not** fixable by target ploidy
(hom-diploid target → still 0) nor by a real females-only diploid reference panel
(→ still 0).

The uniform import gate dropped any variant with `DR2 < 0.3`. With `DR2 = 0`
everywhere on non-PAR it would drop **all** non-PAR — including the user's own
~20,501 **typed** non-PAR anchors — leaving non-PAR ≈ 0, *worse* than baseline.
So the gate, not the imputation, was the blocker.

This also **corrects** the [`finding-029`](finding-029-chrx-imputation-m1.md) M1
verdict: "M1 destroyed information (DR²≈0)" conflated a **structurally dead**
metric with information loss. M1's GO-probe "2.3% > DR²0.3" came from a
**multi-sample** leave-out (which supplies the cross-sample variance the estimator
needs); single-sample production is `DR²=0` regardless of M1 vs M3. The
M1-vs-M3 *accuracy* question cannot be settled by DR² at all — only by
dosage-confidence / LOO. (See the correction note appended to finding-029.)

## Fix: the dosage signal is alive where DR² is dead

The haploid non-PAR Beagle output carries a graded `FORMAT/DS` distribution (run
#0003: 87,814 imputed sites at `DS > 0.9` plus a graded middle band). For a
hemizygous call, **dosage-confidence** `max(DS, 1 − DS)` is a valid,
sample-specific, per-variant quality metric in exactly the regime where DR²
collapses.

### `max(GP) = max(DS, 1 − DS)` for a hemizygous call — so `gp=true` buys nothing

For a haploid biallelic site the genotype posterior is over `{ref, alt}`. Let
`p = P(alt)`. The haploid dosage is `DS = 0·P(ref) + 1·P(alt) = p`, and the two
posteriors are `GP(ref) = 1 − p`, `GP(alt) = p`. Hence

```
max(GP) = max(p, 1 − p) = max(DS, 1 − DS).
```

So the dosage-confidence **equals Beagle's max genotype-posterior** for a
hemizygous call — re-running with `gp=true` to emit `GP` would reproduce the same
number. Verified numerically: max deviation `0.00e+00` over 200k draws. `DS`
already carries the posterior, so the gate reads it directly and avoids a runner
change.

### The gate — informative yield (typed anchors + confident-ALT imputed)

For a **male, non-PAR chrX** variant (`chrom == 'X'` ∧ `profile_sex == 'M'` ∧
`par_regions.is_nonpar(pos)`) the importer keeps the **informative** subset, using
Beagle's `INFO/IMP` to tell typed from imputed sites:

* a **typed** site (no `IMP` flag — the user's own observed genotype) is **always
  kept**, regardless of DS, for **anchor retention** (this fixes the acute
  regression: the ~20,501 typed non-PAR genotypes survive instead of being dropped
  by the dead DR² gate);
* an **imputed** site (`IMP`) is kept iff it is a **confident ALT-bearing** call,
  `DS >= dconf_threshold` (default **0.9**). Confident **hom-ref** imputed (`DS`
  near 0) and the uncertain middle are **dropped**.

Everything else — autosomes, **male PAR** (genuinely diploid, DR²-valid), and the
**female X** (genuinely diploid) — keeps the existing DR² gate. The decision lives
in one shared helper (`ingest._variant_quality`) used by both the real import and
the dry-run count, so they cannot diverge; the non-PAR boundary is
`par_regions.is_nonpar`, the same predicate `consensus_chrx_dosage_v` uses (pinned
by a parity test). The stored `quality` is the dosage-confidence `max(DS, 1 − DS)`
either way (`1.0` for a typed anchor, `= DS` for a kept confident-ALT call).

**Why drop confident hom-ref imputed.** `max(DS, 1 − DS) >= 0.9` (keep *any*
confident call) would, on run #0003, keep **2,285,372 / 2,295,317** non-PAR sites
(99.6%) — ~2.18M of them confident **hom-ref** imputed calls, ballooning the
consensus ~70% from one region for little insight (the user is ref-by-default
there). Restricting imputed sites to confident **ALT** drops that ~2.18M and lands
the yield at the intended order **10⁵** (~25.8K typed anchors + ~77.8K confident-ALT
imputed), while typed anchors — including the **ref** ones — are retained via the
`IMP` distinction (you cannot tell a typed-ref `DS=0` from a confident-ref-imputed
`DS≈0` by dosage alone; `IMP` is what separates them). A VCF with no `IMP` (non-Beagle
/ fixtures) treats every male non-PAR row as observed → kept (anchor-retaining default).

Guards: a missing DS on a male non-PAR row **fails closed** (raises) rather than
silently falling back to the dead DR² gate; a DS above the `0..1` haploid scale
trips a scale guard (the re-diploidizer copies the haploid DS verbatim onto the
`1|1` GT — `chrx_panel.py` — so non-PAR DS is provably on the `0..1` scale; a
future seam change that doubles it to `0..2` must fail loudly, not mis-gate). And
when the prepare manifest rendered the chrX target `male_nonpar_haploid` but the
profile sex does not resolve to `M`, the import refuses (importing that output
under the DR² gate would zero non-PAR) — pass `--sex M`.

## Storage: overload `imputation_r2`, no new column

Male non-PAR dosage-confidence is written into the existing
`genotype_calls.imputation_r2` column, with `quality_flags` appended
`'nonpar_dosage_conf'` as the provenance marker.

Rationale: a new `dosage_confidence` column is a **schema change**, which forces
`rm -rf data/` + `genome init` + re-ingest + a re-run of the whole post-5.7
canonicalize/merge backfill chain — re-deriving every locked number in CLAUDE.md
observations #3–6 — a heavy cost paid solely for this. The overload is contained
to rows that carried a *degenerate* `DR2 = 0` anyway, stays queryable via
`quality_flags`, and the **DR² run-counters are kept DR²-only**: `mean_r2`,
`variants_above_r2_0_3/0_8`, and `low_r2_count` never see a dosage-confidence, so
the run-level DR² statistics stay uncontaminated (the dconf rows are counted in a
separate `nonpar_confident` tally). Downstream, `consensus_chrx_dosage_v` corrects
male non-PAR dosage from the **GT** (`2 → 1`), never from R²/confidence, so the
het-anomaly guard is untouched; `_resolve_imputed_only` propagates the overloaded
value into `consensus_genotypes.consensus_r2` with provenance preserved via the
contributing call's `quality_flags`.

**Deferred tech-debt:** a proper `dosage_confidence` column (NOT NULL-defaulted,
DR² and confidence cleanly separated) the next time a schema rebuild is
independently required. Logged here so a future session does not re-overload by
default.

## Validation: 5-fold leave-one-out against the user's own anchors

DR²-death removes the usual "the metric says it's good" PASS criterion, so it is
replaced with a falsifiable, **accuracy-grounded** one (`imputation/chrx_loo.py`,
`genome imputation chrx-loo`):

- **5 disjoint folds**, each typed non-PAR anchor held out exactly once (dealt
  round-robin over sorted position, so each fold is an evenly-spaced comb and a
  held-out site always has typed neighbours — the realistic LOO condition).
- Per fold: write a masked haploid non-PAR target (the fold's anchors set to
  `.`), run **one** non-PAR Beagle region against the native non-PAR panel
  subset, read the imputed `DS` + `AF` at the masked positions, and compare
  `round(DS)` to the held-out truth.
- **Measures the gate-kept set's precision.** A masked anchor is re-imputed, so
  it is now an *imputed* call; the gate keeps it iff `DS >= dconf_threshold`
  (confident ALT). The headline concordance is therefore the **precision** of
  that kept set: among re-imputed anchors with `DS >= 0.9`, how often the call
  matches truth (a held-out hom-ref that re-imputes to a confident ALT is a
  *false ALT*; one that re-imputes below the bar is gate-dropped and excluded — a
  recall question, not a kept-call-accuracy one).
- **Validate, don't search:** the threshold is fixed a priori at `DS >= 0.9`; LOO
  only *measures* the precision achieved at it. Below the bar is **falsification**
  (escalate: tighten the threshold, or fall back to descope), not a hunt for a
  looser bar.
- **Stratify** by (MAF bin × dosage-confidence bin) over the gate-kept set, so
  each cell is a per-(MAF, confidence) precision the PASS criterion checks for
  collapse. Typed anchors skew common (in run #0003, 100% of confident-ALT sites
  are AF ≥ 0.05), so extrapolation to imputed-only rare sites must be read along
  the confidence axis (which the gate controls), reported per cell.
- Long-op discipline: per-fold structlog progress; all scratch under
  `archive/imputation/run_<id>/loo/` on the big disk, **never** `/tmp`. Emits a
  JSON report artifact and idempotently stamps the headline concordance onto the
  imputed `sample_qc.qc_notes` (same marker convention as the het guard).

The pure scoring core (`partition_folds`, `compute_loo_report`, the binning, the
mask/read VCF helpers) is unit-tested on synthetic fixtures; the Beagle
orchestration is the named long-op the gate runs.

## Verification / PASS bar (replaces "non-PAR mean DR² > 0") — LOCKED, run_0002

Measured on the authoritative gate run (**run_0002**); all criteria hold and the
numbers are locked here and in CLAUDE.md obs #3 (imputation-derived values are
tolerance-banded — Beagle is non-deterministic; re-derive read-only against
`data/genome.duckdb` + `archive/imputation/run_0002/loo/REPORT.json`):

1. **Anchor retention — PASS.** Typed non-PAR anchors imported (vs the prior 0):
   the typed-anchor subset is **84,657** rows at `imputation_r2`=1.0 (the
   `IMP`-absent rows, kept regardless of DS).
2. **Yield order — PASS.** Total non-PAR kept **90,999** (order 10⁵) = **87,578**
   dconf ≥0.99 + **3,421** in [0.9,0.99). Not `< 10⁴` (gate-eating signal), not
   ≈ 2.3M (confident hom-ref not dropped — the `IMP`-aware restriction regressing
   to keep-all). The ~2.18M confident hom-ref imputed are dropped *by design*.
   *Tolerance-banded (run-to-run ±~100).*
3. **LOO precision — PASS.** 5-fold precision of the gate-kept set (re-imputed
   anchors with `DS >= 0.9`) is **0.985550** (6957/7059 @ dconf 0.9; n_anchors
   20,472; not-in-panel 5,276; run_0002 `REPORT.json`) — above the **≥ 95%** bar,
   with **no** high-confidence MAF×conf cell collapsing (the finding-033
   allele-aware fix is in effect). Run-to-run band ≈0.985–0.986 (post-fix run_0003
   was 0.985971).
4. **`male_nonpar_het_anomaly` ≈ 0 — PASS:** **1** (one residual chip miscall; ≈0
   by construction under R1; `apply_chrx_het_guard`).
5. **Negative controls unchanged — PASS.** Autosomal `both_concordant` **115,509**
   / `single_source` **793,917** / `imputed_only` **2,146,302** / `unresolvable`
   **26**; `consensus_total` **3,160,364**; shared-call concordance
   **0.9997760079641613**; the autosomal/PAR DR² anchors, the diploid legs, and the
   DR² run-counters byte-identical to the pre-chrX baseline.

## Out of scope (tracked elsewhere)

- The `count_haploid_gts` O(variants × samples) prep wart —
  [`finding-030`](finding-030-prepare-chrx-haploid-count-perf.md).
- Panel bake-off (females-only vs mixed vs male-inclusive) — calls are 98%+
  stable; LOO validates the chosen 1000G panel.
- Multi-sample DR² — a cohort property, not user-specific; expensive/fragile and
  rejected for the gate (documented here only as the fallback it is not).
- A proper `dosage_confidence` schema column — deferred to the next independent
  schema rebuild (above).

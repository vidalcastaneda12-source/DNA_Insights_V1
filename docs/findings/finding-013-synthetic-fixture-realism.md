# Finding 013 — Synthetic fixtures built from assumptions encode the assumptions, not the source

## Context

1. Phase 5.5 (gnomAD filtered AF loader) ran through three
   implementation sessions before the first real-data verification
   was attempted. The combined session work produced
   `backend/src/genome/annotate/loaders/gnomad.py` (a ~1,300-line
   loader, the longest in the project), `backend/tests/test_loaders_gnomad.py`
   (692 passing unit tests at PR B commit time), per-chromosome
   synthetic VCF fixtures under
   `backend/tests/fixtures/gnomad/`, and integration tests that
   exercised the supersession / resume / partial-run paths against
   the synthetic fixtures.

2. The unit tests were green. Ruff was clean. Mypy --strict was
   clean. Coverage of every visible code path was high. Each of the
   three implementation sessions reviewed the prior session's work
   and ratified it. The loader looked finished.

3. The first real-data verification run produced 4,066 rows under a
   broken `source_version_id` with every `af_*` column NULL and an
   htslib "Coordinates must be > 0" failure cascade. The second run
   (after a partial fix) produced 3,733 rows with `[E::hts_itr_next]
   Failed to seek to offset 106658030152031: Illegal seek` flooding
   stderr. Only the third run, with two independent loader bugs
   fixed, produced the locked numbers in the runbook.

## Observation

4. Two distinct loader bugs survived three implementation sessions
   and 692 unit tests. Both originated in the same root cause: the
   planning prompt asserted assumptions about gnomAD v4.1 that no
   one had verified against the actual remote source.

5. **Bug A — wrong INFO key names.** `_record_to_row` read
   `AF_joint`, `AC_joint`, `AN_joint`, and `AF_joint_<pop>` keys
   from each record's INFO dict. The per-chromosome v4.1 sites VCFs
   (both exomes and genomes) carry the plain-suffix variants — `AF`,
   `AC`, `AN`, `AF_<pop>` — and the `_joint` family lives only on a
   separate combined release that this loader does not consume.
   Additionally, gnomAD v4 renamed the "Other / unspecified"
   ancestry group from `oth` to `remaining` in the VCF INFO keys
   while keeping the schema column `af_oth`. The loader read neither
   the correct key name nor the renamed one. Every population-AF
   column landed NULL on every row.

6. **Bug B — sentinel-position contamination.** `_build_filter_set`
   guarded the ClinVar / GWAS subqueries with `pos_grch38 IS NOT
   NULL`, which admitted ClinVar's sentinel `pos_grch38 = -1` rows
   (20,173 of them under the active release; the loader emits this
   sentinel for variants whose GRCh38 coordinate could not be
   resolved). `_coalesce_positions` merged the `-1` rows into
   `(-1, -1)` ranges and the per-chromosome loader passed
   `chr<N>:-1--1` to cyvcf2. htslib rejected the regions with
   "Coordinates must be > 0" — which itself was load-bearing because
   the failure corrupted htslib's read-offset state and produced the
   absurd `106658030152031` seek offsets observed downstream. Bug B
   masquerading as Bug C (the HTTP/2 framing recovery bug) was what
   made the diagnosis slow.

7. Both bugs were encoded identically in the loader code *and* the
   synthetic test fixtures. The fixtures were built to match the
   loader's assumptions — they carried INFO records named
   `AF_joint=0.05;AC_joint=5;...`; the supersession integration
   tests seeded ClinVar rows with `pos_grch38 = NULL` (which the
   guard correctly admitted) but not `pos_grch38 = -1` (which the
   guard incorrectly admitted). The tests asserted the loader did
   the right thing against the fixtures; the fixtures asserted the
   real data looked the way the planning session said it did; the
   planning session asserted what the data looked like based on a
   reading of gnomAD's documentation rather than an inspection of an
   actual record. Three layers of internal consistency, all wrong
   about the upstream truth.

8. The bugs would have been caught at planning time by one shell
   command:

       cyvcf2 dump --header https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/vcf/exomes/gnomad.exomes.v4.1.sites.chr22.vcf.bgz

   The header lists the actual INFO key set. The first non-header
   record dumps the actual INFO values per key. The total cost of
   running that command was about thirty seconds of operator time
   on the chr22 file (the smallest per-chromosome shard). The cost
   of *not* running it was three implementation sessions, two
   failed real-data runs at 14+ hours each, two fix commits, and
   this finding.

## Implication

9. **External-source loaders must build fixtures from real
   headers.** The convention going forward: for any loader that
   parses an external source (VCF, TSV, JSON, etc.), the planning
   session must include a "verify field names" step that fetches
   one canonical record from the actual source and lists its
   observable schema:

   * For VCF — the `##INFO=`, `##FORMAT=`, and `#CHROM` lines plus
     one non-header record dumping every INFO key.
   * For TSV — the first row (column headers) plus one data row.
   * For JSON REST — one canonical record showing field names,
     types, and any nested envelope.

   The synthetic fixtures used by unit tests should be derived
   from that canonical record — copy the INFO keys, column
   headers, or field names verbatim and only synthesize values.
   The planning prompt should encode the canonical-record output
   so the implementation session can ratify it against the
   loader's projection code.

10. The unit-test suite is necessary but not sufficient. A green
    test suite proves the loader is consistent with its fixtures.
    It says nothing about whether the fixtures are consistent with
    the real source. Real-data verification is the only check that
    closes that loop, and it must happen before a loader is
    considered shippable.

11. **Sentinel-position guards belong at the filter set, not at
    the per-source loader.** The PR-B fix tightened the
    `pos_grch38 > 0` guard uniformly across the user / ClinVar /
    GWAS subqueries and the union in `_build_filter_set`. Any
    future loader that consumes the filter set (sub-phase 5.6
    dbSNP, the gnomAD PGS extension, the materialized
    `variant_annotations_index` refresh in 5.7) inherits the
    guard for free. New sentinel-emitting upstream sources do not
    require per-consumer fixes.

## Follow-up

12. **Apply the "open source, list schema" step to future external
    loaders.** The dbSNP loader (sub-phase 5.6, now shipped) and any
    later VEP / Ensembl / ENCODE loaders should
    incorporate a canonical-record inspection step into the
    planning prompt before any code is written.

13. **Optional: `genome annotate inspect --source URL` helper.**
    A small CLI that opens an arbitrary URL (VCF, TSV, JSON) and
    prints its observable schema — INFO keys for VCF, column
    headers for TSV, top-level field names for JSON — would
    lower the friction of doing this step during planning. The
    helper would be ~50 lines, would not need a registry entry
    (it's a one-shot inspection tool, not a loader), and would
    pay for itself the first time it prevents a bug of the
    shape described in observations 5-7. Not blocking on
    sub-phase 5.6 but worth doing the next time the operator is
    in this code path.

14. **Carry-forward to other planning sessions.** This finding's
    structural lesson — "internal consistency across plan + code +
    tests does not imply correctness against external reality" —
    applies beyond external-source loaders. Any planning session
    whose ground truth lives outside the repo (third-party API
    contracts, upstream tool CLI flags, file-format
    specifications, hardware behaviors) is exposed to the same
    failure mode. The mitigation is the same: include a
    "verify against the source" step before encoding assumptions
    into either prompt or code.

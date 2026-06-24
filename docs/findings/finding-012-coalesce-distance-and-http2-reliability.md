---
type: observation
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-05-22
supersedes: []
superseded_by: []
---
# Finding 012 — Tabix coalesce distance dominates remote-VCF HTTP/2 reliability

## Context

1. Phase 5.5 (gnomAD filtered AF loader) planning encoded a default
   `--coalesce-distance` of 1000 bp into the loader without benchmark
   data. The constant lived at the top of
   `backend/src/genome/annotate/loaders/gnomad.py` as
   `DEFAULT_COALESCE_DISTANCE_BP = 1000`; the CLI exposed the same
   default through `genome annotate refresh --source gnomad
   --coalesce-distance N`. The choice was arms-length: 1 kb felt like
   a reasonable default for "adjacent filter positions merge into one
   tabix range" without anyone having measured what the cyvcf2 +
   htslib + libcurl + HTTP/2 + GCS stack does under load.

2. The loader iterates remote tabix range queries against per-chromosome
   gnomAD v4.1.1 sites VCFs on Google Cloud Storage. Each range query
   is one libcurl `GET` with a `Range:` header against an
   `HTTP/2`-multiplexed connection. The user's filter set (the three-way
   `(user ∪ ClinVar ∪ GWAS)` union, ~5.1 M distinct positions across
   chromosomes 1-22 + X) coalesces into one range per cluster of
   adjacent positions; at 1 kb coalesce, the range count scales close
   to the position count.

3. Real-data verification of PR B against the actual gnomAD bucket
   was the first time the loader was exercised at full scale. Two
   prior verification attempts had failed for unrelated reasons
   (the ClinVar `-1` sentinel feeding `chr<N>:-1--1` regions; the
   loader reading non-existent `AF_joint*` INFO keys); the third
   attempt — post both fixes — was the run that produced the data
   below.

## Observation

4. At the 1000 bp default, the verification run logged 630+
   `gnomad.chrom.htslib_recover` events on chromosome 1 alone within
   the first hour of wall-clock. Each event is a forced
   close-and-reopen of the cyvcf2 VCF handle, triggered when the
   stderr-tap detector saw an htslib BGZF / libcurl error token after
   a region's iteration. The recovery itself is correct (the
   `seen_keys` dedup makes record re-yields across reopens idempotent,
   and the test suite pins this), but each reopen costs a fresh TLS
   handshake plus a tabix-index re-fetch — roughly 2 seconds of
   wall-clock per event. Projecting the chr1 rate to the full 23
   chromosomes put the wall-clock at over 24 hours, with no
   confidence that the next chromosome's rate would not climb
   further as connection-pool effects compounded.

5. The verification run was aborted after one hour and re-launched
   with `--coalesce-distance 50000`. The full-genome run completed in
   14.6 hours with 4 reopens on chr1 and ~0 reopens on every other
   chromosome (≤2 per chromosome typical). The total reopen count
   across the full run was under 30. The 50× reduction in coalesce
   distance produced a roughly 300× reduction in reopens, indicating
   the HTTP/2 framing reliability of htslib's libcurl plugin against
   Google's CDN correlates with request count, not bytes transferred:
   coalescing 50 adjacent 1 kb ranges into one 50 kb range moves
   roughly the same body bytes but trips the framing failure ~50×
   less often.

6. The cause sits in libcurl's `CURLE_HTTP2` (error 16) handler.
   htslib 1.19's `hfile_libcurl` plugin opens one libcurl easy handle
   per `cyvcf2.VCF`; rapid-fire small range requests on the same
   handle eventually produce a framing error mid-BGZF-block-read,
   which corrupts the iterator's internal offset state and silently
   stops yielding records. The detector + reopen path (introduced in
   commit `bf56f96`) recovers correctly, but reopens are not free,
   and high reopen counts blow the wall-clock budget. Larger
   coalesced ranges keep the connection healthier per byte
   transferred — fewer request boundaries means fewer opportunities
   for the framing failure to land.

## Implication

7. The loader's `DEFAULT_COALESCE_DISTANCE_BP` is changed from 1000
   to 50000. The CLI flag still accepts any positive integer, so a
   future operator can tune up or down per their network conditions.
   At 50 kb the loader's behavior is "fewer, larger range requests"
   — the opposite of the planning-session intuition that "smaller
   ranges minimize wasted bytes." The wasted-bytes framing was the
   wrong model: bytes transferred are not the bottleneck, request
   count against HTTP/2 framing is.

8. Loader design guidance for any future remote-VCF source the
   project adds (notably dbSNP at sub-phase 5.6, which may use the
   same remote-tabix shape against NCBI's bucket): default
   `--coalesce-distance` to ≥50 kb unless benchmark data against
   that source's bucket says otherwise. The principle generalizes —
   "prefer fewer-but-larger range requests over many-small for
   remote VCF access" — but bucket-side framing behavior varies, so
   the specific number is worth re-measuring per source rather than
   inherited blindly.

9. The HTTP/2 retry mechanism in `_load_chromosome` stays in. The
   coalesce-distance bump reduces the rate of framing failures by
   two orders of magnitude but does not eliminate them; the
   detector + reopen path is still the durable recovery mechanism
   for the residual events.

## Follow-up

10. **Sub-phase 5.6 (dbSNP filtered).** When the dbSNP loader is
    written, default its analogous coalesce parameter to 50 kb or
    larger unless a NCBI-bucket-specific benchmark says otherwise.
    Mirror the audited HEAD + stderr-tap + reopen pattern from
    `gnomad.py` for the same reasons; dbSNP's bucket is on a
    different CDN but the htslib + libcurl stack is identical.

11. **Other remote-tabix sources.** Any future external annotation
    source that uses remote tabix (variant catalogs, population
    databases, etc.) should inherit the same coalesce-distance
    default and the same HTTP/2 retry mechanism. The shared
    machinery (the stderr-tap and the reopen loop) is currently
    inlined inside `gnomad.py`'s `_load_chromosome`; if a second
    remote-tabix loader lands, extracting a helper module under
    `genome.annotate.remote_tabix` (or similar) becomes worthwhile.
    Until then the inline implementation is fine.

12. **Optional: drift sentinel on reopens.** A
    `gnomad.refresh.complete` event field reporting the total
    reopen count across a run would surface a network-degradation
    signal cleanly. Today the per-chrom `gnomad.chrom.htslib_recover`
    events carry the data but a downstream consumer would have to
    aggregate them out of structlog. Worth adding when the gnomAD
    loader next gets touched.

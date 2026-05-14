# Phase 4 Beagle pipeline — real-data cleanup

## Context

The Phase 4 pivot to local Beagle 5.5 imputation (`finding-006`) shipped
clean against the synthetic test corpus, but exercising the pipeline
against the real merged 23andMe v5 + Ancestry v2 corpus surfaced three
small but real defects. None requires a schema change; all three are
addressed in this PR as a tight code-only cleanup ahead of Phase 5.

## Observations

### 1 — Genetic map chromosome labels are not chr-prefixed upstream

The Browning Lab's `plink.GRCh38.map.zip` ships per-chromosome map files
whose column 1 labels chromosomes as bare numbers (`22`, `23`) — not
`chr`-prefixed. Beagle 5.5's pre-built reference panels and our
`prepare`-generated upload VCFs both use the `chr`-prefixed form (`chr22`,
`chrX`). Beagle does exact-string chromosome matching against the genetic
map and refuses to run with mismatched labels, exiting per chromosome
with `missing genetic map for chromosome chr22`.

The first real run on this machine was unblocked by manually rewriting
each extracted `.map` in place with `awk`. The next user (or any
`panel install --force`) would hit the same wall.

### 2 — htslib floods stderr with contig warnings on every imputed VCF read

Beagle 5.5's output VCFs declare contigs only via implicit length-derived
headers that htslib does not accept as canonical, so every cyvcf2 read of
a Beagle result fires
`[W::vcf_parse] Contig 'chr<N>' is not defined in the header.` once per
record. The parse itself succeeds and the records are well-formed, but
the warning dominates stderr on a multi-million-variant import. The
warning fires at three sites: `beagle_runner._vcf_parses_cleanly` (used
to validate each per-chromosome Beagle output), `imputation/ingest.py`
`_stream_chromosome` (the streaming insert), and the dry-run path's
`_count_chromosome_variants`.

### 3 — `imputation_runs.submitted_at` and `completed_at` are not stamped reliably

`genome imputation list` against runs produced by the local Beagle flow
showed:

    #0002 status=completed ... submitted=- completed=-
    #0001 status=completed ... submitted=- completed=2026-05-13 23:11:52

Two distinct issues:

- `submitted_at` was never stamped for Beagle runs. The TopMed flow set
  it when the user supplied a status URL (signal of upload). The Phase 4
  pivot dropped that path without redefining the local-Beagle semantics.
- `completed_at` was stamped only on the chr22-only `--force` re-run
  path (`#0001`), not on the fresh pending → processing → completed run
  (`#0002`). `import_result`'s `update_status("completed")` call did not
  pass `set_completed=True`, so the column stayed NULL.

## Implications and fixes

### Fix 1 — Normalize map labels at install time

`reference_panel._install_genetic_map` now rewrites each extracted `.map`
file in place: every non-blank, non-comment line whose column 1 is not
already `chr`-prefixed has `chr` prepended atomically (write
`<path>.tmp`, rename), preserving the `0600` permission. The rewrite is
idempotent: a map whose column 1 already carries `chr` is left
byte-identical, so re-running `panel install` (with or without
`--force`) does not produce `chrchr<N>` and is a no-op on already-
normalized files. One info log line per rewritten file
(`reference_panel.genetic_map.chr_prefix_added`) provides a forensic
trail.

### Fix 2 — Scope htslib log-level suppression to imputation reads

A new private module `genome.imputation._htslib` exposes a
`silence_htslib_contig_warnings()` context manager. It lowers htslib's
global log level to `HTS_LOG_ERROR` (`1`) for the duration of one read
and restores `HTS_LOG_WARNING` (`3`, htslib's default) on exit. The
manager wraps the three read sites listed above. The suppression is
scoped: real htslib errors (truncated body, malformed records) still
fire at the ERROR level and continue to surface, and unrelated cyvcf2
readers elsewhere in the process see htslib's normal warning verbosity
once the imputation read completes. Test coverage verifies both halves —
suppressed inside the manager, restored after — via `capfd` (the
contig warning is emitted to the C-level stderr, which `capsys` cannot
intercept).

### Fix 3 — Restore the stamping invariant on every update_status call

`update_status`'s `COALESCE(..., CURRENT_TIMESTAMP)` semantics are
correct as-is; the bug was at the call sites that omitted the timestamp
flags. Two call sites were patched:

- `beagle_runner._move_to_processing_if_pending` now passes
  `set_submitted=True` on the `pending` → `processing` transition. The
  semantics for the local Beagle workflow are now explicit:
  ``submitted_at`` is stamped when the first chromosome's subprocess
  starts.
- `ingest._execute_import` now passes `set_completed=True` on the
  `processing` → `completed` transition that closes a successful import.

The invariant the helper callers must honour — every transition out of
``pending`` passes `set_submitted=True`; every transition to
``completed`` passes `set_completed=True` — is now documented in the
`update_status` docstring and at each transition site so future
callers (e.g. a future merge or analysis pipeline that reuses
`update_status`) inherit the rule.

## Verification

- `uv run pytest` — 279 tests green (up from 264, adding 15 tests
  covering the three fixes).
- `uv run ruff check` — clean.
- `uv run mypy --strict backend/src` — clean.
- Real-data spot check on the existing chr22 panel: rewritten
  `plink.chr22.GRCh38.map` column 1 reads `chr22`; chr22 re-run produces
  a row with both `submitted_at` and `completed_at` populated; no
  htslib contig warnings on either the run or the import.

## Follow-up

None for this session. The three defects are now closed. Future
imputation enhancements (HRC panel support, dbSNP-based hom-only
recovery, bref3 conversion) remain deferred per `finding-005` and the
Phase 4 plan.

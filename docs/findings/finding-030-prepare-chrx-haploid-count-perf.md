---
type: observation
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-06-19
supersedes: []
superseded_by: []
---
# `prepare-chrx` haploid-count is O(variants × samples) — ~80 min on the real panel

## Status

Open (doc-only). Surfaced by the PR 5a real-data chrX gate. Recommended fix
below is **not yet applied** — this finding records the issue and the remedy so
a follow-up (or this PR) can land it. No behavior change in this note.

## Symptom

`genome imputation panel prepare-chrx` (the M3-physical region split, PR 5a /
`finding-029`) took **~80 minutes** on the real 1000 Genomes chrX panel. It
**succeeded** — the three native subsets are correct — but the wall-clock is
wildly out of line with the rest of the pipeline, and the longest step emits
**no progress output**.

Measured (real-data gate, verified via `/proc`):

- Single non-PAR composition assertion ≈ **55 CPU-min pegged at 100% CPU**.
- Non-PAR subset: **3,202 samples**, ~2.6 GB compressed.
- Work performed by that one assertion: ~**10 billion** awk `split()` calls
  (≈ variants × samples), to compute an **exact** haploid-GT total.

## Root cause

`count_haploid_gts` (`backend/src/genome/imputation/chrx_panel.py:123`, awk at
`:58` `_COUNT_HAPLOID_AWK`) computes the *exact* number of haploid GT fields
across the whole file by `split($i, a, ":")`-ing **every genotype field of every
record** — O(variants × samples). On the 3,202-sample non-PAR subset that is the
~10 billion splits above.

Three call sites pay this at panel scale, all in `prepare_chrx_panel`:

- `chrx_panel.py:336` — the PAR1 / PAR2 haploid-free checks (loop at `:335`).
  Cheaper than non-PAR (PAR is ~3 Mb of the 156 Mb chromosome) but still
  O(samples) per record.
- `chrx_panel.py:345` — the **non-PAR** retains-males check. This is the
  dominant cost: the non-PAR subset is essentially the whole chromosome.

It passed CI because the unit-test panel fixtures are ~270-byte synthetic VCFs
(2 samples), so the O(samples) blow-up never appears at test scale — the same
**units-green / real-data-bites** pattern as the M1 round (`finding-029`).

## Recommended fix (existence, not exact count)

All three assertion call sites only need to know whether **any** haploid GT
exists (`> 0`), not the exact total:

- PAR subsets: assert haploid-free (`count == 0`).
- non-PAR subset: assert it retains male haploids (`count > 0`).

So short-circuit the count to stop at the first haploid GT. Concretely, add an
existence helper using a short-circuit awk and use it at the three call sites:

```awk
/^#/ { next }
{ for (i = 10; i <= NF; i++) { split($i, a, ":"); if (a[1] !~ /[|\/]/) { print 1; exit } } }
END { print 0 }
```

`def has_haploid_gt(vcf, *, bgzip_bin, awk_bin) -> bool` returning
`awk-output == "1"`. The first male non-PAR genotype is haploid, so this returns
in **sub-second** instead of streaming the whole 2.6 GB subset. (htslib `bgzip
-dc` feeding a piped `awk` that `exit`s closes the pipe early via SIGPIPE.)

**Is the exact count worth preserving?** No, not at panel scale. The exact value
is purely informational — it is only consumed by:

- the `imputation.panel.prepare_chrx.complete` structlog field
  (`chrx_panel.py:356`), and
- `ChrxPanelResult.nonpar_haploid_gts` (`chrx_panel.py:362`).

Nothing branches on the value. So change `ChrxPanelResult.nonpar_haploid_gts`
(int count) to an existence flag — e.g. `nonpar_has_haploid: bool` — and log
that instead. **Keep `count_haploid_gts` as-is** for `rediploidize_vcf`'s
post-assertion (`chrx_panel.py` → called from `beagle_runner.py:806`): there it
runs on the **single-sample** Beagle output, where the exact `doubled` count is
both cheap (O(variants × 1)) and a useful log value.

Suggested test (keeps existing tests green): assert the non-PAR composition
check still **rejects** a haploid-free non-PAR subset (the `has_haploid_gt`
existence path returns `False` → `prepare_chrx_panel` raises), mirroring the
current `test_region_split_rejects_nonpar_without_haploid`.

## Scope / non-impact (explicit)

- **Prep-time only, and idempotent.** The cost is paid by
  `panel prepare-chrx`, which is a one-time op: the three subsets cache beside
  the panel and a re-run hits `skip_existing` (`chrx_panel.py:319`) without
  re-counting. It is not on the routine `refresh` / `ingest` / `merge` path.
- **Does NOT affect `genome imputation run`.** The runner's only
  `count_haploid_gts` use is inside `rediploidize_vcf`
  (`beagle_runner.py:806`), which runs on the **single-sample** Beagle region
  output — O(variants × 1), not O(variants × 3202). The panel is never
  re-counted at run time.
- **Does NOT touch the gate anchors.** This is purely the assertion's runtime;
  the subsets it validates, the imputed output, the merge/consensus counts, and
  the index match counts are all unchanged. The fix is a pure speed-up of an
  existence check — byte-for-byte identical subsets and downstream results.
- **Out of the CLAUDE.md performance contract.** Routine CLI ops target ~30 s;
  long ops are gated named subcommands that **must emit per-step structlog
  progress** so the wall-clock window is observable. `prepare-chrx`'s ~55 CPU-min
  count step has zero progress output, so it is out of contract on the
  progress-instrumentation clause regardless of the speed-up — and the speed-up
  removes the need for instrumentation entirely (sub-second).

## Follow-up

- Apply the short-circuit existence helper + the `ChrxPanelResult` field change
  + the rejection test (keep `count_haploid_gts` for the single-sample
  rediploidize post-assertion). Small, prep-time-only, no anchor impact.

---
type: observation
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-05-24
supersedes: []
superseded_by: []
---
# Finding 015 — gnomad v10 audit-trail anomaly: orphan version rows from chrom-grain partial runs

## Context

1. The post-PR-B regression sweep on 2026-05-22 left an extra
   `annotation_source_versions` row for gnomad — `source_version_id = 10`,
   `version = 4.1.1`, `record_count = 154,080` — alongside the locked
   v9 baseline (`source_version_id = 9`, `record_count = 7,275,664`,
   active per `annotation_sources`). finding-014 #1 hypothesized v10
   was a `--chromosomes`-style partial run and assumed v6-v9 were
   stable historical versions but did not investigate. This finding
   resolves both questions and inventories the gnomad version state
   end-to-end.

## Observation

2. **Five gnomad `annotation_source_versions` rows exist; only v9
   holds any data.** All share `version = 4.1.1`, the same
   `source_url` template, and the placeholder `source_file_hash =
   'gnomad_4.1.1'` the loader writes at allocation
   (`gnomad.py:1364`). `ingested_at` is stored in local time
   (America/Chicago = UTC−5):

   | sv_id | `ingested_at` (local)   | `record_count` | active? | rows in `gnomad_frequencies` |
   |-------|-------------------------|---------------:|---------|------------------------------:|
   | 6     | 2026-05-20 13:09:14     | 4,066          | no      | 0                             |
   | 7     | 2026-05-20 14:27:18     | 3,733          | no      | 0                             |
   | 8     | 2026-05-21 11:44:28     | NULL           | no      | 0                             |
   | 9     | 2026-05-21 12:29:04     | 7,275,664      | **yes** | 7,275,664                     |
   | 10    | 2026-05-22 08:07:18     | 154,080        | no      | 0                             |

3. **v9 is one contiguous run.** Its 7,275,664 rows span all 23
   supported chromosomes (1-22 + X), every row shares
   `retrieval_date = 2026-05-21 17:29:07.281878 UTC` (= v9's
   `ingested_at` + 5h), and `freq_id` is a single contiguous range
   `1 .. 7,275,664` with zero gaps. v9 chr22 contains exactly
   `154,080` rows — numerically identical to v10's recorded
   `record_count`.

4. **No row in `gnomad_frequencies` (or in any of the other five
   per-source annotation tables) has ever been committed under
   v6 / v7 / v8 / v10.** `_next_freq_id` (`gnomad.py:794`) is
   `COALESCE(MAX(freq_id), 0) + 1`, so a prior committed run would
   have produced either a gap before v9's `freq_id = 1` (impossible)
   or pushed v10's run past `freq_id = 7,275,664` (the table-wide
   max). All four non-v9 rows are pure orphans. The `record_count`
   values 4,066 / 3,733 / 154,080 were set by the loader's
   unconditional backfill (`gnomad.py:1484-1492`) before the actual
   rows became durable; the exact transactional sequence is not
   recoverable from the static snapshot but the net effect is
   identical for all four — `record_count` claims a count the table
   does not contain.

5. **v10's `audit_log` slice matches a `--chromosomes 22` partial-run
   shape exactly.** `app.db` (UTC) shows three rows bracketing v10's
   creation (`ingested_at` + 5h = 13:07 UTC):

   | log_id  | timestamp (UTC) | resource_id              | phase / status      |
   |--------:|-----------------|--------------------------|---------------------|
   | 359     | 13:07:17        | `external_calls_enabled` | `config_change`     |
   | 360-361 | 13:07:21        | `gnomad_remote_vcf_open` | HEAD intent+success |
   | 362-363 | 13:13:42        | `gnomad_remote_vcf_open` | HEAD intent+success |

   Two `_audited_head` calls (`gnomad.py:876`) = one chromosome
   (exomes + genomes), versus the 46 that a full-genome run produces.
   The chr22 audited HEAD ~6m before the chr22 genomes HEAD is
   consistent with a normal iteration window. The contrasting v9
   shape is 46 successful HEAD pairs spread across 2026-05-21 17:29
   UTC through 2026-05-22 07:40 UTC (~14h11m, matching finding-012's
   documented runtime).

6. **The loader allocates `source_version_id` before any
   chromosome load and has no orphan-row cleanup path.**
   `gnomad.py:1358-1368` calls `insert_source_version` immediately
   after the pre-flight check, before the per-chrom loop. DuckDB's
   Python client commits each statement on its own (empirically: a
   fresh connection sees the row before any explicit `conn.commit()`),
   so the `annotation_source_versions` row is durable the moment the
   INSERT runs. The per-chrom loop's `conn.rollback()`
   (`gnomad.py:1436`) cannot undo it, and the loader has no
   equivalent of the `_cleanup_orphan_version_row` helper that
   ClinVar, GWAS Catalog, PharmGKB, CPIC, and PGS Catalog each ship
   for exactly this case (see e.g. `clinvar.py:740-764`,
   `pharmgkb.py:427-449`). gnomad is the only Phase-5 loader without
   it.

7. **The pointer-flip logic correctly handled every partial run.**
   `gnomad.py:1450-1473` only flips `annotation_sources` when
   `not failed and not partial_run and succeeded` and every supported
   chromosome has at least one populated row. v10's `--chromosomes 22`
   set `partial_run = True`, so the flip was skipped — pointer stayed
   on v9. This matches `test_partial_chromosomes_filter_does_not_flip`
   exactly. v6/v7/v8 match `test_partial_failure_leaves_pointer_unflipped`:
   the pointer remained unset until v9 first achieved full coverage.

## Implication

8. **v10 is a legitimate "no-flip" audit row, but `record_count` is
   wrong and the loader has no path to prune the orphan.** The
   version-pointer invariant is intact (readers join through
   `annotation_sources` and see only v9 rows), the partial-run
   semantics are honored, and the audit-log paper trail matches the
   shape of the invocation. The defect is narrow: the
   `annotation_source_versions` row outlives its data without a
   pruning step, and the backfilled `record_count` reports
   what-would-have-landed rather than what-actually-landed. v6, v7,
   v8 are functionally the same class of artifact from earlier
   failed full-genome attempts.

9. **`--resume` will reuse v10.** `_find_in_flight_source_version_id`
   (`gnomad.py:1068-1099`) returns the largest non-active sv_id with
   matching `version`. A future `genome annotate refresh --source
   gnomad --resume` will pick v10, see zero populated chromosomes,
   attempt all 23 under sv_id=10, and flip the pointer if successful.
   The end state is correct for the operator (active v10 pointer)
   but the "resume" framing is misleading — there is nothing to
   resume; the operator gets a fresh full load attributed to a
   leftover orphan id. v6/v7/v8 are eclipsed by v10 in that query and
   have no functional consequence as long as v10 exists.

## Follow-up

10. **Option A — doc-only (recommended).** Treat the orphans as the
    documented partial-run audit trail. v6/v7/v8/v10 stay in
    `annotation_source_versions`; the planning chat adds a sentence
    to the gnomad runbook noting that orphan rows with zero
    `gnomad_frequencies` references are an expected artifact of an
    interrupted `--chromosomes 22` invocation on 2026-05-22 (v10)
    and earlier failed full-genome attempts (v6/v7/v8), and that
    `--resume` will reuse v10. No code or data writes. The pointer
    behavior is already correct.

11. **Option B — small loader hardening.** Add
    `_cleanup_orphan_version_row(conn, source_version_id)` to
    `gnomad.py`, mirroring `clinvar.py:740-764` /
    `pharmgkb.py:427-449`, and call it from the per-chrom-loop
    failure path and from a final post-loop guard when no chrom
    committed any rows. The helper is FK-safe (no
    `gnomad_frequencies` row references an orphan sv_id, per the
    `_next_freq_id` ratchet argument above). Stops future orphan-row
    creation without touching existing v6/v7/v8/v10. Best aligns
    gnomad with the precedent the other five Phase-5 loaders follow.

12. **`[SUPERSEDED · STALE · DO-NOT-RUN · PR7-MOOT-2026-06-26 — see the
    Amendment below: the live DB has NO zero-row orphans; id=8 and id=10
    BOTH carry data, so this DELETE would erase the active + superseded
    gnomAD builds]`** **Option C — cleanup SQL plus Option B.** Option B plus a one-off
    `DELETE FROM annotation_source_versions WHERE source_db =
    'gnomad' AND source_version_id IN (6, 7, 8, 10)`. FK-safe by the
    same argument. Removes the `--resume` ambiguity (a future
    `--resume` would allocate fresh) at the cost of erasing the
    failed-attempt audit trail.

13. **Recommendation: Option A.** v10 (and v6/v7/v8) are functionally
    inert. The pointer is correct, readers see only v9, and the only
    edge case — `--resume` reusing v10 — produces a correct end
    state. Option B becomes the right next step the first time a
    second chrom-grain partial run produces another orphan (e.g.,
    during the pre-Phase-6 sweep). Option C is appropriate only if
    the planning chat decides the failed-attempt audit has no
    forward value; that judgement is the planning chat's, not this
    investigation's.

## Amendment — post-PR-C reload (2026-06-22): the id-specific cleanup is stale

The orphan analysis above (v6/v7/v8/v10 inert, **v9** the active pointer) describes
a **pre-rebuild** DB. The DB has since been rebuilt and PR C re-ran the gnomAD load,
so the live source-version landscape is now entirely different: **only**
`source_version_id=8` (superseded, **4,467,370** rows — the pre-chrX `user_only`
build) and `source_version_id=10` (**ACTIVE** pointer, **4,568,802** rows — the
post-chrX reload) exist; there is **no** v6 / v7 / v9.

**⚠️ The §12 Option-C SQL `DELETE … source_version_id IN (6, 7, 8, 10)` is STALE and
DANGEROUS on the current DB.** It would delete the **active** version (v10) and the
superseded build (v8, 4.47M rows) — no longer the zero-reference case the §12 FK-safety
argument relied on (v10 is referenced by `annotation_sources`; v8 by 4.47M
`gnomad_frequencies` rows). **Do not run it.** Any orphan cleanup (ROADMAP PR 7) must
first re-derive the *actual* zero-row orphan set against the live DB — by current
evidence there are **none** by those ids, so PR 7 may now be **moot**. Current
landscape: CLAUDE.md obs #4 (PR C re-lock).

**Closing note — PR-7 probe (read-only, 2026-06-26):** the zero-row-orphan query
(*not the active pointer **and** not referenced by any `gnomad_frequencies` row*)
returns **0 rows**. The live gnomad `annotation_source_versions` inventory is
**{8 (4,467,370 rows, superseded-with-data), 10 (4,568,802 rows, active)}**, the
`annotation_sources` pointer = **10**, and both ids carry matching
`gnomad_frequencies` data — there is **no FK-safe orphan to delete**. **PR 7 is
closed as moot; no `DELETE` was executed.** Future-orphan *prevention* already
shipped (Option B above, PR #53); the general superseded-row cleanup procedure
(including the data-bearing id=8) remains ROADMAP **PR 9** (finding-010 #14). See
ROADMAP PR-7 (now closed-as-moot) and the `CHANGELOG` entry.

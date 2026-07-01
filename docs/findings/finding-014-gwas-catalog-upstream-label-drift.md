---
type: observation
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-05-22
supersedes: []
superseded_by: []
---
# Finding 014 — GWAS Catalog upstream label drifts on byte-identical content

## Context

1. The post-PR-B regression sweep on 2026-05-22 was a vanilla
   per-source refresh against every Phase-5 loader: `genome annotate
   refresh --source <db>` for each of `clinvar`, `cpic`, `pgs_catalog`,
   `pharmgkb`, `gwas_catalog`, plus an interleaved `gnomad` run.
   `--force` was not used on the gwas_catalog leg. (Indirect evidence
   from `annotation_source_versions`: the four label-stable loaders
   produced no new version rows on 2026-05-22, which they would have
   under `--force` since the existing Step-2 short-circuit is
   `force=False`-gated. gnomad's drift between v6-v10 is a separate
   audit-trail question and not in scope for this finding.)

2. The pre-existing label-based short-circuit (`gwas_catalog.skip_already_current`,
   in place since PR #38) fires before the download when the resolved
   upstream version label matches `annotation_sources.current_source_version_id`'s
   `version` field and `force` is `False`. The five label-stable
   loaders all share this shape; finding-009 #14 layered an opt-in
   `--skip-if-same-version` flag on top of it for the post-download
   hash-match safety net (PR #41).

3. The sweep produced a single new `annotation_source_versions` row
   for gwas_catalog: `source_version_id = 11`, `version = 2026_04_27`,
   ingested at 2026-05-22 08:22:39 UTC. The prior-active row was
   `source_version_id = 4`, `version = 2026_05_16`, ingested
   2026-05-19 15:38:30 UTC under the locked PR #47 numbers. The
   `annotation_sources` pointer for `gwas_catalog` flipped from 4 to 11.

## Observation

4. The two `annotation_source_versions` rows describe byte-identical
   release content:

   | column                    | source_version_id 4 | source_version_id 11 |
   |---------------------------|---------------------|----------------------|
   | `version`                 | `2026_05_16`        | `2026_04_27`         |
   | `ingested_at`             | 2026-05-19 15:38:30 | 2026-05-22 08:22:39  |
   | `source_file_hash` (SHA-256) | `4717ff06cf2e6913…` | `4717ff06cf2e6913…` |
   | `source_file_size` (bytes)| 62,625,662          | 62,625,662           |
   | `record_count`            | 919,446             | 919,446              |

   `gwas_catalog_associations` rows under both ids agree on every
   locked drift identifier: `distinct_study_accession = 59,310`,
   `distinct_pmid = 6,627`, `distinct_rsid = 410,192`,
   `distinct_trait_name = 16,162`. The two refreshes loaded the same
   ZIP body — only the upstream label changed.

5. The label drift was **backwards in calendar time**:
   `2026-05-16` → `2026-04-27`, ~19 days earlier. That is not
   timestamp noise; it is the EBI stats endpoint returning a
   substantively different release identifier for content that has
   not changed. Two plausible upstream mechanisms (no way to
   discriminate from outside EBI):

   * **Cache inconsistency.** Different EBI cache fronts hold
     different snapshots of `/api/search/stats`'s response; routing
     between them produces a date that drifts in either calendar
     direction depending on which front responds.
   * **Revised release labeling.** EBI re-labeled the same data
     freeze backwards (e.g., to align the `date` field with an
     earlier curation cutoff rather than the most recent publication
     batch). The same content moved from `2026_05_16` to
     `2026_04_27` deliberately.

   Either way, the loader has no signal to distinguish "label
   drifted, same release" from "EBI re-shipped older data under its
   own label". Hash is the stable identity; label is the noisy proxy.

6. Only one drift event is on record (the May-19 v4 vs May-22 v11
   pair). Two data points across a 3-day window are insufficient to
   establish drift frequency, periodicity, or stable direction.
   Future sweeps should be expected to expose more.

7. The other four label-stable loaders did not exhibit this behavior
   during the same sweep. ClinVar resolves version via the FTP
   `Last-Modified` HTTP header on the canonical TSV.gz — a value
   that changes only when the release file itself is re-published.
   CPIC and PharmGKB resolve version from a `CREATED_*.txt` member
   inside the downloaded archive (no separate API call). PGS Catalog
   resolves version from the REST `releaseDate` field of the catalog
   metadata, which has empirically been stable across re-runs. GWAS
   Catalog is the only Phase-5 loader whose version label comes from
   a separate REST endpoint that ships its own date, decoupled from
   the release file's identity.

## Implication

8. **Hash-based fallback short-circuit (this PR).** A new
   `gwas_catalog.skip_content_unchanged` short-circuit runs after the
   download and before allocation of a new `source_version_id`. When
   `force=False`, the resolved upstream label differs from the active
   row's `version`, AND the downloaded ZIP's SHA-256 matches the
   active row's `source_file_hash`, the loader emits the event with
   the full label-vs-label and hash-vs-hash payload and returns
   `was_already_current=True` against the active row. The full
   reload path (insert_source_version → bulk_insert → version-pointer
   flip) is skipped. `--force` continues to bypass both the existing
   label-based short-circuit and the new hash-based fallback.

9. **The fallback is post-download by necessity.** The hash is only
   available after the ZIP bytes are on disk, so this short-circuit
   cannot save the network fetch — only the bulk-load, the version
   allocation, and the pointer flip. That is the right trade: at ~60
   MB the download is a few seconds on a residential connection
   while bulk-load + flip is ~1-2 minutes of DuckDB work plus a
   permanent audit-trail entry per drift event.

10. **Label is preserved in the active row.** The `RefreshResult`
    returned by the short-circuit reports the *active* version label
    (`2026_05_16` in the observed case), not the drifted label
    (`2026_04_27`). The CLI's printed `version=...` line therefore
    matches `annotation_source_versions.version` for the active row.
    The drifted label appears in the structlog event's
    `resolved_version` field for audit, but does not propagate to
    any persistent state.

11. **The audit trail honestly records the drift event.** The
    pre-existing v11 row stays in `annotation_source_versions`. It
    documents that EBI shipped that label on 2026-05-22 against the
    same ZIP bytes. Future operators inspecting the table will see
    the v4 → v11 pair plus this finding; rolling v11 back would
    erase the only durable evidence the drift happened. (`v11`'s
    `gwas_catalog_associations` rows under `source_version_id = 11`
    are also preserved; they duplicate v4's content but the storage
    cost is one-time and the rows-vs-rows tooling already handles
    multiple `source_version_id` values per source.)

## Follow-up

12. **Watch for drift on other label-by-side-channel sources.** If
    a future Phase-5 or Phase-6 loader resolves its version label
    from an API endpoint decoupled from the release file, it
    inherits the same risk. The gwas_catalog-specific fallback in
    this PR is the template; generalize it (a shared
    `maybe_skip_on_hash_match(source_db, version, hash, force)`
    helper in `genome.annotate.supersession`) the first time a
    second source exhibits the same drift pattern. Tracked as a
    bullet in `finding-005-deferred-improvements.md`.
    **Still OPEN after PR 10 / `RM-9f3c52c` (finding-043):** PR 10 shipped a
    *label-binding* sidecar + an inline version+hash steady-state guard, but
    deliberately did **not** extract this shared helper (OQ-4=4a-i keeps
    `supersession.py` untouched). The generalization remains open as its own scope,
    `RM-25072d2`.

13. **Optional: replace label-based identity with hash-based
    identity outright (Phase 6+ refactor).** The current model treats
    `(source_db, version)` as the human-readable identifier and
    `source_file_hash` as supporting metadata. An alternative model
    treats the hash as the canonical identity and the label as
    display metadata. Under that model, a refresh inserts a new row
    only when the hash differs; the label becomes a free-form name
    that can be revised in place without minting a new row. The
    schema already carries `source_file_hash` on every
    `annotation_source_versions` row, so the refactor is mostly a
    reader-side migration of joins from `version` to `hash`. Out of
    scope here; defer to the planning session for the version-identity
    refactor.

14. **Operator action for the v4/v11 audit trail.** None. v11 stays;
    `annotation_sources.current_source_version_id` stays at 11. The
    next refresh, with the new fallback in place, will short-circuit
    against v11 when EBI ships another drifted label for the same
    bytes (or against the active row at that time, whichever EBI's
    stats endpoint last labeled). Re-locking the runbook numbers
    against v11's `2026_04_27` label is unnecessary because the
    drift-identifier counts are identical to v4.

---
type: observation
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-06-10
supersedes: []
superseded_by: []
---
# Finding 022 — ClinVar/GWAS version label decouples from cached data on a rebuild reload

## Context

1. The schema-rebuild workflow is `rm -rf data/ && genome init`, then re-ingest +
   re-refresh the annotation loaders (CLAUDE.md "Schema changes require rebuilding
   local databases"). The on-disk **download cache survives** that rebuild on
   purpose — `genome.annotate.downloads.download_to_cache` is skip-if-exists
   (`dest.exists() and not force` → re-hash the local file and return its metadata,
   no network fetch), precisely so a rebuild does not re-pull hundreds of MB
   (downloads.py docstring). So after `rm -rf data/`, the annotation tables are
   empty but the cached source files are whatever release was last downloaded.

2. ClinVar and GWAS Catalog resolve their **version label from a live network
   call placed before the download**: `clinvar._resolve_version_via_head` issues an
   HTTP `HEAD` and reads `Last-Modified`; `gwas_catalog._resolve_version_via_stats`
   GETs the EBI stats endpoint and reads its `date`. The resolved label feeds the
   Step-2 idempotence check and then the new `annotation_source_versions` row. (CPIC
   and PharmGKB are immune — they read the version from a `CREATED_*.txt` member
   *inside* the cached archive, so their label is bound to the bytes; see
   finding-014 #7.)

3. The PR-3 canonicalize gate (2026-06) ran on a swept, rebuilt DB. The annotation
   reload happened in June against a **May** cache (ClinVar `2026_05_17`, GWAS
   `2026_05_19`).

## Observation

4. **The version label and the loaded data decouple on a fresh-rebuild reload.**
   On a rebuild the loader runs with no prior active row:

   * Step 1 resolves the label from the **live** upstream — in June that returns a
     **June** release identifier (ClinVar publishes weekly; EBI ships a current
     `date`).
   * Step 3 `download_to_cache(..., force=False)` finds the **May** cache file
     already on disk and returns it **without any network fetch** — the loaded
     bytes are the May release.
   * Step 4 inserts an `annotation_source_versions` row stamped with the **June**
     label, `record_count` / `source_file_hash` / `source_file_size` computed from
     the **May** cache bytes.

   Result: the in-DB version row reads June while the data it points at is the May
   release. `DownloadResult` carries `path` / `sha256` / `size_bytes` only — **no
   "was this a cache hit or a fresh download" signal** — so the loader has nothing
   to tell it the label it resolved does not describe the bytes it loaded.

5. **finding-014's hash fallback does not catch this on a rebuild.** That
   short-circuit (`gwas_catalog.skip_content_unchanged` / the opt-in
   `maybe_skip_same_version`) fires only when the downloaded file's hash matches the
   **currently-active row's** hash — keeping the active label instead of writing the
   drifted one (finding-014 #8/#10). On a fresh `rm -rf data/` rebuild there **is no
   active row**, so there is nothing for the fallback to reconcile against, and the
   live-resolved June label is written unopposed. finding-014's bug is *upstream*
   (EBI ships a different label for byte-identical content); this one is *local* (the
   preserved cache holds an older release than upstream's current label). Same
   symptom — label ≠ data — different cause, and the existing fallback covers only
   the first.

6. **This is a labeling defect, not a data defect.** The May data is correct and
   internally consistent (the gate's `gwas_matches`, `clinvar_matches`, every
   downstream count are the honest result of the May release). Only the
   `annotation_source_versions.version` string is wrong. NULL/relabel is lossless —
   the true release is recoverable from `source_file_hash`.

7. **Do not conflate this with the canonicalize `gwas_matches` −23.** The cache
   skew cannot produce a pre→post-canonicalize delta — the *same* loaded May data
   sits on both sides of canonicalize. The −23 (66,724 → 66,701) is a separate
   collapse-dedup effect, reconciled in finding-020 "recon C". This finding is only
   about the label string.

## Implication — the DB-vs-docs split (read this if you query the DB)

8. **The DB and the docs will disagree on the version string, by design until the
   fix lands.** After the gate:

   | | version label | actual release |
   |---|---|---|
   | `annotation_source_versions` (in the DB) | a **June** date | — |
   | this finding + finding-018/020 (docs) | — | ClinVar `2026_05_17`, GWAS `2026_05_19` |

   A reader who queries `annotation_source_versions.version` and sees a June date is
   looking at a **mislabel**: the data is the May release named in the docs. Trust
   `source_file_hash` (bound to the bytes) over `version` (resolved from a live call)
   whenever they can disagree. The locked drift identifiers in finding-018 / finding-020
   are stated against the **true** May releases, not the DB's June label.

9. **Docs-only this session.** This PR documents the decoupling and states the true
   cache dates; it does **not** change loader code (that would broaden the
   canonicalize squash). finding-020's corpus line and CLAUDE.md obs #4 are corrected
   to the true GWAS `2026_05_19`. The code fix is deferred — see finding-005 #10.

## Follow-up

10. **Bind the label to the loaded bytes (finding-005 #10; the pre-Phase-6 PR 10 slot, ROADMAP).** Two
    viable shapes: (a) write a sidecar `<file>.version` next to the cache on a fresh
    download and read it back on a cache hit, so the label always describes the bytes
    on disk; or (b) generalize finding-014's `maybe_skip_on_hash_match` so that on a
    rebuild reload, a cached file whose hash matches *any* prior
    `annotation_source_versions` row adopts that row's label instead of the
    live-resolved one. Either binds label↔bytes; (a) also covers the
    no-prior-row-anywhere case. Tracked in finding-005 #10.

11. **Operator action for the mislabeled rows.** None blocking. The data is correct;
    a future `genome annotate refresh --source {clinvar,gwas_catalog} --force` against
    a re-pulled current cache will mint a correctly-labeled row. Until then, this
    finding is the authoritative map from the DB's June label to the true May release.

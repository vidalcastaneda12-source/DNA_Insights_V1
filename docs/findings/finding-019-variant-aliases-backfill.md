# Finding 019 — variant_aliases backfill from dbSNP RsMergeArch

## Context

`variant_aliases` (the rsID merge/withdrawal map) shipped empty when the dbSNP
loader landed in sub-phase 5.6 (finding-016 #8): PR #59 populated
`dbsnp_annotations` only, and the `_next_alias_id` allocator (PR #57) sat unused.
The table is the data dependency for the deferred **tier-2 rsID merge matching**
(finding-005 #4) — the one merge tier Phase 3 left unimplemented because there
was nothing to match against. This is the first of the post-5.7 backfills
(ROADMAP "Post-5.7 backfills"): it fills `variant_aliases` so the later tier-2
PR has a canonical `old_rsid → current_rsid` map.

This finding records the four design decisions and the drift identifiers.

## Observation

### 1. Data source — `RsMergeArch.bcp.gz`, not the dbSNP VCF

The dbSNP VCF already loaded for `dbsnp_annotations` (`GCF_000001405.40.gz`)
exposes only `record.ID`, the *current* rsID for each record — it carries **no
list of the rsIDs that merged into it**. Merge history lives in a separate NCBI
file:

- **URL:** `https://ftp.ncbi.nih.gov/snp/organisms/human_9606/database/organism_data/RsMergeArch.bcp.gz`
- **Size / vintage:** 146 MB gzipped, last modified **2018-02-07** (URL + size
  re-confirmed live 2026-05-27).
- **Format:** gzipped, tab-delimited BCP dump, no header, bare-integer rsIDs
  (no `rs` prefix). Columns (0-indexed):
  `0 rsHigh | 1 rsLow | 2 build_id | 3 orien | 4 create_time | 5 last_updated_time | 6 rsCurrent | 7 orien2Current | 8 comment`.
  The loader maps **`alias_rsid = rsHigh`** (the merged-away ID) and
  **`current_rsid = rsCurrent`** (the resolved survivor, which already collapses
  transitive merge chains — `rsLow`, the immediate target, is deliberately not
  used). `alias_type` is `'merged'` for every row.

**Why the staleness is acceptable.** RsMergeArch is the legacy flat-file dump,
frozen since the build-151 era (2018); the redesigned build-152+ dbSNP pipeline
embeds merge history in the per-chromosome RefSnp JSON (hundreds of GB, not
viable for a local-first app). But merges are append-only and historically
monotonic — a merge recorded at build 151 stays valid — and the user's chip
manifests (23andMe v5, Ancestry v2) are contemporaneous with that horizon, so
the frozen file covers exactly the era of stale rsIDs a chip is likely to carry.
Post-2018 merges are missed; that is a documented limitation, not a regression.
If a future consumer needs newer merges, the source revisit is the per-rsID SPDI
API or the RefSnp JSON, both far heavier.

### 2. Filter strategy — user-relevant, both sides

The full file is tens of millions of rows. Mirroring the dbSNP `user_only`
precedent (finding-016), the loader keeps only rows where **`rsHigh ∈
variants_master.rsid` OR `rsCurrent ∈ variants_master.rsid`**. Both sides are
kept because tier-2 matching resolves in two directions:

- `rsHigh ∈ user` — the user carries a stale rsID; the lookup canonicalises it
  (the primary lift).
- `rsCurrent ∈ user` — the user carries the canonical rsID while an external
  source (or the other chip) carries the old one; the old rsID must resolve to
  the user's variant.

Self-merges (`rsHigh == rsCurrent`), rows touching neither user side, and
malformed / non-numeric rows are dropped; output is deduped on `alias_rsid`.

### 3. Supersession — same dbSNP epoch, no pointer flip

`variant_aliases` is part of the **dbsnp source group**: PR #57 whitelisted both
`dbsnp_annotations` and `variant_aliases` in `_SUPERSESSION_TABLES` under the
single `annotation_sources` pointer for `'dbsnp'`. One `source_version_id`
governs both tables. The backfill therefore writes alias rows **under the
`source_version_id` the dbsnp pointer already names** — it allocates no new
version and **does not flip the pointer**, so the 29 GB VCF is not re-streamed
and `dbsnp_annotations` is untouched. The rows are "current" by construction
because they share the pointed-to id. `annotation_source_versions.record_count`
is **not** mutated (it belongs to `dbsnp_annotations`, which shares the row).

Re-run semantics: a re-run with no rows present is a pure INSERT; `--force`
does `DELETE … WHERE source_version_id = <current> ` then re-INSERT, wrapped in
one transaction so the supersession atomicity guarantee (CLAUDE.md #7) holds — a
reader sees the whole prior set or the whole new set, never a mix.

**Coupling (documented):** after any future `genome annotate refresh --source
dbsnp` flips the dbsnp pointer to a fresh `source_version_id`, the alias rows
under the old id stop being current and `refresh-aliases` must be re-run to
re-attach the map to the new epoch. This is the version-pointer pattern
(finding-010) working as designed; the runbook reload sequence reflects it.

### 4. CLI — a dedicated `genome annotate refresh-aliases` command

Not folded into `refresh --source dbsnp`, which is wired to the remote-tabix VCF
path (`--chromosomes/--resume/--coalesce-distance`). RsMergeArch is a different
artifact (whole-file download via `download_to_cache`, no tabix), a different
cadence (frozen file), and a different (rsID, not position) filter. The command
follows the `refresh-index` precedent: a standalone `annotate` subcommand,
lazy-imported from the CLI, **not** a registered loader. It reads the current
dbSNP `source_version_id`, fails fast with a clear error if dbSNP is not loaded
(before any download), and writes under that id.

## Implication

Drift identifiers (locked on first real-data run, mirrored in CLAUDE.md
"Real-data observations" and the CHANGELOG entry):

| Metric | Value |
|---|---|
| `rows_loaded` | _pending first real-data run_ |
| `distinct_alias_rsid` | _pending_ |
| `distinct_current_rsid` | _pending_ |
| `user_old_rsid_hits` (user rsID ∈ `alias_rsid`) | _pending — the tier-2-lift proxy_ |
| `user_current_rsid_hits` (user rsID ∈ `current_rsid`) | _pending_ |
| wall-clock | _pending (expected single-digit minutes)_ |

`user_old_rsid_hits` is the headline number: it is the count of user variants
whose rsID is a merged-away alias that now has a canonical mapping — i.e. how
much tier-2 lift the matching PR can land. A re-run against the same corpus +
frozen RsMergeArch that deviates from the locked values is a regression signal.

**Performance.** The ~80M-row RsMergeArch scan runs in Python (`csv.reader` is
C-backed) with the matched set bulk-inserted via PyArrow; expected single-digit
minutes. This exceeds the 30 s routine-refresh target by design — it is a named,
gated backfill subcommand with per-5M-row `variant_aliases.scan.progress`
logging, within the same contract carve-out as `imputation run`.

## Follow-up

- Lock the table numbers above on the first real-data run (verification step).
- Withdrawals (`SNPHistory.bcp.gz`, `alias_type='withdrawn'`) and splits are out
  of scope for this backfill; revisit if a consumer needs them.
- Tier-2 rsID matching in `refresh-index` / `merge` (finding-005 #4) is the
  consumer; it is a separate PR.
- Orphan-row cleanup under superseded dbSNP `source_version_id`s is the
  project-wide finding-010 #14 concern, unchanged here.

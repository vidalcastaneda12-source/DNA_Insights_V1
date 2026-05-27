# Finding 017 — Phase 5 reset around the loader/runner cut

## Context

Phase 5 sub-phase planning drifted across `ROADMAP.md`, `README.md`,
`CLAUDE.md`, and `CHANGELOG.md`. The loader phase had accreted items that were
never loaders: a profile-level QC rollup (5.8), a `variants_master.is_acmg_sf`
enrichment bullet, a gnomAD PGS-coverage extension (5.5b), and three
finding-005 backfills — while the ROADMAP simultaneously still marked 5.6
unshipped after PR #57 + #59 closed it. This finding records the reset and the
disposition of every item that moved.

## The loader/runner cut

The organizing principle that decides what belongs in Phase 5 vs Phase 6:

- **Phase 5 = downloads-and-loads.** A per-source downloader fetches an
  external release, resolves a version label, parses the artifact, and
  bulk-inserts into its annotation table under the version-pointer pattern.
  Seven such sources: ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog
  metadata, gnomAD filtered, dbSNP filtered.
- **Phase 6 = runs-tools-against-user-variants.** A subprocess tool consumes
  the user's own variants and writes derived rows. Beagle already shipped this
  shape in Phase 4; PharmCAT, HIBAG, plink2, and VEP are its Phase 6 members.

The single Phase-5 deliverable that is neither a downloader nor a runner — the
`variant_annotations_index` rollup refresh (5.7) — closes the phase. It is a
local recompute over already-loaded sources, so it is the natural terminal
step.

## VEP stays in Phase 6 — the closest call

VEP is the one item the cut places against intuition, so it gets the explicit
rationale. The `variant_annotations_index` carries VEP columns
(`most_severe_consequence`, `impact`, `cadd_phred`, `alphamissense_class`),
which tempts treating VEP as a Phase-5 source so the rollup ships fully
populated. It is not one. VEP is a subprocess tool run locally against the
user's variants — structurally identical to PharmCAT and HIBAG, and unlike any
download-and-load source. The schema already encodes this: the
`schema_group_2` coverage-strategy table classifies VEP as **"Computed on user
variants — Run locally via Ensembl VEP CLI; no bulk-load needed."**

Resolution: 5.7 ships the rollup with the VEP columns **NULL** — a documented
gap — and Phase 6's VEP runner backfills them via a later rollup refresh.
Placing VEP in Phase 5 would mean shipping a runner inside the loader phase,
which breaks the cut for the sake of a one-time fully-populated index.

## Disposition of every moved item

| Item (old label) | New home | Reference |
|---|---|---|
| Profile-level QC rollup (5.8) | Phase 6 genome-QC; resolves CLAUDE.md "Real-data observations" #1 | finding-005 #2 |
| `variants_master.is_acmg_sf` population | Phase 6, first task of ACMG SF detection | finding-005 #5 |
| gnomAD PGS coverage extension (5.5b) | Phase 6 follow-up, gated on `pgs_score_weights` | finding-011 |
| dbSNP PGS leg (hypothetical 5.6b) | Phase 6 follow-up, gated on `pgs_score_weights` | finding-016 |
| Canonical REF/ALT, tier-2 rsID, hom-only recovery | Post-5.7 backfills slot (re-derive `variants_master` / `consensus_genotypes`) | finding-005 #1/#4/#6 |
| Genes / traits / pathways dictionaries | Phase 7 (unchanged) | — |
| VEP local runner | Phase 6 (rationale above) | finding-016 #12 |

The two PGS extensions are symmetric: both are append-not-refresh coverage
extensions of an already-loaded source (no version-pointer flip), both gated on
the same `pgs_score_weights` table that lands in Phase 6, both retaining their
own drift sentinel (a zero-delta on added coverage is a mis-wiring signal, not
a success). They were never separable from Phase 6 because the gate is a Phase
6 deliverable.

## Closure shape

Phase 5 = 5.0–5.6 shipped + 5.7 remaining. There is **no 5.7a / 5.7b split and
no 5.8**; 5.7a/5.7b never existed in the repo, and 5.8 is retired here. When
5.7 lands, Phase 5 is complete.

## Residual drift accounting

- **Backend comments** carrying the retired labels were corrected in the same
  PR: `gnomad.py` (5.5b → Phase 6 follow-up, ×2), `consensus.py` (VEP-in-Phase-5
  → post-5.7 backfill + Phase 6 VEP). The `pgs_catalog.py` / `gwas_catalog.py`
  `5.8 → 5.7` edits correct a **write-time mislabel** — the
  `variant_annotations_index` refresh has always been sub-phase 5.7, never 5.8
  — and are not part of the restructure relabel.
- **CHANGELOG history is retained** (entries naming 5.5b/5.8 record what past
  PRs said; the changelog is append-only). The new `[Unreleased]` entry
  documents the relabeling going forward.
- **Schema `is_acmg_sf` references are retained and are not stale**: the
  "populated once group 2 lands" phrasing refers to *schema group 2*, which
  landed in Phase 5; the population *task* runs in Phase 6 and consumes that
  group's ClinVar data. Schema files are immutable here regardless.

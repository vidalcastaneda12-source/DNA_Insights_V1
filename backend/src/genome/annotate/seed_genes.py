"""``genes`` FK-satisfying seed — ACMG SF v3.3 union in-DB PGx/carrier symbols.

PR 6 (ROADMAP "Pre-Phase-6 sequence"). ``genes`` ships empty, but four Phase-6
``derived_*`` tables plus ``pathway_genes`` carry
``gene_symbol VARCHAR NOT NULL REFERENCES genes(gene_symbol)``. With ``genes``
empty every Phase-6 insert into those tables fails the FK. This backfill seeds
the **FK-satisfying gene-symbol subset only**: the set-union of the hand-curated
ACMG SF v3.3 secondary-findings panel (84 genes) and the gene symbols the loaded
CPIC + PharmGKB tables actually carry (the PGx/carrier symbols Phase-6 PharmCAT /
carrier pipelines consume). Full ``genes`` / ``traits`` / ``pathways``
dictionaries + an HGNC bulk loader remain deferred to Phase 7.

**Why a separate file, not a registered loader.** Like
:mod:`genome.annotate.loaders.variant_aliases` and
:mod:`genome.annotate.index_refresh`, this is a standalone ``annotate``
subcommand (``seed-genes``), invoked via lazy import from the CLI rather than
routed through ``register_loader`` / ``refresh --source``. It is deliberately
absent from ``loaders/__init__.py``'s eager side-effect imports (those are the
registered loaders only). It lives under ``annotate/`` because it writes a
reference-annotation table with full provenance.

**Provenance (CLAUDE.md decision #8).** Every ``genes`` row carries
``retrieval_date`` and ``source_version_id`` pointing at a freshly-allocated
``annotation_source_versions`` row under ``source_db='hgnc'`` (already in
:data:`genome.annotate.source_versions.KNOWN_SOURCE_DBS`) with a synthetic
version label. The version row is a *new* id (the ``clinvar.py`` model), **not**
the ``variant_aliases`` model of reusing a sibling's id.

**Supersession (CLAUDE.md decision #7).** ``genes`` is **not** in
:data:`genome.annotate.supersession._SUPERSESSION_TABLES` and is **not** a
registered loader; this is a one-time static FK-satisfying seed, so it correctly
bypasses the version-pointer flip. **No** ``annotation_sources`` pointer for
``hgnc`` is created — the seed writes the version row + the ``genes`` rows and
STOPS (it must NOT call ``flip_to_new_version``). Atomicity is still honored: the
version row is inserted in autocommit, then the bulk INSERT + ``record_count``
backfill sit inside one ``conn.begin()`` block, following ``clinvar.py``'s
autocommit-then-``begin()`` ordering (without its pointer flip),
so a mid-INSERT failure rolls the ``genes`` rows back and a best-effort cleanup
DELETEs the now-orphan version row (finding-015-safe — the rollback discarded the
partial ``genes`` first, so the cleanup DELETE has no FK children).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pyarrow as pa
import structlog

from genome.annotate.source_versions import insert_source_version
from genome.annotate.supersession import commit_and_checkpoint
from genome.db.duckdb_conn import duckdb_connection

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)


SOURCE_DB: Final[str] = "hgnc"
"""``genes`` rows attach to a freshly-allocated ``hgnc`` source-version (decision #8)."""

SEED_VERSION: Final[str] = "acmg_sf_v3.3+pgx_derived"
"""Synthetic version label for the seed's ``annotation_source_versions`` row.

Not a real upstream release identifier — this seed is the union of the ACMG SF
v3.3 panel (the curated half) and the in-DB CPIC/PharmGKB gene symbols (the
derived half). Tests do not assert its exact value; it just has to be stable.
"""

_TARGET_TABLE: Final[str] = "genes"


# ---------------------------------------------------------------------------
# The static ACMG SF v3.3 secondary-findings panel.
#
# Transcribed VERBATIM from the official ACMG SF v3.3 supplementary (84 distinct
# genes = ACMG SF v3.2's 81 + the three v3.3 additions ABCD1, CYP27A1, PLN).
# Source of truth: the official ACMG SF v3.3 supplementary spreadsheet
# (mmc1.xlsx, doi:10.1016/j.gim.2025.101454). gene_symbol, inheritance, and the
# per-gene `SF List Version` (the version each gene was *added* in) are verbatim;
# only the umbrella disease label is editorial (multi-phenotype genes collapsed
# to one label, since genes.gene_symbol is the PK). Disease strings preserve
# embedded commas exactly (e.g. "Ehlers-Danlos syndrome, vascular type").
#
# Tuple layout: (gene_symbol, acmg_sf_disease, acmg_sf_inheritance, acmg_sf_version).
# All 84 carry is_acmg_sf=TRUE.
# ---------------------------------------------------------------------------

_ACMG_SF_V3_3: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("ABCD1", "X-linked adrenoleukodystrophy", "XL", "v3.3"),
    ("ACTA2", "Familial thoracic aortic aneurysm and dissection", "AD", "v1.0"),
    ("ACTC1", "Hypertrophic cardiomyopathy", "AD", "v1.0"),
    ("ACVRL1", "Hereditary hemorrhagic telangiectasia", "AD", "v3.0"),
    ("APC", "Familial adenomatous polyposis", "AD", "v1.0"),
    ("APOB", "Familial hypercholesterolemia", "AD", "v1.0"),
    ("ATP7B", "Wilson disease", "AR", "v2.0"),
    ("BAG3", "Dilated cardiomyopathy / myofibrillar myopathy", "AD", "v3.1"),
    ("BMPR1A", "Juvenile polyposis syndrome", "AD", "v1.0"),
    ("BRCA1", "Hereditary breast and ovarian cancer", "AD", "v1.0"),
    ("BRCA2", "Hereditary breast and ovarian cancer", "AD", "v1.0"),
    ("BTD", "Biotinidase deficiency", "AR", "v3.0"),
    ("CACNA1S", "Malignant hyperthermia susceptibility", "AD", "v1.0"),
    (
        "CALM1",
        "Long QT syndrome / catecholaminergic polymorphic ventricular tachycardia",
        "AD",
        "v3.2",
    ),
    (
        "CALM2",
        "Long QT syndrome / catecholaminergic polymorphic ventricular tachycardia",
        "AD",
        "v3.2",
    ),
    (
        "CALM3",
        "Long QT syndrome / catecholaminergic polymorphic ventricular tachycardia",
        "AD",
        "v3.2",
    ),
    ("CASQ2", "Catecholaminergic polymorphic ventricular tachycardia", "AR", "v3.0"),
    ("COL3A1", "Ehlers-Danlos syndrome, vascular type", "AD", "v1.0"),
    ("CYP27A1", "Cerebrotendinous xanthomatosis", "AR", "v3.3"),
    ("DES", "Dilated cardiomyopathy / myofibrillar myopathy", "AD", "v3.1"),
    ("DSC2", "Arrhythmogenic right ventricular cardiomyopathy", "AD", "v1.0"),
    ("DSG2", "Arrhythmogenic right ventricular cardiomyopathy", "AD", "v1.0"),
    (
        "DSP",
        "Arrhythmogenic right ventricular cardiomyopathy / dilated cardiomyopathy",
        "AD",
        "v1.0",
    ),
    ("ENG", "Hereditary hemorrhagic telangiectasia", "AD", "v3.0"),
    ("FBN1", "Marfan syndrome", "AD", "v1.0"),
    (
        "FLNC",
        "Dilated/hypertrophic cardiomyopathy / myofibrillar myopathy",
        "AD",
        "v3.0",
    ),
    ("GAA", "Pompe disease", "AR", "v3.0"),
    ("GLA", "Fabry disease", "XL", "v1.0"),
    ("HFE", "Hereditary hemochromatosis", "AR", "v3.0"),
    ("HNF1A", "Maturity-onset diabetes of the young", "AD", "v3.0"),
    ("KCNH2", "Long QT syndrome", "AD", "v1.0"),
    ("KCNQ1", "Long QT syndrome", "AD", "v1.0"),
    ("LDLR", "Familial hypercholesterolemia", "AD", "v1.0"),
    ("LMNA", "Dilated cardiomyopathy", "AD", "v1.0"),
    ("MAX", "Hereditary paraganglioma-pheochromocytoma syndrome", "AD", "v3.0"),
    ("MEN1", "Multiple endocrine neoplasia type 1", "AD", "v1.0"),
    ("MLH1", "Lynch syndrome", "AD", "v1.0"),
    ("MSH2", "Lynch syndrome", "AD", "v1.0"),
    ("MSH6", "Lynch syndrome", "AD", "v1.0"),
    ("MUTYH", "MUTYH-associated polyposis", "AR", "v1.0"),
    ("MYBPC3", "Hypertrophic cardiomyopathy", "AD", "v1.0"),
    ("MYH11", "Familial thoracic aortic aneurysm and dissection", "AD", "v1.0"),
    ("MYH7", "Hypertrophic / dilated cardiomyopathy", "AD", "v1.0"),
    ("MYL2", "Hypertrophic cardiomyopathy", "AD", "v1.0"),
    ("MYL3", "Hypertrophic cardiomyopathy", "AD", "v1.0"),
    ("NF2", "NF2-related schwannomatosis", "AD", "v1.0"),
    ("OTC", "Ornithine transcarbamylase deficiency", "XL", "v2.0"),
    ("PALB2", "Hereditary breast cancer", "AD", "v3.0"),
    ("PCSK9", "Familial hypercholesterolemia", "AD", "v1.0"),
    ("PKP2", "Arrhythmogenic right ventricular cardiomyopathy", "AD", "v1.0"),
    (
        "PLN",
        "Dilated cardiomyopathy / arrhythmogenic cardiomyopathy",
        "AD",
        "v3.3",
    ),
    ("PMS2", "Lynch syndrome", "AD", "v1.0"),
    ("PRKAG2", "Hypertrophic cardiomyopathy", "AD", "v1.0"),
    ("PTEN", "PTEN hamartoma tumor syndrome", "AD", "v1.0"),
    ("RB1", "Retinoblastoma", "AD", "v1.0"),
    ("RBM20", "Dilated cardiomyopathy", "AD", "v3.1"),
    (
        "RET",
        "Multiple endocrine neoplasia type 2 / familial medullary thyroid cancer",
        "AD",
        "v1.0",
    ),
    ("RPE65", "RPE65-related retinopathy", "AR", "v3.0"),
    ("RYR1", "Malignant hyperthermia susceptibility", "AD", "v1.0"),
    ("RYR2", "Catecholaminergic polymorphic ventricular tachycardia", "AD", "v1.0"),
    ("SCN5A", "Long QT syndrome type 3 / Brugada syndrome", "AD", "v1.0"),
    ("SDHAF2", "Hereditary paraganglioma-pheochromocytoma syndrome", "AD", "v1.0"),
    ("SDHB", "Hereditary paraganglioma-pheochromocytoma syndrome", "AD", "v1.0"),
    ("SDHC", "Hereditary paraganglioma-pheochromocytoma syndrome", "AD", "v1.0"),
    ("SDHD", "Hereditary paraganglioma-pheochromocytoma syndrome", "AD", "v1.0"),
    ("SMAD3", "Loeys-Dietz syndrome", "AD", "v1.0"),
    (
        "SMAD4",
        "Juvenile polyposis syndrome / hereditary hemorrhagic telangiectasia",
        "AD",
        "v1.0",
    ),
    ("STK11", "Peutz-Jeghers syndrome", "AD", "v1.0"),
    ("TGFBR1", "Loeys-Dietz syndrome", "AD", "v1.0"),
    ("TGFBR2", "Loeys-Dietz syndrome", "AD", "v1.0"),
    ("TMEM127", "Hereditary paraganglioma-pheochromocytoma syndrome", "AD", "v3.0"),
    ("TMEM43", "Arrhythmogenic right ventricular cardiomyopathy", "AD", "v1.0"),
    ("TNNC1", "Dilated cardiomyopathy", "AD", "v3.1"),
    ("TNNI3", "Hypertrophic cardiomyopathy", "AD", "v1.0"),
    ("TNNT2", "Dilated / hypertrophic cardiomyopathy", "AD", "v1.0"),
    ("TP53", "Li-Fraumeni syndrome", "AD", "v1.0"),
    ("TPM1", "Hypertrophic cardiomyopathy", "AD", "v1.0"),
    (
        "TRDN",
        "Catecholaminergic polymorphic ventricular tachycardia / long QT syndrome",
        "AR",
        "v3.0",
    ),
    ("TSC1", "Tuberous sclerosis complex", "AD", "v1.0"),
    ("TSC2", "Tuberous sclerosis complex", "AD", "v1.0"),
    ("TTN", "Dilated cardiomyopathy (truncating variants only)", "AD", "v3.0"),
    ("TTR", "Hereditary transthyretin-related amyloidosis", "AD", "v3.1"),
    ("VHL", "Von Hippel-Lindau syndrome", "AD", "v1.0"),
    ("WT1", "WT1-related Wilms tumor", "AD", "v1.0"),
)
"""The 84-gene ACMG SF v3.3 panel, verbatim from the verified CSV."""


# The genes column order the Arrow stage table + INSERT ... SELECT use. Only the
# FK-satisfying columns plus the ACMG/PGx flags + provenance are populated; every
# other genes column (ensembl_gene_id, chrom, start/end_grch38, strand,
# gene_type, description, omim_id, uniprot_id, entrez_gene_id, hgnc_id,
# is_haploinsufficient) stays NULL — the Phase-7 dictionary backfills those.
_INSERT_COLUMNS: Final[tuple[str, ...]] = (
    "gene_symbol",
    "is_acmg_sf",
    "acmg_sf_disease",
    "acmg_sf_inheritance",
    "acmg_sf_version",
    "is_pgx_relevant",
    "source_version_id",
    "retrieval_date",
)

_ARROW_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("gene_symbol", pa.string(), nullable=False),
        pa.field("is_acmg_sf", pa.bool_(), nullable=False),
        pa.field("acmg_sf_disease", pa.string()),
        pa.field("acmg_sf_inheritance", pa.string()),
        pa.field("acmg_sf_version", pa.string()),
        pa.field("is_pgx_relevant", pa.bool_(), nullable=False),
        pa.field("source_version_id", pa.int64(), nullable=False),
        pa.field("retrieval_date", pa.timestamp("us"), nullable=False),
    ],
)

# The five FK dependents of genes(gene_symbol) — the four Phase-6 derived_*
# tables AND pathway_genes (the auditor-flagged 5th; the stale "genes is a leaf"
# note in finding-020 missed it). The --force leaf-check enumerates all five.
_FK_DEPENDENTS: Final[tuple[str, ...]] = (
    "derived_pgx_phenotypes",
    "derived_carrier_findings",
    "derived_acmg_sf_findings",
    "derived_compound_het",
    "pathway_genes",
)


# ---------------------------------------------------------------------------
# Errors + result.
# ---------------------------------------------------------------------------


class GeneSeedCoverageError(RuntimeError):
    """Raised when the post-INSERT coverage gate finds an uncovered PGx symbol.

    The PGx half of the seed is derived *from* the current CPIC + PharmGKB
    tables, so ``(DISTINCT cpic/pharmgkb gene_symbol) EXCEPT (genes)`` is 0 by
    construction. A non-zero residual means the build/INSERT wiring lost a
    symbol — a bug, not a data condition. Raised INSIDE the ``begin()``
    transaction (after ``_insert_genes`` + the ``record_count`` UPDATE, before
    ``commit_and_checkpoint``) so it falls through to the ``except`` →
    rollback + cleanup and never commits a half-seed.
    """


class GenesNotLeafError(RuntimeError):
    """Raised by the ``force`` leaf-check when ``genes`` still has FK children.

    ``genes`` has five FK dependents (the four ``derived_*`` tables plus
    ``pathway_genes``). ``force=True`` would ``DELETE FROM genes`` and re-seed; if
    any dependent still references ``genes`` the DELETE would either fail the FK
    or strand those rows. Rather than silently delete, the force path RAISES so a
    re-seed after Phase-6 rows exist is a loud, deliberate operation.
    """


@dataclass(frozen=True, slots=True)
class GeneSeedResult:
    """Outcome of one :func:`seed_genes` call.

    ``genes_rows`` is the total seeded; ``acmg_sf_genes`` / ``pgx_genes`` are the
    flag counts (a symbol in both is counted in each). ``cpic_uncovered`` /
    ``pharmgkb_uncovered`` are the two ``EXCEPT``-probe cardinalities (0 by
    construction on a fresh seed — the PGx half is derived from the same
    current-version-scoped query); ``cpic_covered`` / ``pharmgkb_covered`` are
    their booleans. On the idempotent ``already_populated=True`` early return the
    probes are **not** re-run: these four fields are reported as covered/0 by
    contract, not as a live re-measurement of the existing seed's coverage. A
    cpic/pharmgkb refresh landing *after* the seed is detected at Phase-6 entry,
    not here (see the plan's pre-mortem #1).
    """

    source_version_id: int
    already_populated: bool
    genes_rows: int
    acmg_sf_genes: int
    pgx_genes: int
    cpic_covered: bool
    pharmgkb_covered: bool
    cpic_uncovered: int
    pharmgkb_uncovered: int


# ---------------------------------------------------------------------------
# PGx/carrier symbol derivation (current-version-scoped).
# ---------------------------------------------------------------------------


def _derive_pgx_symbols(conn: DuckDBPyConnection) -> set[str]:
    """Return the DISTINCT gene symbols in the *current* CPIC + PharmGKB sets.

    Each leg joins the source table to its ``annotation_sources`` pointer so only
    the currently-active release's rows participate (a superseded older version's
    symbols do not leak in). PharmGKB's ``gene_symbol`` is nullable, so that leg
    filters ``gene_symbol IS NOT NULL``. Casing is taken verbatim — this is
    exactly what the Phase-6 PharmCAT / carrier pipelines read, which is what
    makes the seed match at Phase-6 runtime (the coverage gate's invariant).
    """
    rows = conn.execute(
        """
        SELECT DISTINCT cg.gene_symbol
          FROM cpic_guidelines cg
          JOIN annotation_sources s
            ON s.source_db = 'cpic'
           AND s.current_source_version_id = cg.source_version_id
        UNION
        SELECT DISTINCT pa.gene_symbol
          FROM pharmgkb_annotations pa
          JOIN annotation_sources s
            ON s.source_db = 'pharmgkb'
           AND s.current_source_version_id = pa.source_version_id
         WHERE pa.gene_symbol IS NOT NULL
        """,
    ).fetchall()
    return {str(symbol) for (symbol,) in rows if symbol is not None}


# ---------------------------------------------------------------------------
# Row-set construction (before any write).
# ---------------------------------------------------------------------------


def _build_rows(
    pgx_symbols: set[str],
    *,
    source_version_id: int,
    retrieval_date: datetime,
) -> list[dict[str, object]]:
    """Merge the static ACMG panel with the derived PGx symbols into genes rows.

    Set-union on ``gene_symbol``: an ACMG gene carries ``is_acmg_sf=TRUE`` + its
    disease/inheritance/version; a PGx-derived symbol carries
    ``is_pgx_relevant=TRUE``; a symbol in both carries BOTH flags + the ACMG
    metadata. ``is_acmg_sf`` / ``is_pgx_relevant`` are always explicit TRUE/FALSE
    (never NULL). Output is sorted by ``gene_symbol`` so the row order — and thus
    the payload hash (pre-mortem #3) — is deterministic across runs.
    """
    acmg_by_symbol: dict[str, tuple[str, str, str, str]] = {
        symbol: (symbol, disease, inheritance, version)
        for symbol, disease, inheritance, version in _ACMG_SF_V3_3
    }
    all_symbols = sorted(set(acmg_by_symbol) | pgx_symbols)

    naive_retrieval = retrieval_date.astimezone(UTC).replace(tzinfo=None)
    rows: list[dict[str, object]] = []
    for symbol in all_symbols:
        acmg = acmg_by_symbol.get(symbol)
        is_acmg = acmg is not None
        rows.append(
            {
                "gene_symbol": symbol,
                "is_acmg_sf": is_acmg,
                "acmg_sf_disease": acmg[1] if acmg is not None else None,
                "acmg_sf_inheritance": acmg[2] if acmg is not None else None,
                "acmg_sf_version": acmg[3] if acmg is not None else None,
                "is_pgx_relevant": symbol in pgx_symbols,
                "source_version_id": source_version_id,
                "retrieval_date": naive_retrieval,
            },
        )
    return rows


def _canonical_payload(rows: list[dict[str, object]]) -> bytes:
    """Serialise the seed to a byte-stable canonical payload for hashing.

    Hashes only the *content* columns (gene_symbol + flags + ACMG metadata), NOT
    the per-run ``source_version_id`` / ``retrieval_date`` — those differ every
    run by design and would make the hash useless as a same-content identifier.
    ``rows`` is already sorted by ``gene_symbol`` (:func:`_build_rows`), so the
    JSON is deterministic; ``sort_keys=True`` pins intra-row key order too. This
    closes pre-mortem #3 (a set-iteration-order hash would be unstable).
    """
    content = [
        {
            "gene_symbol": r["gene_symbol"],
            "is_acmg_sf": r["is_acmg_sf"],
            "acmg_sf_disease": r["acmg_sf_disease"],
            "acmg_sf_inheritance": r["acmg_sf_inheritance"],
            "acmg_sf_version": r["acmg_sf_version"],
            "is_pgx_relevant": r["is_pgx_relevant"],
        }
        for r in rows
    ]
    return json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Bulk insert (PyArrow Table registration + INSERT ... SELECT).
# ---------------------------------------------------------------------------


def _insert_genes(conn: DuckDBPyConnection, rows: list[dict[str, object]]) -> int:
    """Bulk-insert the seed rows into ``genes`` via PyArrow + INSERT...SELECT.

    The project's locked bulk-load convention (CLAUDE.md — never ``executemany``),
    mirroring ``variant_aliases._insert_batch``. ``genes.gene_symbol`` is the PK,
    so no surrogate id is allocated. Returns the number of rows inserted. This is
    the monkeypatchable seam tests override to force a mid-INSERT failure; it is
    called after ``insert_source_version`` (autocommit) and inside
    ``conn.begin()``.
    """
    if not rows:
        return 0
    n = len(rows)
    table_data: dict[str, pa.Array] = {
        "gene_symbol": pa.array([r["gene_symbol"] for r in rows], type=pa.string()),
        "is_acmg_sf": pa.array([r["is_acmg_sf"] for r in rows], type=pa.bool_()),
        "acmg_sf_disease": pa.array([r["acmg_sf_disease"] for r in rows], type=pa.string()),
        "acmg_sf_inheritance": pa.array(
            [r["acmg_sf_inheritance"] for r in rows],
            type=pa.string(),
        ),
        "acmg_sf_version": pa.array([r["acmg_sf_version"] for r in rows], type=pa.string()),
        "is_pgx_relevant": pa.array([r["is_pgx_relevant"] for r in rows], type=pa.bool_()),
        "source_version_id": pa.array(
            [r["source_version_id"] for r in rows],
            type=pa.int64(),
        ),
        "retrieval_date": pa.array(
            [r["retrieval_date"] for r in rows],
            type=pa.timestamp("us"),
        ),
    }
    table = pa.table(table_data, schema=_ARROW_SCHEMA)
    columns = ", ".join(_INSERT_COLUMNS)
    try:
        conn.register("_genes_seed_stage_arrow", table)
        conn.execute(
            f"""
            INSERT INTO {_TARGET_TABLE} ({columns})
            SELECT {columns}
              FROM _genes_seed_stage_arrow
            """,  # noqa: S608 — table + column lists are module constants
        )
    finally:
        conn.unregister("_genes_seed_stage_arrow")
    return n


# ---------------------------------------------------------------------------
# Coverage gate + counts.
# ---------------------------------------------------------------------------


def _count_uncovered(conn: DuckDBPyConnection, source_db: str) -> int:
    """Cardinality of ``(current source_db gene_symbol) EXCEPT (genes.gene_symbol)``.

    0 by construction once the seed is in place (the PGx half is derived from the
    same current-version-scoped query). ``source_db`` is one of ``'cpic'`` /
    ``'pharmgkb'`` — a literal supplied by the two call sites, not user input. The
    PharmGKB leg filters ``gene_symbol IS NOT NULL`` to mirror the derivation.
    """
    not_null = " AND src.gene_symbol IS NOT NULL" if source_db == "pharmgkb" else ""
    table = "cpic_guidelines" if source_db == "cpic" else "pharmgkb_annotations"
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT src.gene_symbol
              FROM {table} src
              JOIN annotation_sources s
                ON s.source_db = ?
               AND s.current_source_version_id = src.source_version_id
             WHERE TRUE{not_null}
            EXCEPT
            SELECT gene_symbol FROM {_TARGET_TABLE}
        )
        """,  # noqa: S608 — table name + NOT NULL clause are module-controlled literals
        [source_db],
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _existing_counts(conn: DuckDBPyConnection) -> tuple[int, int, int, int | None]:
    """Return ``(genes_rows, acmg_sf_genes, pgx_genes, source_version_id)`` for the live table.

    Used by the idempotent early-return path so the result reports the *existing*
    seed's shape. ``source_version_id`` is the MAX over the table (the seed writes
    one id for every row, so MAX == that id); ``None`` only when the table is
    empty (which the caller never reaches on this path).
    """
    row = conn.execute(
        f"""
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE is_acmg_sf),
            COUNT(*) FILTER (WHERE is_pgx_relevant),
            MAX(source_version_id)
          FROM {_TARGET_TABLE}
        """,  # noqa: S608 — _TARGET_TABLE is a module constant
    ).fetchone()
    if row is None:
        return (0, 0, 0, None)
    svid = None if row[3] is None else int(row[3])
    return (int(row[0]), int(row[1]), int(row[2]), svid)


# ---------------------------------------------------------------------------
# Rollback helper.
# ---------------------------------------------------------------------------


def _cleanup_orphan_version_row(
    conn: DuckDBPyConnection,
    source_version_id: int,
) -> None:
    """Best-effort delete of an orphan ``annotation_source_versions`` row.

    Same shape as the ClinVar / PharmGKB / CPIC helpers — called when the
    INSERT transaction rolls back so the version row that
    :func:`insert_source_version` committed in autocommit doesn't leave a
    dangling "version exists but zero genes reference it" state. The DELETE is
    FK-safe because the rolled-back transaction already discarded every partial
    ``genes`` row, so nothing references the new ``source_version_id``. Failures
    are swallowed and logged; the caller is already re-raising the original
    exception.
    """
    try:
        conn.execute(
            "DELETE FROM annotation_source_versions WHERE source_version_id = ?",
            [source_version_id],
        )
    except Exception:  # noqa: BLE001 — best-effort cleanup; original exc re-raised by caller
        logger.warning(
            "genes.seed.cleanup_orphan_version_row_delete_failed",
            source_version_id=source_version_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Force-path leaf check.
# ---------------------------------------------------------------------------


def _assert_genes_is_leaf(conn: DuckDBPyConnection) -> None:
    """Raise :class:`GenesNotLeafError` if any of the five FK dependents has a row.

    Enumerates all five tables that carry ``REFERENCES genes(gene_symbol)`` (the
    four ``derived_*`` tables plus ``pathway_genes``). A non-empty dependent means
    a ``DELETE FROM genes`` would either fail the FK or strand those rows, so the
    force path refuses rather than silently deleting (pre-mortem #2).
    """
    non_empty: list[str] = []
    for table in _FK_DEPENDENTS:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 — module constant
        if row is not None and int(row[0]) > 0:
            non_empty.append(f"{table}={int(row[0])}")
    if non_empty:
        msg = (
            "cannot --force re-seed genes: the following FK dependents still "
            f"reference genes(gene_symbol): {', '.join(non_empty)}. Supersede or "
            "clear those rows first."
        )
        raise GenesNotLeafError(msg)


# ---------------------------------------------------------------------------
# Top-level entrypoint.
# ---------------------------------------------------------------------------


def seed_genes(
    conn: DuckDBPyConnection | None = None,
    *,
    force: bool = False,
) -> GeneSeedResult:
    """Seed ``genes`` with the ACMG SF v3.3 panel union the in-DB PGx/carrier symbols.

    Pipeline:

    1. Idempotency: if ``COUNT(genes) > 0`` and not ``force``, short-circuit and
       return ``already_populated=True`` with the existing seed's counts +
       ``source_version_id``. No ``insert_source_version`` is called on this path,
       so an idempotent re-run leaves no orphan version row.
    2. Build the row set **before any write**: derive the current-version-scoped
       CPIC + PharmGKB symbols, set-union with the static ACMG panel, capture
       ``retrieval_date = datetime.now(UTC)`` once, and compute the byte-stable
       payload hash over the sorted content.
    3. ``insert_source_version`` under ``source_db='hgnc'`` in **autocommit**
       (the ClinVar model: a new ``source_version_id``, NOT the ``variant_aliases``
       reuse-a-sibling's-id model), with ``record_count=None``.
    4. ``conn.begin()`` → (if ``force``: assert ``genes`` is a leaf across all five
       FK dependents, raise :class:`GenesNotLeafError` if not, then
       ``DELETE FROM genes``) → ``_insert_genes`` → ``UPDATE
       annotation_source_versions SET record_count`` → compute the two coverage
       ``EXCEPT`` probes and raise :class:`GeneSeedCoverageError` if either is
       non-zero → ``commit_and_checkpoint``. On any exception: ``conn.rollback()``
       THEN ``_cleanup_orphan_version_row`` (best-effort); re-raise. The pointer is
       **not** flipped — ``genes`` is a one-time static seed, not an evolving
       source (decision #7).

    ``conn`` defaults to a fresh read-write connection; a borrowed conn is left
    open for the caller (tests). ``force=True`` DELETEs and re-seeds under a fresh
    ``source_version_id``.
    """
    ctx: contextlib.AbstractContextManager[DuckDBPyConnection] = (
        duckdb_connection() if conn is None else contextlib.nullcontext(conn)
    )
    with ctx as active_conn:
        existing_row = active_conn.execute(
            f"SELECT COUNT(*) FROM {_TARGET_TABLE}",  # noqa: S608 — module constant
        ).fetchone()
        existing = int(existing_row[0]) if existing_row is not None else 0

        if existing > 0 and not force:
            genes_rows, acmg_genes, pgx_genes, existing_svid = _existing_counts(active_conn)
            logger.info(
                "genes.seed.skip_already_populated",
                genes_rows=genes_rows,
                source_version_id=existing_svid,
            )
            # Coverage is NOT re-probed on the idempotent path: the existing seed
            # passed the gate when it was written, and re-validating a post-seed
            # cpic/pharmgkb refresh is a Phase-6-entry concern (plan pre-mortem #1),
            # not this short-circuit's job. The four coverage fields below are
            # reported as covered/0 by contract, not measured here.
            return GeneSeedResult(
                source_version_id=existing_svid if existing_svid is not None else 0,
                already_populated=True,
                genes_rows=genes_rows,
                acmg_sf_genes=acmg_genes,
                pgx_genes=pgx_genes,
                cpic_covered=True,
                pharmgkb_covered=True,
                cpic_uncovered=0,
                pharmgkb_uncovered=0,
            )

        # 2. Build the row set BEFORE any write.
        pgx_symbols = _derive_pgx_symbols(active_conn)
        retrieval_date = datetime.now(UTC)
        log = logger.bind(source_db=SOURCE_DB, force=force)
        log.info("genes.seed.symbols_derived", pgx_symbols=len(pgx_symbols))

        # 3. Version row in AUTOCOMMIT (clinvar-exact). new_svid captured before
        #    begin() so the cleanup helper can target it on rollback.
        # Build rows once against the new id; the payload hash is over the
        # id-independent content, so it is byte-stable across runs.
        rows_preview = _build_rows(
            pgx_symbols,
            source_version_id=0,
            retrieval_date=retrieval_date,
        )
        payload = _canonical_payload(rows_preview)
        source_file_hash = hashlib.sha256(payload).hexdigest()
        source_file_size = len(payload)

        new_svid = insert_source_version(
            active_conn,
            source_db=SOURCE_DB,
            version=SEED_VERSION,
            source_url=None,
            source_file_hash=source_file_hash,
            source_file_size=source_file_size,
            record_count=None,
            notes="genes FK-satisfying seed: ACMG SF v3.3 + in-DB CPIC/PharmGKB symbols (PR 6)",
        )

        rows = _build_rows(
            pgx_symbols,
            source_version_id=new_svid,
            retrieval_date=retrieval_date,
        )

        # 4. Single-transaction load (clinvar-exact rollback-then-cleanup).
        active_conn.begin()
        try:
            if force and existing > 0:
                _assert_genes_is_leaf(active_conn)
                active_conn.execute(f"DELETE FROM {_TARGET_TABLE}")  # noqa: S608 — module constant
                log.info("genes.seed.cleared_for_reseed", removed_rows=existing)

            inserted = _insert_genes(active_conn, rows)
            active_conn.execute(
                "UPDATE annotation_source_versions SET record_count = ? "
                "WHERE source_version_id = ?",
                [inserted, new_svid],
            )

            cpic_uncovered = _count_uncovered(active_conn, "cpic")
            pharmgkb_uncovered = _count_uncovered(active_conn, "pharmgkb")
            if cpic_uncovered != 0 or pharmgkb_uncovered != 0:
                msg = (
                    "genes seed coverage gate failed (wiring bug): "
                    f"cpic_uncovered={cpic_uncovered} pharmgkb_uncovered={pharmgkb_uncovered}; "
                    "the derived PGx half should cover both by construction."
                )
                # TRY301 suppressed: the raise must fire INSIDE the try so it
                # falls through to the except -> rollback + orphan-version
                # cleanup; that pre-commit abort is the whole point of the gate
                # (a wiring bug must never commit a half-seed), so it cannot move
                # to a helper.
                raise GeneSeedCoverageError(msg)  # noqa: TRY301

            commit_and_checkpoint(active_conn, source_name=_TARGET_TABLE)
        except Exception:
            active_conn.rollback()
            _cleanup_orphan_version_row(active_conn, new_svid)
            raise

        acmg_count = sum(1 for r in rows if r["is_acmg_sf"])
        pgx_count = sum(1 for r in rows if r["is_pgx_relevant"])
        log.info(
            "genes.seed.complete",
            source_version_id=new_svid,
            genes_rows=inserted,
            acmg_sf_genes=acmg_count,
            pgx_genes=pgx_count,
            cpic_covered=cpic_uncovered == 0,
            pharmgkb_covered=pharmgkb_uncovered == 0,
        )
        return GeneSeedResult(
            source_version_id=new_svid,
            already_populated=False,
            genes_rows=inserted,
            acmg_sf_genes=acmg_count,
            pgx_genes=pgx_count,
            cpic_covered=cpic_uncovered == 0,
            pharmgkb_covered=pharmgkb_uncovered == 0,
            cpic_uncovered=cpic_uncovered,
            pharmgkb_uncovered=pharmgkb_uncovered,
        )


__all__ = [
    "SEED_VERSION",
    "SOURCE_DB",
    "GeneSeedCoverageError",
    "GeneSeedResult",
    "GenesNotLeafError",
    "seed_genes",
]

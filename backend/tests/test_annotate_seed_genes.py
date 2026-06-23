"""Tests for :mod:`genome.annotate.seed_genes` — the minimal ``genes`` seed (PR 6).

Plan-blind spec source: ``docs/plans/pr-6-genes-seed.md`` §5 (test list) + §6
(verification / expected values), plus the frozen interface contract handed to the
Stage-2 test-author and the DDL NOT-NULL/FK shapes in ``ddl/group_2_annotations.sql``
(``genes`` :374-409, ``annotation_sources`` :33-36) and ``ddl/group_3_derived.sql``
(``analysis_runs`` :9-39, ``derived_acmg_sf_findings`` :190-216, ``derived_compound_het``
:426-449, ``derived_pgx_phenotypes`` :112, ``derived_carrier_findings`` :155).

Every expected value here is derived from the plan spec + the DDL, never from the
implementation body of ``seed_genes.py`` (which is authored concurrently and is NOT read
by this file — see ``docs/runbooks/verification.md`` on the test-mutation failure mode).

Harness conventions mirror ``test_loaders_variant_aliases.py`` (the autouse ``_isolated``
fixture wrapping ``isolated_settings``; ``init_databases()`` + ``duckdb_connection()``;
``'1'::chromosome_enum`` seeds; ``insert_source_version`` + ``flip_to_new_version`` to
establish a current pointer; ``CliRunner().invoke(app, [...])`` for the CLI surface) and
the clinvar rollback test (``monkeypatch.setattr(<module>, "<bulk-insert fn>", _explode)``
then assert rollback + zero orphan version rows).
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import duckdb
import pytest
import structlog
from typer.testing import CliRunner

from genome.annotate.seed_genes import (
    GeneSeedResult,
    GenesNotLeafError,
    seed_genes,
)
from genome.annotate.source_versions import insert_source_version
from genome.annotate.supersession import flip_to_new_version
from genome.cli import app
from genome.db import duckdb_connection, init_databases

if TYPE_CHECKING:
    from collections.abc import Iterator

    from duckdb import DuckDBPyConnection

# Resolve the module object explicitly: ``genome.annotate.__init__`` re-exports the
# ``seed_genes`` *function*, which shadows the submodule name, so a plain
# ``import genome.annotate.seed_genes as ...`` would bind the function, not the module.
# ``importlib.import_module`` returns the module so the ``_insert_genes`` monkeypatch seam
# (frozen interface contract) can be patched on it.
seed_genes_module = importlib.import_module("genome.annotate.seed_genes")


# ---------------------------------------------------------------------------
# Spec-locked expected values (from plan section 6 + the verified ACMG panel)
# ---------------------------------------------------------------------------
#
# Fresh init_databases() leaves cpic_guidelines / pharmgkb_annotations EMPTY, so on a
# fresh DB seed_genes seeds exactly the 84-gene ACMG SF v3.3 panel and nothing else.
# The fresh-DB GeneSeedResult is therefore: genes_rows 84, acmg_sf_genes 84, pgx_genes 0,
# cpic_covered/pharmgkb_covered True, cpic_uncovered/pharmgkb_uncovered 0,
# already_populated False (first run on an empty genes table).
#
# 84 = the verified ACMG SF v3.3 panel size (plan Dataset: 81 of v3.2 plus the three v3.3
# additions ABCD1, CYP27A1, PLN; none removed).
_EXPECTED_FRESH_ACMG_GENES = 84

# A gene known to be in the ACMG panel (pr-6-acmg-sf-v3.3-genes.csv row for BRCA1).
_SEEDED_ACMG_GENE = "BRCA1"

# A symbol that is in neither the ACMG panel nor any seeded PGx table.
_UNSEEDED_GENE = "ZZZ_NOT_A_GENE"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated(
    isolated_settings: dict[str, str],  # noqa: ARG001 — activates the tmp-dir settings
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Mirror the variant_aliases autouse isolation: tmp settings + structlog reset."""
    yield
    structlog.reset_defaults()
    monkeypatch.undo()


def _insert_variant(conn: DuckDBPyConnection) -> int:
    """Insert one ``variants_master`` row and return its allocated ``variant_id``.

    ``variant_id`` carries ``DEFAULT nextval('variant_id_seq')`` (group_1), so we omit
    it and capture the surrogate key via ``RETURNING``.
    """
    row = conn.execute(
        """
        INSERT INTO variants_master (chrom, pos_grch38, ref_allele, alt_allele, rsid)
        VALUES ('1'::chromosome_enum, 1000, 'A', 'C', 'rs1')
        RETURNING variant_id
        """,
    ).fetchone()
    assert row is not None
    return int(row[0])


def _insert_analysis_run(conn: DuckDBPyConnection, run_id: int = 1) -> int:
    """Insert one ``analysis_runs`` row with all NOT-NULLs and return its id.

    NOT NULLs (ddl/group_3_derived.sql:9-39): ``analysis_run_id`` (bare PK, no default —
    supplied), ``analysis_type``, ``method``, ``method_version``, ``pipeline_version``;
    ``status`` has a DEFAULT.
    """
    conn.execute(
        """
        INSERT INTO analysis_runs (
            analysis_run_id, analysis_type, method, method_version, pipeline_version
        )
        VALUES (?, 'acmg_sf', 'acmg_detector', '1.0', 'phase6.0')
        """,
        [run_id],
    )
    return run_id


def _insert_acmg_finding(
    conn: DuckDBPyConnection,
    *,
    derived_id: int,
    analysis_run_id: int,
    gene_symbol: str,
    variant_id: int,
) -> None:
    """Insert one ``derived_acmg_sf_findings`` row with all co-required NOT-NULLs.

    NOT NULLs (ddl/group_3_derived.sql:190-216): ``derived_acmg_id`` (PK),
    ``analysis_run_id``, ``gene_symbol`` (→ genes FK), ``acmg_sf_version``, ``disease``,
    ``variant_id`` (→ variants_master FK). ``gene_symbol`` is the only free FK variable;
    a valid ``analysis_run_id`` + ``variant_id`` are supplied so that when ``gene_symbol``
    is unseeded the FK that fires is provably the ``genes`` one.
    """
    conn.execute(
        """
        INSERT INTO derived_acmg_sf_findings (
            derived_acmg_id, analysis_run_id, gene_symbol,
            acmg_sf_version, disease, variant_id
        )
        VALUES (?, ?, ?, 'v3.3', 'Test disease', ?)
        """,
        [derived_id, analysis_run_id, gene_symbol, variant_id],
    )


def _seed_pgx_source_with_gene(
    conn: DuckDBPyConnection,
    *,
    source_db: str,
    table: str,
    gene_symbol: str,
) -> int:
    """Establish a current version pointer for a PGx source and seed one gene row.

    Mirrors the variant_aliases ``_seed_dbsnp_version`` pattern: allocate a real
    ``annotation_source_versions`` row, ``flip_to_new_version`` so the source is current,
    then INSERT one ``cpic_guidelines`` / ``pharmgkb_annotations`` row carrying
    ``gene_symbol`` under that svid. Returns the new svid.
    """
    svid = insert_source_version(
        conn,
        source_db=source_db,
        version="test-1",
        source_url=None,
        source_file_hash=f"{source_db}_test",
        source_file_size=0,
        record_count=1,
    )
    flip_to_new_version(
        conn,
        source=source_db,
        table=table,
        new_source_version_id=svid,
    )
    retrieval = datetime.now(UTC)
    if table == "cpic_guidelines":
        conn.execute(
            """
            INSERT INTO cpic_guidelines (
                guideline_id, gene_symbol, drug_name, source_version_id, retrieval_date
            )
            VALUES (1, ?, 'warfarin', ?, ?)
            """,
            [gene_symbol, svid, retrieval],
        )
    elif table == "pharmgkb_annotations":
        conn.execute(
            """
            INSERT INTO pharmgkb_annotations (
                pharmgkb_id, gene_symbol, source_version_id, retrieval_date
            )
            VALUES (1, ?, ?, ?)
            """,
            [gene_symbol, svid, retrieval],
        )
    else:  # pragma: no cover - guard against a typo in a future edit
        msg = f"unexpected PGx table {table!r}"
        raise ValueError(msg)
    return svid


def _genes_count(conn: DuckDBPyConnection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM genes").fetchone()
    assert row is not None
    return int(row[0])


def _hgnc_version_count(conn: DuckDBPyConnection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'hgnc'",
    ).fetchone()
    assert row is not None
    return int(row[0])


def _hgnc_svid(conn: DuckDBPyConnection) -> int:
    row = conn.execute(
        "SELECT source_version_id FROM annotation_source_versions"
        " WHERE source_db = 'hgnc' ORDER BY source_version_id DESC LIMIT 1",
    ).fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# 1. Keystone FK — a derived_acmg_sf_findings insert against a seeded gene SUCCEEDS
# ---------------------------------------------------------------------------


def test_seed_genes_keystone_fk_satisfied() -> None:
    """from: plan §5 keystone + §6 gate-clear probe-INSERT.

    The whole point of the seed: after it runs, a Phase-6-shaped
    ``derived_acmg_sf_findings`` insert that references a seeded ACMG ``gene_symbol``
    (with every co-required NOT NULL supplied) satisfies the ``genes`` FK and lands.
    Only ``gene_symbol`` is the free FK variable.
    """
    init_databases()
    with duckdb_connection() as conn:
        variant_id = _insert_variant(conn)
        run_id = _insert_analysis_run(conn)
        seed_genes(conn)

        # Seeded ACMG gene → the genes FK is satisfied → insert succeeds.
        _insert_acmg_finding(
            conn,
            derived_id=1,
            analysis_run_id=run_id,
            gene_symbol=_SEEDED_ACMG_GENE,
            variant_id=variant_id,
        )
        landed = conn.execute(
            "SELECT COUNT(*) FROM derived_acmg_sf_findings WHERE gene_symbol = ?",
            [_SEEDED_ACMG_GENE],
        ).fetchone()
    assert landed is not None
    assert landed[0] == 1


# ---------------------------------------------------------------------------
# 2. Negative control — an UNSEEDED gene_symbol RAISES on the genes FK
# ---------------------------------------------------------------------------


def test_seed_genes_fk_rejects_unseeded_symbol() -> None:
    """from: plan §5 negative control + §6 gate-clear probe-INSERT (raises naming genes).

    Same fully-specified fixture (valid analysis_run_id + variant_id), but
    ``gene_symbol='ZZZ_NOT_A_GENE'``. The insert must raise, and the failure must be the
    ``genes`` FK — not ``variants_master`` / ``analysis_runs``. We supply valid values for
    those two so the only FK that can fire is the genes one, and we assert the error text
    names ``genes``/``gene_symbol`` and does NOT name the other two parents.
    """
    init_databases()
    with duckdb_connection() as conn:
        variant_id = _insert_variant(conn)
        run_id = _insert_analysis_run(conn)
        seed_genes(conn)

        with pytest.raises(duckdb.Error) as excinfo:
            _insert_acmg_finding(
                conn,
                derived_id=1,
                analysis_run_id=run_id,
                gene_symbol=_UNSEEDED_GENE,
                variant_id=variant_id,
            )
    message = str(excinfo.value).lower()
    # The FK that fired must be the genes one.
    assert "genes" in message or "gene_symbol" in message, message
    # Fail loudly if instead the wrong parent FK fired.
    assert "variants_master" not in message, message
    assert "analysis_runs" not in message, message


# ---------------------------------------------------------------------------
# 3. Atomicity — a mid-INSERT failure leaves zero genes AND zero orphan version row
# ---------------------------------------------------------------------------


def test_seed_genes_atomic_no_orphan_version_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """from: plan §5 atomicity + §2(b) finding-015 orphan-version closure.

    Force a failure mid-INSERT by exploding the module's ``_insert_genes`` seam. After the
    raise: ``genes`` is empty (txn rolled back) AND no orphan ``hgnc``
    ``annotation_source_versions`` row survives (rollback-then-cleanup, clinvar-exact), AND
    no secondary exception leaked from the best-effort cleanup.
    """
    init_databases()

    boom = RuntimeError("simulated seed insert failure")

    def _explode(*_args: object, **_kwargs: object) -> int:
        raise boom

    monkeypatch.setattr(seed_genes_module, "_insert_genes", _explode)

    with duckdb_connection() as conn:
        # The only exception that surfaces is the seeded one (no secondary cleanup error).
        with pytest.raises(RuntimeError, match="simulated seed insert failure"):
            seed_genes(conn)

        genes_n = _genes_count(conn)
        hgnc_versions = _hgnc_version_count(conn)
    assert genes_n == 0
    assert hgnc_versions == 0


# ---------------------------------------------------------------------------
# 4. Provenance columns + across-run hash stability (pre-mortem #3)
# ---------------------------------------------------------------------------


def test_seed_genes_provenance_columns() -> None:
    """from: plan §5 provenance + §6 provenance + pre-mortem #3 (deterministic hash).

    Every genes row carries non-NULL ``retrieval_date`` and ``source_version_id`` == the
    single hgnc svid; exactly one hgnc version row exists; its ``record_count`` ==
    COUNT(genes). PLUS: two independent seed runs over the same input produce a
    byte-identical ``source_file_hash`` (the union is sorted before hashing).
    """
    # Run 1 — fresh DB.
    init_databases()
    with duckdb_connection() as conn:
        result = seed_genes(conn)
        svid = _hgnc_svid(conn)

        assert result.source_version_id == svid
        assert _hgnc_version_count(conn) == 1

        null_sv = conn.execute(
            "SELECT COUNT(*) FROM genes WHERE source_version_id IS NULL",
        ).fetchone()
        null_rd = conn.execute(
            "SELECT COUNT(*) FROM genes WHERE retrieval_date IS NULL",
        ).fetchone()
        wrong_sv = conn.execute(
            "SELECT COUNT(*) FROM genes WHERE source_version_id <> ?",
            [svid],
        ).fetchone()
        record_count = conn.execute(
            "SELECT record_count FROM annotation_source_versions WHERE source_version_id = ?",
            [svid],
        ).fetchone()
        genes_n = _genes_count(conn)
        hash_1 = conn.execute(
            "SELECT source_file_hash FROM annotation_source_versions WHERE source_version_id = ?",
            [svid],
        ).fetchone()

    assert null_sv is not None
    assert null_sv[0] == 0
    assert null_rd is not None
    assert null_rd[0] == 0
    assert wrong_sv is not None
    assert wrong_sv[0] == 0
    assert record_count is not None
    assert record_count[0] == genes_n
    assert hash_1 is not None

    # Run 2 — a second fresh DB. Same input → byte-identical source_file_hash.
    init_databases()
    with duckdb_connection() as conn:
        seed_genes(conn)
        svid2 = _hgnc_svid(conn)
        hash_2 = conn.execute(
            "SELECT source_file_hash FROM annotation_source_versions WHERE source_version_id = ?",
            [svid2],
        ).fetchone()
    assert hash_2 is not None
    assert hash_2[0] == hash_1[0]


# ---------------------------------------------------------------------------
# 5. No pointer flip — the seed writes the version row + rows and STOPS
# ---------------------------------------------------------------------------


def test_seed_genes_no_pointer_flip() -> None:
    """from: plan §5 no-pointer-flip + §3 constraint #7 + §6 negative control.

    The seed must NOT create an ``annotation_sources`` pointer for ``hgnc``: the
    ``annotation_sources`` total row count is unchanged by the seed, no ``hgnc`` pointer
    appears, and an unrelated pre-existing pointer (gnomad) is left at its value.
    """
    init_databases()
    with duckdb_connection() as conn:
        # Establish an unrelated existing pointer (gnomad) before the seed.
        gnomad_svid = insert_source_version(
            conn,
            source_db="gnomad",
            version="4.1.1",
            source_url=None,
            source_file_hash="gnomad_test",
            source_file_size=0,
            record_count=1,
        )
        flip_to_new_version(
            conn,
            source="gnomad",
            table="gnomad_frequencies",
            new_source_version_id=gnomad_svid,
        )

        before_total = conn.execute("SELECT COUNT(*) FROM annotation_sources").fetchone()

        seed_genes(conn)

        after_total = conn.execute("SELECT COUNT(*) FROM annotation_sources").fetchone()
        hgnc_pointer = conn.execute(
            "SELECT COUNT(*) FROM annotation_sources WHERE source_db = 'hgnc'",
        ).fetchone()
        gnomad_pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db = 'gnomad'",
        ).fetchone()

    assert before_total is not None
    assert after_total is not None
    assert after_total[0] == before_total[0]  # unchanged by the seed
    assert hgnc_pointer is not None
    assert hgnc_pointer[0] == 0  # no hgnc pointer created
    assert gnomad_pointer is not None
    assert gnomad_pointer[0] == gnomad_svid  # unrelated pointer untouched


# ---------------------------------------------------------------------------
# 6. Coverage gate — both EXCEPT probes are 0 against the consumed PGx tables
# ---------------------------------------------------------------------------


def test_seed_genes_coverage_gate_against_consumed_tables() -> None:
    """from: plan §5 coverage gate + §6 gate-clear coverage + pre-mortem #1 backstop.

    Seed one cpic gene + one pharmgkb gene under flipped pointers BEFORE the seed, then run
    ``seed_genes``. By construction the PGx half is derived from those tables, so:

      * the ``cpic EXCEPT genes`` probe == 0 and the ``pharmgkb EXCEPT genes`` probe == 0,
      * ``result.cpic_uncovered == result.pharmgkb_uncovered == 0``,
      * ``result.cpic_covered`` and ``result.pharmgkb_covered`` are True,
      * ``pgx_genes`` reflects the two seeded PGx symbols,
      * both PGx symbols appear in ``genes`` with ``is_pgx_relevant = TRUE``.
    """
    cpic_gene = "CYP2C19"
    pharmgkb_gene = "DPYD"

    init_databases()
    with duckdb_connection() as conn:
        _seed_pgx_source_with_gene(
            conn,
            source_db="cpic",
            table="cpic_guidelines",
            gene_symbol=cpic_gene,
        )
        _seed_pgx_source_with_gene(
            conn,
            source_db="pharmgkb",
            table="pharmgkb_annotations",
            gene_symbol=pharmgkb_gene,
        )

        result = seed_genes(conn)

        # The two EXCEPT probes the gate computes — both must return zero rows.
        cpic_uncovered = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT gene_symbol FROM cpic_guidelines
                EXCEPT
                SELECT gene_symbol FROM genes
            )
            """,
        ).fetchone()
        pharmgkb_uncovered = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT gene_symbol FROM pharmgkb_annotations WHERE gene_symbol IS NOT NULL
                EXCEPT
                SELECT gene_symbol FROM genes
            )
            """,
        ).fetchone()
        cpic_flag = conn.execute(
            "SELECT is_pgx_relevant FROM genes WHERE gene_symbol = ?",
            [cpic_gene],
        ).fetchone()
        pharmgkb_flag = conn.execute(
            "SELECT is_pgx_relevant FROM genes WHERE gene_symbol = ?",
            [pharmgkb_gene],
        ).fetchone()

    assert cpic_uncovered is not None
    assert cpic_uncovered[0] == 0
    assert pharmgkb_uncovered is not None
    assert pharmgkb_uncovered[0] == 0

    assert result.cpic_uncovered == 0
    assert result.pharmgkb_uncovered == 0
    assert result.cpic_covered is True
    assert result.pharmgkb_covered is True

    # The two PGx symbols (cpic_gene + pharmgkb_gene) were seeded and flagged.
    expected_pgx = 2
    assert result.pgx_genes == expected_pgx
    assert cpic_flag is not None
    assert cpic_flag[0] is True
    assert pharmgkb_flag is not None
    assert pharmgkb_flag[0] is True


# ---------------------------------------------------------------------------
# 6a. Union/merge semantics + verbatim ACMG metadata (plan §4 steps 2-3)
# ---------------------------------------------------------------------------


def test_seed_genes_dual_flag_and_acmg_metadata() -> None:
    """from: plan §4 step 3 (merge semantics) + §4 step 2 / Dataset (verbatim ACMG metadata).

    Locks three rows the count-only tests do not:
      * a symbol in BOTH the ACMG panel and a PGx table -> is_acmg_sf=TRUE AND
        is_pgx_relevant=TRUE with the ACMG metadata RETAINED. RYR1 is in the
        verified ACMG SF v3.3 panel AND is a real CPIC gene, so seeding it via
        cpic exercises the union/merge branch.
      * a pure-ACMG gene -> metadata populated verbatim, is_pgx_relevant=FALSE.
      * a PGx-only gene -> is_acmg_sf=FALSE and acmg_sf_disease IS NULL.

    Every asserted metadata value is verbatim from the spec
    (docs/plans/pr-6-acmg-sf-v3.3-genes.csv: RYR1 / BRCA1 rows), NOT read from the
    implementation — a subtle flag-swap or metadata-null bug in _build_rows would
    pass the count-only tests but fail here.
    """
    init_databases()
    with duckdb_connection() as conn:
        _seed_pgx_source_with_gene(
            conn,
            source_db="cpic",
            table="cpic_guidelines",
            gene_symbol="RYR1",  # in the ACMG panel AND a real CPIC gene -> dual-flag
        )
        _seed_pgx_source_with_gene(
            conn,
            source_db="pharmgkb",
            table="pharmgkb_annotations",
            gene_symbol="DPYD",  # PGx-only, not in the ACMG panel
        )
        result = seed_genes(conn)
        dual = conn.execute(
            "SELECT is_acmg_sf, is_pgx_relevant, acmg_sf_disease, acmg_sf_inheritance,"
            " acmg_sf_version FROM genes WHERE gene_symbol = 'RYR1'",
        ).fetchone()
        pure_acmg = conn.execute(
            "SELECT is_acmg_sf, is_pgx_relevant, acmg_sf_disease, acmg_sf_inheritance,"
            " acmg_sf_version FROM genes WHERE gene_symbol = 'BRCA1'",
        ).fetchone()
        pgx_only = conn.execute(
            "SELECT is_acmg_sf, is_pgx_relevant, acmg_sf_disease"
            " FROM genes WHERE gene_symbol = 'DPYD'",
        ).fetchone()

    # Dual-flagged, ACMG metadata retained (verbatim from the CSV row for RYR1).
    assert dual == (True, True, "Malignant hyperthermia susceptibility", "AD", "v1.0")
    # Pure ACMG: metadata populated verbatim, not flagged PGx (BRCA1 CSV row).
    assert pure_acmg == (True, False, "Hereditary breast and ovarian cancer", "AD", "v1.0")
    # PGx-only: no ACMG flag, no ACMG disease metadata.
    assert pgx_only == (False, True, None)
    # RYR1 is already in the 84-gene panel, so the union adds no ACMG gene; only
    # the two derived PGx symbols (RYR1, DPYD) are pgx-flagged.
    assert result.acmg_sf_genes == _EXPECTED_FRESH_ACMG_GENES
    expected_pgx = 2
    assert result.pgx_genes == expected_pgx


# ---------------------------------------------------------------------------
# 6b. Fresh-DB headline counts (the §6 summary on an empty consumed-table DB)
# ---------------------------------------------------------------------------


def test_seed_genes_fresh_db_counts() -> None:
    """from: plan §6 summary + "Behavior facts" (fresh init has empty cpic/pharmgkb).

    A fresh ``init_databases()`` DB has empty cpic/pharmgkb, so the seed plants exactly the
    84 ACMG genes: genes_rows == acmg_sf_genes == 84, pgx_genes == 0, both *covered* True,
    both *uncovered* 0, already_populated False. (84 = the verified ACMG SF v3.3 panel.)
    """
    init_databases()
    with duckdb_connection() as conn:
        result = seed_genes(conn)
        genes_n = _genes_count(conn)
        acmg_in_table = conn.execute(
            "SELECT COUNT(*) FROM genes WHERE is_acmg_sf = TRUE",
        ).fetchone()

    assert isinstance(result, GeneSeedResult)
    assert result.already_populated is False
    assert result.genes_rows == _EXPECTED_FRESH_ACMG_GENES
    assert result.acmg_sf_genes == _EXPECTED_FRESH_ACMG_GENES
    assert result.pgx_genes == 0
    assert result.cpic_covered is True
    assert result.pharmgkb_covered is True
    assert result.cpic_uncovered == 0
    assert result.pharmgkb_uncovered == 0
    assert genes_n == _EXPECTED_FRESH_ACMG_GENES
    assert acmg_in_table is not None
    assert acmg_in_table[0] == _EXPECTED_FRESH_ACMG_GENES


# ---------------------------------------------------------------------------
# 7. Idempotence — a second run without force is a no-op short-circuit
# ---------------------------------------------------------------------------


def test_seed_genes_idempotent() -> None:
    """from: plan §5 idempotent + §6 idempotence.

    Second ``seed_genes`` without force → ``already_populated is True``, counts unchanged,
    and the ``source_version_id`` equals the first run's svid; exactly ONE hgnc version row
    survives (no extra orphan per re-run).
    """
    init_databases()
    with duckdb_connection() as conn:
        first = seed_genes(conn)
        first_genes = _genes_count(conn)
        first_svid = _hgnc_svid(conn)

        second = seed_genes(conn)
        second_genes = _genes_count(conn)
        hgnc_versions = _hgnc_version_count(conn)

    assert first.already_populated is False
    assert second.already_populated is True
    assert second.genes_rows == first.genes_rows
    assert second.acmg_sf_genes == first.acmg_sf_genes
    assert second.pgx_genes == first.pgx_genes
    assert second_genes == first_genes
    assert second.source_version_id == first_svid
    assert hgnc_versions == 1  # no orphan version row added by the re-run


# ---------------------------------------------------------------------------
# 8. Force — leaf DB re-seeds under a FRESH svid; non-leaf DB RAISES (not silent DELETE)
# ---------------------------------------------------------------------------


def test_seed_genes_force_reseeds_on_leaf_db() -> None:
    """from: plan §5 force + §4 (force re-seeds under a fresh, larger svid on a leaf DB)."""
    init_databases()
    with duckdb_connection() as conn:
        seed_genes(conn)
        old_svid = _hgnc_svid(conn)
        old_genes = _genes_count(conn)

        # No derived_* / pathway_genes rows reference genes → it is a leaf → force succeeds.
        result = seed_genes(conn, force=True)
        new_svid = _hgnc_svid(conn)
        new_genes = _genes_count(conn)

    assert result.already_populated is False
    assert new_svid > old_svid  # a FRESH (larger) svid was allocated
    assert result.source_version_id == new_svid
    assert new_genes == old_genes  # DELETE + re-INSERT of the same content


def test_seed_genes_force_raises_when_not_leaf() -> None:
    """from: plan §5 force + pre-mortem #2 (5 FK dependents — RAISE, never silent DELETE).

    With a ``derived_acmg_sf_findings`` row referencing a seeded gene, ``genes`` is NOT a
    leaf, so ``force=True`` must raise ``GenesNotLeafError`` rather than DELETE the gene out
    from under the child row. (The implementation enumerates all five dependents — the four
    ``derived_*`` tables and ``pathway_genes``; this exercises the ``derived_acmg_sf_findings``
    leg, which is the directly-constructible one without first seeding a ``pathways`` row.)
    """
    init_databases()
    with duckdb_connection() as conn:
        variant_id = _insert_variant(conn)
        run_id = _insert_analysis_run(conn)
        seed_genes(conn)
        _insert_acmg_finding(
            conn,
            derived_id=1,
            analysis_run_id=run_id,
            gene_symbol=_SEEDED_ACMG_GENE,
            variant_id=variant_id,
        )

        with pytest.raises(GenesNotLeafError):
            seed_genes(conn, force=True)

        # The non-leaf guard must not have deleted any genes.
        genes_n = _genes_count(conn)
    assert genes_n == _EXPECTED_FRESH_ACMG_GENES


# ---------------------------------------------------------------------------
# 9. CLI discoverability — seed-genes appears in `annotate --help`
# ---------------------------------------------------------------------------


def test_seed_genes_appears_in_help() -> None:
    """from: plan §5 help test (mirror test_annotate_refresh_index_appears_in_help)."""
    result = CliRunner().invoke(app, ["annotate", "--help"])
    assert result.exit_code == 0
    assert "seed-genes" in result.output


# ---------------------------------------------------------------------------
# 10. CLI happy path — `annotate seed-genes` populates + echoes the summary line
# ---------------------------------------------------------------------------


def test_seed_genes_populates_and_echoes_summary() -> None:
    """from: plan §5 CLI + §6 summary (fresh init_databases() DB).

    ``genome annotate seed-genes`` on a fresh DB exits 0 and echoes a one-line summary
    starting ``genes seeded:`` carrying ``genes_rows=84``, ``acmg_sf_genes=84``,
    ``pgx_genes=0``, ``cpic_covered=True``, ``pharmgkb_covered=True``.
    """
    init_databases()
    result = CliRunner().invoke(app, ["annotate", "seed-genes"])
    assert result.exit_code == 0, result.output
    output = result.output
    assert "genes seeded:" in output
    assert f"genes_rows={_EXPECTED_FRESH_ACMG_GENES}" in output
    assert f"acmg_sf_genes={_EXPECTED_FRESH_ACMG_GENES}" in output
    assert "pgx_genes=0" in output
    assert "cpic_covered=True" in output
    assert "pharmgkb_covered=True" in output

    # The rows actually landed.
    with duckdb_connection() as conn:
        genes_n = _genes_count(conn)
    assert genes_n == _EXPECTED_FRESH_ACMG_GENES

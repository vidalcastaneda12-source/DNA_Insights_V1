"""Phase 3 merge orchestrator: pair, resolve, tier-3 flip, write.

The merge is **idempotent**: ``merge_all`` clears ``consensus_genotypes`` and
``discrepancies`` first, then rebuilds them from the current set of active
``genotype_calls``. Re-running after a re-ingest is the supported way to
refresh the consensus.

Tier-2 (rsid-based matching across positions) is intentionally deferred to
Phase 5. It depends on the ``variant_aliases`` table loaded from dbSNP, which
arrives with the reference-annotation loaders.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Final

import pyarrow as pa
import structlog

from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.merge.consensus import resolve
from genome.merge.models import (
    MERGE_VERSION,
    CallView,
    ConsensusRow,
    DiscrepancyRow,
    MergeResult,
    Source,
    VariantPair,
)
from genome.merge.strand import complement_pair, is_palindromic_site, sorted_pair

if TYPE_CHECKING:
    from pathlib import Path

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

_STRAND_FLIP_CONFIDENCE: Final[float] = 0.90


def _fetch_variant_pairs(conn: DuckDBPyConnection) -> list[VariantPair]:
    """Return one row per ``variants_master`` row with paired 23andme + ancestry calls.

    The aggregation uses ``MAX(... is_active)`` on each per-source call to
    pivot into a single wide row — there is at most one active call per
    ``(variant_id, source)`` by the writer's invariant, so ``MAX`` is just a
    "the value if it exists" extractor.
    """
    rows = conn.execute(
        """
        SELECT
            vm.variant_id,
            CAST(vm.chrom AS VARCHAR) AS chrom,
            vm.pos_grch38,
            vm.ref_allele,
            vm.alt_allele,
            MAX(CASE WHEN gc.source = '23andme'  THEN gc.call_id END)    AS call_id_23,
            MAX(CASE WHEN gc.source = '23andme'  THEN gc.allele_1 END)   AS a1_23,
            MAX(CASE WHEN gc.source = '23andme'  THEN gc.allele_2 END)   AS a2_23,
            BOOL_OR(gc.source = '23andme'  AND gc.is_no_call)            AS nc_23,
            MAX(CASE WHEN gc.source = 'ancestry' THEN gc.call_id END)    AS call_id_anc,
            MAX(CASE WHEN gc.source = 'ancestry' THEN gc.allele_1 END)   AS a1_anc,
            MAX(CASE WHEN gc.source = 'ancestry' THEN gc.allele_2 END)   AS a2_anc,
            BOOL_OR(gc.source = 'ancestry' AND gc.is_no_call)            AS nc_anc
        FROM variants_master vm
        LEFT JOIN genotype_calls gc
            ON gc.variant_id = vm.variant_id
           AND gc.is_active
           AND gc.source IN ('23andme', 'ancestry')
        GROUP BY vm.variant_id, vm.chrom, vm.pos_grch38, vm.ref_allele, vm.alt_allele
        ORDER BY vm.variant_id
        """,
    ).fetchall()

    pairs: list[VariantPair] = []
    for row in rows:
        # DuckDB returns each row as an untyped tuple; per the SELECT shape it
        # is exactly the 13 columns above in order. Build the pair eagerly so
        # mypy never has to see an opaque-typed intermediate flow back out.
        (
            variant_id,
            chrom,
            pos_grch38,
            ref_allele,
            alt_allele,
            call_id_23,
            a1_23,
            a2_23,
            nc_23,
            call_id_anc,
            a1_anc,
            a2_anc,
            nc_anc,
        ) = row
        pairs.append(
            VariantPair(
                variant_id=int(variant_id),
                chrom=str(chrom),
                pos_grch38=int(pos_grch38),
                ref_allele=str(ref_allele),
                alt_allele=str(alt_allele),
                twentythree=_build_call_view("23andme", call_id_23, a1_23, a2_23, nc_23),
                ancestry=_build_call_view("ancestry", call_id_anc, a1_anc, a2_anc, nc_anc),
            ),
        )
    return pairs


def _build_call_view(
    source: Source,
    call_id: object,
    allele_1: object,
    allele_2: object,
    is_no_call: object,
) -> CallView | None:
    """Wrap one source's pivoted columns into a :class:`CallView` (or ``None``).

    ``object`` is used in the signature (not ``Any``) because the values come
    straight off a DuckDB row tuple and ruff's ``ANN401`` forbids ``Any``
    annotations. The ``int(...)`` / ``str(...)`` / ``bool(...)`` casts accept
    ``object`` at type-check time and validate at runtime.
    """
    if call_id is None:
        return None
    return CallView(
        call_id=int(call_id),  # type: ignore[call-overload]
        source=source,
        allele_1=None if allele_1 is None else str(allele_1),
        allele_2=None if allele_2 is None else str(allele_2),
        is_no_call=bool(is_no_call),
    )


def _next_id(conn: DuckDBPyConnection, table: str, column: str) -> int:
    sql = f"SELECT COALESCE(MAX({column}), 0) FROM {table}"  # noqa: S608
    row = conn.execute(sql).fetchone()
    return int(row[0]) + 1 if row is not None else 1


def _single_source_call(pair: VariantPair) -> CallView | None:
    """Return the sole called (non-no-call) ``CallView`` for a candidate pair.

    A pair qualifies for tier-3 strand-flip matching only when exactly one
    source has an active call AND that call is not a no-call. Returns ``None``
    for any other shape so the caller can skip the row.
    """
    has_23 = pair.twentythree is not None
    has_anc = pair.ancestry is not None
    if has_23 == has_anc:  # both or neither — not a single-source candidate
        return None
    sole = pair.twentythree if has_23 else pair.ancestry
    if sole is None or sole.is_no_call:
        return None
    return sole


def _is_strand_flip_match(
    a: VariantPair,
    b: VariantPair,
    call_a: CallView,
    call_b: CallView,
    consensus_by_id: dict[int, ConsensusRow],
) -> bool:
    """Return True when ``(a, b)`` is a tier-3 strand-flip pair to rewrite."""
    if (call_a.source == "23andme") == (call_b.source == "23andme"):
        return False
    if is_palindromic_site(a.ref_allele, a.alt_allele):
        return False
    if is_palindromic_site(b.ref_allele, b.alt_allele):
        return False
    a_alleles = sorted_pair(call_a.allele_1 or "", call_a.allele_2 or "")
    b_flipped = complement_pair(call_b.allele_1 or "", call_b.allele_2 or "")
    if a_alleles != b_flipped:
        return False
    ca = consensus_by_id.get(a.variant_id)
    cb = consensus_by_id.get(b.variant_id)
    if ca is None or cb is None:
        return False
    return ca.consensus_method == "single_source" and cb.consensus_method == "single_source"


def _strand_flip_partners(
    pairs: list[VariantPair],
    consensus_by_id: dict[int, ConsensusRow],
) -> list[tuple[VariantPair, VariantPair]]:
    """Find tier-3 partner pairs: two single-source ``variants_master`` rows at
    the same ``(chrom, pos_grch38)`` whose alleles complement-match.

    Returns ordered ``(a, b)`` tuples where ``a`` carries the 23andme call and
    ``b`` carries the ancestry call. Palindromic sites are excluded — those
    are unresolvable from genotype alone and stay as separate
    ``platform_unique`` rows.
    """
    by_pos: dict[tuple[str, int], list[tuple[VariantPair, CallView]]] = defaultdict(list)
    for pair in pairs:
        call = _single_source_call(pair)
        if call is None:
            continue
        by_pos[(pair.chrom, pair.pos_grch38)].append((pair, call))

    out: list[tuple[VariantPair, VariantPair]] = []
    for siblings in by_pos.values():
        if len(siblings) < 2:  # noqa: PLR2004 — pairs need at least two siblings
            continue
        for i, (a, call_a) in enumerate(siblings):
            for b, call_b in siblings[i + 1 :]:
                if not _is_strand_flip_match(a, b, call_a, call_b, consensus_by_id):
                    continue
                left, right = (a, b) if call_a.source == "23andme" else (b, a)
                out.append((left, right))
    return out


def _strand_flip_consensus(
    pair: VariantPair,
    self_call: CallView,
    partner_call: CallView,
) -> ConsensusRow:
    """Rewrite a single-source consensus into a strand-flip-resolved one.

    The consensus alleles stay in this row's own ref/alt frame (so dosage
    stays self-consistent with ``variants_master.alt_allele``); both call_ids
    feed ``contributing_calls``.
    """
    a_alleles = sorted_pair(self_call.allele_1 or "", self_call.allele_2 or "")
    return ConsensusRow(
        variant_id=pair.variant_id,
        consensus_allele_1=a_alleles[0],
        consensus_allele_2=a_alleles[1],
        is_no_call=False,
        dosage=(int(a_alleles[0] == pair.alt_allele) + int(a_alleles[1] == pair.alt_allele)),
        consensus_method="disagreement_resolved",
        is_imputed=False,
        consensus_r2=None,
        contributing_calls=(self_call.call_id, partner_call.call_id),
        resolution_rule=MERGE_VERSION,
        confidence=_STRAND_FLIP_CONFIDENCE,
    )


def _strand_flip_discrepancy(
    pair: VariantPair,
    self_call: CallView,
    partner_call: CallView,
) -> DiscrepancyRow:
    return DiscrepancyRow(
        variant_id=pair.variant_id,
        discrepancy_type="genotype_mismatch",
        severity="info",
        source_a=self_call.source,
        call_a_id=self_call.call_id,
        genotype_a=f"{self_call.allele_1 or ''}/{self_call.allele_2 or ''}",
        source_b=partner_call.source,
        call_b_id=partner_call.call_id,
        genotype_b=f"{partner_call.allele_1 or ''}/{partner_call.allele_2 or ''}",
        resolution="flipped_strand_match",
        resolution_reason=(
            "tier-3 cross-row strand flip: partner variants_master row at the same "
            "(chrom, pos_grch38) carries complement alleles; merging across rows"
        ),
    )


def _apply_strand_flip(
    pairs_index: dict[int, VariantPair],
    consensus_by_id: dict[int, ConsensusRow],
    discrepancies_by_variant: dict[int, list[DiscrepancyRow]],
    partner_pairs: list[tuple[VariantPair, VariantPair]],
) -> int:
    """Rewrite consensus + discrepancy for each tier-3 matched pair. Returns count."""
    resolved = 0
    for left, right in partner_pairs:
        left_call = left.twentythree
        right_call = right.ancestry
        if left_call is None or right_call is None:
            continue

        for self_pair, self_call, partner_call in (
            (left, left_call, right_call),
            (right, right_call, left_call),
        ):
            pair_in_index = pairs_index[self_pair.variant_id]
            consensus_by_id[pair_in_index.variant_id] = _strand_flip_consensus(
                pair_in_index,
                self_call,
                partner_call,
            )
            # Replace any prior discrepancies for this variant with the strand-flip one.
            discrepancies_by_variant[pair_in_index.variant_id] = [
                _strand_flip_discrepancy(pair_in_index, self_call, partner_call),
            ]
            resolved += 1
    return resolved


def _stage_consensus(conn: DuckDBPyConnection, rows: list[ConsensusRow]) -> None:
    conn.execute("DROP TABLE IF EXISTS _merge_consensus_stage")
    conn.execute(
        """
        CREATE TEMP TABLE _merge_consensus_stage (
            variant_id          BIGINT,
            consensus_allele_1  VARCHAR,
            consensus_allele_2  VARCHAR,
            is_no_call          BOOLEAN,
            dosage              SMALLINT,
            consensus_method    VARCHAR,
            is_imputed          BOOLEAN,
            consensus_r2        DOUBLE,
            contributing_calls  BIGINT[],
            resolution_rule     VARCHAR,
            confidence          DOUBLE
        )
        """,
    )
    if not rows:
        return
    table = pa.table(
        {
            "variant_id": pa.array([r.variant_id for r in rows], type=pa.int64()),
            "consensus_allele_1": pa.array([r.consensus_allele_1 for r in rows], type=pa.string()),
            "consensus_allele_2": pa.array([r.consensus_allele_2 for r in rows], type=pa.string()),
            "is_no_call": pa.array([r.is_no_call for r in rows], type=pa.bool_()),
            "dosage": pa.array([r.dosage for r in rows], type=pa.int16()),
            "consensus_method": pa.array([r.consensus_method for r in rows], type=pa.string()),
            "is_imputed": pa.array([r.is_imputed for r in rows], type=pa.bool_()),
            "consensus_r2": pa.array([r.consensus_r2 for r in rows], type=pa.float64()),
            "contributing_calls": pa.array(
                [list(r.contributing_calls) for r in rows],
                type=pa.list_(pa.int64()),
            ),
            "resolution_rule": pa.array([r.resolution_rule for r in rows], type=pa.string()),
            "confidence": pa.array([r.confidence for r in rows], type=pa.float64()),
        },
    )
    try:
        conn.register("_merge_consensus_arrow", table)
        conn.execute(
            "INSERT INTO _merge_consensus_stage SELECT * FROM _merge_consensus_arrow",
        )
    finally:
        conn.unregister("_merge_consensus_arrow")


def _stage_discrepancies(conn: DuckDBPyConnection, rows: list[DiscrepancyRow]) -> None:
    conn.execute("DROP TABLE IF EXISTS _merge_discrepancy_stage")
    conn.execute(
        """
        CREATE TEMP TABLE _merge_discrepancy_stage (
            ord               BIGINT,
            variant_id        BIGINT,
            discrepancy_type  VARCHAR,
            severity          VARCHAR,
            source_a          VARCHAR,
            call_a_id         BIGINT,
            genotype_a        VARCHAR,
            source_b          VARCHAR,
            call_b_id         BIGINT,
            genotype_b        VARCHAR,
            resolution        VARCHAR,
            resolution_reason VARCHAR
        )
        """,
    )
    if not rows:
        return
    n = len(rows)
    table = pa.table(
        {
            "ord": pa.array(range(n), type=pa.int64()),
            "variant_id": pa.array([r.variant_id for r in rows], type=pa.int64()),
            "discrepancy_type": pa.array([r.discrepancy_type for r in rows], type=pa.string()),
            "severity": pa.array([r.severity for r in rows], type=pa.string()),
            "source_a": pa.array([r.source_a for r in rows], type=pa.string()),
            "call_a_id": pa.array([r.call_a_id for r in rows], type=pa.int64()),
            "genotype_a": pa.array([r.genotype_a for r in rows], type=pa.string()),
            "source_b": pa.array([r.source_b for r in rows], type=pa.string()),
            "call_b_id": pa.array([r.call_b_id for r in rows], type=pa.int64()),
            "genotype_b": pa.array([r.genotype_b for r in rows], type=pa.string()),
            "resolution": pa.array([r.resolution for r in rows], type=pa.string()),
            "resolution_reason": pa.array([r.resolution_reason for r in rows], type=pa.string()),
        },
    )
    try:
        conn.register("_merge_discrepancy_arrow", table)
        conn.execute(
            "INSERT INTO _merge_discrepancy_stage SELECT * FROM _merge_discrepancy_arrow",
        )
    finally:
        conn.unregister("_merge_discrepancy_arrow")


def _flush_consensus(conn: DuckDBPyConnection) -> int:
    conn.execute(
        """
        INSERT INTO consensus_genotypes (
            variant_id, consensus_allele_1, consensus_allele_2, is_no_call,
            dosage, consensus_method, is_imputed, consensus_r2,
            contributing_calls, resolution_rule, confidence
        )
        SELECT
            variant_id, consensus_allele_1, consensus_allele_2, is_no_call,
            dosage,
            consensus_method::consensus_method_enum,
            is_imputed, consensus_r2,
            contributing_calls,
            resolution_rule,
            CAST(confidence AS DECIMAL(3,2))
          FROM _merge_consensus_stage
        """,
    )
    row = conn.execute("SELECT COUNT(*) FROM _merge_consensus_stage").fetchone()
    return int(row[0]) if row else 0


def _flush_discrepancies(conn: DuckDBPyConnection, base_id: int) -> int:
    conn.execute(
        """
        INSERT INTO discrepancies (
            discrepancy_id, variant_id, discrepancy_type, severity,
            source_a, call_a_id, genotype_a,
            source_b, call_b_id, genotype_b,
            resolution, resolution_reason
        )
        SELECT
            ? + ord                                       AS discrepancy_id,
            variant_id,
            discrepancy_type::discrepancy_type_enum,
            severity::severity_enum,
            source_a::source_enum,
            call_a_id,
            genotype_a,
            CASE WHEN source_b IS NULL THEN NULL
                 ELSE source_b::source_enum END           AS source_b,
            call_b_id,
            genotype_b,
            resolution,
            resolution_reason
          FROM _merge_discrepancy_stage
        """,
        [base_id],
    )
    row = conn.execute("SELECT COUNT(*) FROM _merge_discrepancy_stage").fetchone()
    return int(row[0]) if row else 0


def _summarize(
    consensus_rows: list[ConsensusRow],
    discrepancy_rows: list[DiscrepancyRow],
    strand_flip_resolutions: int,
) -> MergeResult:
    method_counts: dict[str, int] = defaultdict(int)
    for c in consensus_rows:
        method_counts[c.consensus_method] += 1

    type_counts: dict[str, int] = defaultdict(int)
    sev_counts: dict[str, int] = defaultdict(int)
    for d in discrepancy_rows:
        type_counts[d.discrepancy_type] += 1
        sev_counts[d.severity] += 1

    shared = method_counts["both_concordant"] + method_counts["disagreement_resolved"]
    discordant = (
        type_counts["genotype_mismatch"]
        + type_counts["strand_ambiguous"]
        - strand_flip_resolutions  # flipped-resolutions are not biological discords
    )
    denom = shared + max(discordant, 0)
    concordance = float(shared) / float(denom) if denom else None

    return MergeResult(
        consensus_rows_written=len(consensus_rows),
        discrepancy_rows_written=len(discrepancy_rows),
        method_counts=dict(method_counts),
        discrepancy_type_counts=dict(type_counts),
        severity_counts=dict(sev_counts),
        strand_flip_resolutions=strand_flip_resolutions,
        concordance_rate=concordance,
    )


def merge_all(*, duckdb_path: Path | None = None) -> MergeResult:
    """Run the full Phase 3 merge against the configured DuckDB.

    Steps:

    1. ``DELETE`` the existing ``consensus_genotypes`` and ``discrepancies``
       rows (idempotence).
    2. Fetch one paired row per ``variants_master`` row.
    3. Resolve each row via :func:`consensus.resolve`.
    4. Apply tier-3 strand-flip resolution across single-source pairs at the
       same ``(chrom, pos_grch38)``.
    5. Bulk-insert the resulting consensus + discrepancy rows.

    The whole thing runs inside one transaction so a mid-merge failure leaves
    the database in its previous consistent state.
    """
    settings = get_settings()
    db_path = duckdb_path or settings.genome_duckdb_path

    log = logger.bind(rule=MERGE_VERSION)
    log.info("merge.start")

    with duckdb_connection(db_path) as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute("DELETE FROM discrepancies")
            conn.execute("DELETE FROM consensus_genotypes")

            pairs = _fetch_variant_pairs(conn)
            pairs_index = {p.variant_id: p for p in pairs}

            consensus_by_id: dict[int, ConsensusRow] = {}
            discrepancies_by_variant: dict[int, list[DiscrepancyRow]] = defaultdict(list)
            for pair in pairs:
                consensus, discs = resolve(pair)
                consensus_by_id[pair.variant_id] = consensus
                if discs:
                    discrepancies_by_variant[pair.variant_id].extend(discs)

            partner_pairs = _strand_flip_partners(pairs, consensus_by_id)
            strand_flips = _apply_strand_flip(
                pairs_index,
                consensus_by_id,
                discrepancies_by_variant,
                partner_pairs,
            )

            consensus_rows = [consensus_by_id[p.variant_id] for p in pairs]
            discrepancy_rows = [
                d
                for variant_id in pairs_index
                for d in discrepancies_by_variant.get(variant_id, [])
            ]

            _stage_consensus(conn, consensus_rows)
            _stage_discrepancies(conn, discrepancy_rows)

            base_discrepancy_id = _next_id(conn, "discrepancies", "discrepancy_id")
            consensus_n = _flush_consensus(conn)
            discrepancy_n = _flush_discrepancies(conn, base_discrepancy_id)
            conn.execute("DROP TABLE IF EXISTS _merge_consensus_stage")
            conn.execute("DROP TABLE IF EXISTS _merge_discrepancy_stage")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            log.exception("merge.failed")
            raise

    result = _summarize(consensus_rows, discrepancy_rows, strand_flips)
    log.info(
        "merge.complete",
        consensus_rows=consensus_n,
        discrepancy_rows=discrepancy_n,
        strand_flip_resolutions=strand_flips,
        method_counts=result.method_counts,
        discrepancy_type_counts=result.discrepancy_type_counts,
        severity_counts=result.severity_counts,
        concordance_rate=result.concordance_rate,
    )
    return result

"""End-to-end ingest: parse → normalize → lift-over → write → QC."""

from __future__ import annotations

import hashlib
import shutil
import stat
from typing import TYPE_CHECKING, Final

import structlog

from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.ingest import parsers
from genome.ingest.liftover import IdentityLiftover, make_liftover
from genome.ingest.models import (
    IngestResult,
    NormalizedCall,
    ParseStats,
    RawCall,
    RawFileMeta,
    Source,
)
from genome.ingest.normalize import normalize_calls
from genome.ingest.qc import compute_sample_qc
from genome.ingest.writer import (
    insert_ingestion_run,
    insert_sample_qc,
    write_calls,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from genome.ingest.liftover import Liftover

    _RawParser = Callable[[Path], tuple[RawFileMeta, Iterator[RawCall], ParseStats]]

logger = structlog.get_logger(__name__)

PIPELINE_VERSION: Final[str] = "pipeline_v0.2.0"
_HASH_BLOCK = 1 << 20  # 1 MiB


_PARSERS: dict[Source, _RawParser] = {
    "23andme": parsers.parse_23andme,
    "ancestry": parsers.parse_ancestry,
}


def _hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_BLOCK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _archive_file(
    src: Path,
    archive_root: Path,
    source: Source,
    file_hash: str,
) -> Path:
    """Copy ``src`` into the archive under ``<source>/<hash>__<basename>``.

    Idempotent: if the destination already exists it is left untouched. The
    archived file is chmod'd to ``0600`` to match the rest of the data layout.
    """
    bucket = archive_root / source
    bucket.mkdir(parents=True, exist_ok=True)
    dest = bucket / f"{file_hash}__{src.name}"
    if not dest.exists():
        shutil.copy2(src, dest)
    dest.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return dest


def _open_parser(
    source: Source,
    path: Path,
) -> tuple[RawFileMeta, Iterator[RawCall], ParseStats]:
    parser = _PARSERS.get(source)
    if parser is None:
        msg = f"unsupported source for raw ingest: {source!r}"
        raise ValueError(msg)
    return parser(path)


def ingest_file(  # noqa: PLR0913 — five overrides + path/source is the user-facing surface
    *,
    source: Source,
    path: Path,
    chain_file: Path | None = None,
    liftover: Liftover | None = None,
    archive_root: Path | None = None,
    duckdb_path: Path | None = None,
) -> IngestResult:
    """Run the full Phase 2 ingest pipeline on a single raw export file.

    Parameters
    ----------
    source       : the raw-export vendor; ``'23andme'`` or ``'ancestry'``.
    path         : the file on disk to ingest. Must exist.
    chain_file   : optional UCSC chain file for GRCh37→GRCh38. Required when
                   the file's native build is GRCh37 and ``liftover`` is not
                   supplied.
    liftover     : optional pre-built lift-over (overrides ``chain_file``).
                   Use :class:`liftover.IdentityLiftover` in tests where the
                   fixture is already in the target build.
    archive_root : override the archive directory. Defaults to settings.
    duckdb_path  : override the DuckDB path. Defaults to settings.

    Returns
    -------
    :class:`IngestResult` with run/qc IDs and summary counts.
    """
    if not path.is_file():
        msg = f"input file not found: {path}"
        raise FileNotFoundError(msg)

    settings = get_settings()
    archive_root = archive_root or settings.archive_path
    duckdb_path = duckdb_path or settings.genome_duckdb_path

    log = logger.bind(source=source, path=str(path))
    log.info("ingest.start")

    file_hash, file_size = _hash_file(path)
    log = log.bind(file_hash=file_hash[:12], file_size=file_size)

    archived = _archive_file(path, archive_root, source, file_hash)

    meta, raw_iter, parse_stats = _open_parser(source, path)
    log = log.bind(native_build=meta.native_build, chip_version=meta.chip_version)

    if liftover is None:
        liftover = (
            IdentityLiftover(chain_label="native_grch38")
            if meta.native_build == "GRCh38"
            else make_liftover(meta.native_build, chain_file=chain_file)
        )

    normalized: list[NormalizedCall] = list(
        normalize_calls(raw_iter, native_build=meta.native_build, liftover=liftover),
    )
    log.info(
        "ingest.normalized",
        count=len(normalized),
        dropped_alt_contig=parse_stats.dropped_alt_contig,
    )

    qc = compute_sample_qc(normalized)

    with duckdb_connection(duckdb_path) as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            run_id = insert_ingestion_run(
                conn,
                source=source,
                chip_version=meta.chip_version,
                file_path=str(archived),
                file_hash_sha256=file_hash,
                file_size_bytes=file_size,
                file_native_build=meta.native_build,
                pipeline_version=PIPELINE_VERSION,
                variants_total=qc.variants_total,
                variants_called=qc.variants_called,
                variants_no_call=qc.variants_no_call,
                variants_imputed=0,
                variants_dropped_alt_contig=parse_stats.dropped_alt_contig,
            )
            new_variants, deactivated = write_calls(
                conn,
                normalized,
                run_id=run_id,
                source=source,
                source_chip_version=meta.chip_version,
            )
            qc_id = insert_sample_qc(conn, run_id=run_id, qc=qc)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            log.exception("ingest.failed")
            raise

    result = IngestResult(
        run_id=run_id,
        qc_id=qc_id,
        source=source,
        file_path=path,
        archived_path=archived,
        file_hash_sha256=file_hash,
        file_size_bytes=file_size,
        file_native_build=meta.native_build,
        variants_total=qc.variants_total,
        variants_called=qc.variants_called,
        variants_no_call=qc.variants_no_call,
        variants_imputed=0,
        variants_dropped_alt_contig=parse_stats.dropped_alt_contig,
        new_variants_master_rows=new_variants,
        deactivated_prior_calls=deactivated,
        qc_status=qc.qc_status,
        qc_notes=qc.qc_notes,
        sex_inferred=qc.sex_inferred,
        call_rate=float(qc.call_rate),
        heterozygosity_rate=float(qc.heterozygosity_rate),
        chr_x_het_rate=float(qc.chr_x_het_rate) if qc.chr_x_het_rate is not None else None,
    )
    log.info(
        "ingest.complete",
        run_id=run_id,
        qc_id=qc_id,
        variants_total=result.variants_total,
        variants_called=result.variants_called,
        variants_dropped_alt_contig=result.variants_dropped_alt_contig,
        new_variants=new_variants,
        qc_status=qc.qc_status,
    )
    return result

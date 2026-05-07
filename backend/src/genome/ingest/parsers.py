"""Stream raw 23andMe and Ancestry exports into ``RawCall`` records."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

import structlog

from genome.ingest.models import ParseStats, RawCall, RawFileMeta, normalize_chrom

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = structlog.get_logger(__name__)

# Header look-ups.
_BUILD_38_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"build\s*38", re.IGNORECASE),
    re.compile(r"grch38", re.IGNORECASE),
    re.compile(r"hg38", re.IGNORECASE),
)
_BUILD_37_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"build\s*37", re.IGNORECASE),
    re.compile(r"grch37", re.IGNORECASE),
    re.compile(r"hg19", re.IGNORECASE),
)
_CHIP_23ANDME = re.compile(r"version[: ]*([Vv][0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_CHIP_ANCESTRY = re.compile(
    r"AncestryDNA\s+array\s+version[: ]*([Vv][0-9.]+)",
    re.IGNORECASE,
)

_TWENTYTHREE_COLS: Final[int] = 4
_ANCESTRY_COLS: Final[int] = 5


def detect_build(header_lines: list[str]) -> str:
    """Return ``'GRCh38'`` or ``'GRCh37'`` from header comments.

    Defaults to ``'GRCh37'`` (legacy 23andMe / AncestryDNA exports).
    """
    for line in header_lines:
        for pat in _BUILD_38_PATTERNS:
            if pat.search(line):
                return "GRCh38"
        for pat in _BUILD_37_PATTERNS:
            if pat.search(line):
                return "GRCh37"
    return "GRCh37"


def _detect_chip(header_lines: list[str], pattern: re.Pattern[str]) -> str | None:
    for line in header_lines:
        m = pattern.search(line)
        if m:
            return m.group(1)
    return None


def _classify_allele(token: str) -> str:
    """Return the canonical single-char allele or ``''`` when not callable."""
    t = token.strip().upper()
    if t in {"", "-", "--", "0", "00", "."}:
        return ""
    if t in {"A", "C", "G", "T", "I", "D", "N"}:
        return t
    return ""


def _split_genotype_23andme(geno: str) -> tuple[str, str, bool]:
    """Split a 23andMe genotype cell into ``(allele_1, allele_2, is_no_call)``."""
    s = geno.strip()
    if s in {"", "--", "00", "."}:
        return ("", "", True)
    if len(s) == 1:
        # Haploid: chrY / chrMT in any sample, or chrX in a male sample.
        a = _classify_allele(s)
        if not a:
            return ("", "", True)
        return (a, a, False)
    # Standard 23andMe diploid: two characters concatenated.
    a1 = _classify_allele(s[0])
    a2 = _classify_allele(s[1])
    if not a1 or not a2:
        return ("", "", True)
    return (a1, a2, False)


def _read_header(
    handle: object,
    comment_prefix: str,
) -> tuple[list[str], str | None]:
    """Consume the leading comment block; return (lines, first non-comment line)."""
    header: list[str] = []
    first_data: str | None = None
    for raw in handle:  # type: ignore[attr-defined]
        line = raw.rstrip("\n").rstrip("\r")
        if line.startswith(comment_prefix):
            header.append(line)
            continue
        if not line.strip():
            continue
        first_data = line
        break
    return header, first_data


def parse_23andme(
    path: Path,
) -> tuple[RawFileMeta, Iterator[RawCall], ParseStats]:
    """Open a 23andMe raw export and return ``(meta, iter_of_calls, stats)``.

    The iterator is a generator tied to the file; iterate it eagerly or wrap
    the call in a context manager. The ``stats`` object is mutated as the
    iterator runs and is fully populated once iteration completes. Format
    reference: tab-separated ``rsid``, ``chromosome``, ``position``,
    ``genotype`` columns; comment lines start with ``#``.
    """
    handle = path.open(encoding="utf-8", errors="replace")
    header, first_data = _read_header(handle, "#")
    meta = RawFileMeta(
        source="23andme",
        native_build=detect_build(header),
        chip_version=_detect_chip(header, _CHIP_23ANDME),
        raw_header=tuple(header),
    )
    stats = ParseStats()
    return meta, _iter_23andme_rows(handle, first_data, stats), stats


def _iter_23andme_rows(
    handle: object,
    first_data: str | None,
    stats: ParseStats,
) -> Iterator[RawCall]:
    try:
        if first_data is not None:
            yield from _emit_23andme_line(first_data, stats)
        for raw in handle:  # type: ignore[attr-defined]
            line = raw.rstrip("\n").rstrip("\r")
            if not line or line.startswith("#"):
                continue
            yield from _emit_23andme_line(line, stats)
    finally:
        handle.close()  # type: ignore[attr-defined]
        logger.info(
            "parse.non_canonical_summary",
            source="23andme",
            dropped_non_canonical=stats.dropped_non_canonical,
        )


def _emit_23andme_line(line: str, stats: ParseStats) -> Iterator[RawCall]:
    parts = line.split("\t")
    if len(parts) < _TWENTYTHREE_COLS:
        return
    if parts[0].lower() == "rsid":  # header row inside data
        return
    chrom_raw = parts[1]
    chrom = normalize_chrom(chrom_raw)
    if chrom is None:
        stats.dropped_non_canonical += 1
        logger.debug(
            "parse.non_canonical_drop",
            source="23andme",
            chrom=chrom_raw,
            rsid=parts[0],
        )
        return
    try:
        pos = int(parts[2])
    except ValueError:
        return
    a1, a2, is_no_call = _split_genotype_23andme(parts[3])
    rsid = parts[0].strip() or None
    yield RawCall(
        rsid=rsid,
        chrom=chrom,
        pos=pos,
        allele_1=a1,
        allele_2=a2,
        is_no_call=is_no_call,
    )


def parse_ancestry(
    path: Path,
) -> tuple[RawFileMeta, Iterator[RawCall], ParseStats]:
    """Open an AncestryDNA raw export and return ``(meta, iter_of_calls, stats)``.

    Format: tab-separated ``rsid``, ``chromosome``, ``position``, ``allele1``,
    ``allele2`` columns. AncestryDNA uses ``0`` (not ``-``) for no-calls and
    encodes chromosomes 1-22 numerically with ``23``=X, ``24``=Y, ``25``=PAR,
    ``26``=MT in some chip versions. The ``stats`` object is mutated as the
    iterator runs and is fully populated once iteration completes.
    """
    handle = path.open(encoding="utf-8", errors="replace")
    header, first_data = _read_header(handle, "#")
    meta = RawFileMeta(
        source="ancestry",
        native_build=detect_build(header),
        chip_version=_detect_chip(header, _CHIP_ANCESTRY),
        raw_header=tuple(header),
    )
    stats = ParseStats()
    return meta, _iter_ancestry_rows(handle, first_data, stats), stats


def _iter_ancestry_rows(
    handle: object,
    first_data: str | None,
    stats: ParseStats,
) -> Iterator[RawCall]:
    try:
        if first_data is not None:
            yield from _emit_ancestry_line(first_data, stats)
        for raw in handle:  # type: ignore[attr-defined]
            line = raw.rstrip("\n").rstrip("\r")
            if not line or line.startswith("#"):
                continue
            yield from _emit_ancestry_line(line, stats)
    finally:
        handle.close()  # type: ignore[attr-defined]
        logger.info(
            "parse.non_canonical_summary",
            source="ancestry",
            dropped_non_canonical=stats.dropped_non_canonical,
        )


def _emit_ancestry_line(line: str, stats: ParseStats) -> Iterator[RawCall]:
    parts = line.split("\t")
    if len(parts) < _ANCESTRY_COLS:
        return
    if parts[0].lower() == "rsid":
        return
    chrom_raw = parts[1]
    chrom = normalize_chrom(chrom_raw)
    if chrom is None:
        stats.dropped_non_canonical += 1
        logger.debug(
            "parse.non_canonical_drop",
            source="ancestry",
            chrom=chrom_raw,
            rsid=parts[0],
        )
        return
    try:
        pos = int(parts[2])
    except ValueError:
        return
    a1 = _classify_allele(parts[3])
    a2 = _classify_allele(parts[4])
    is_no_call = (not a1) or (not a2)
    rsid = parts[0].strip() or None
    yield RawCall(
        rsid=rsid,
        chrom=chrom,
        pos=pos,
        allele_1=a1 if not is_no_call else "",
        allele_2=a2 if not is_no_call else "",
        is_no_call=is_no_call,
    )

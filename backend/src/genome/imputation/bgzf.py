"""BGZF (blocked gzip) inspection helpers shared across the imputation pipeline.

Beagle writes its per-chromosome output as BGZF (htslib's blocked gzip). A
cleanly-closed BGZF file ends with a fixed 28-byte empty-block EOF marker; its
absence means the writer died mid-stream — the truncated ``result/chrX.vcf.gz``
of finding-008 #2, which cyvcf2 reads as zero variants with only a
``[W::bgzf_read_block] EOF marker is absent`` warning rather than an error.

Two Phase-4 steps need to detect that truncation, and they must agree on what
"truncated" means, so the predicate lives here once:

* the import step (:mod:`genome.imputation.ingest`) refuses a truncated result
  VCF rather than importing it as a silent empty success;
* the runner (:mod:`genome.imputation.beagle_runner`) treats a truncated output
  as "not complete" so its resumable skip-existing check re-imputes instead of
  skipping a half-written file.

Plain gzip — our synthetic test fixtures and the prepare-step upload VCFs — is
exempt: it carries no BGZF EOF marker to be missing, so it is never "truncated"
by this module's definition.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

# A cleanly-closed BGZF file ends with this 28-byte empty-block EOF marker
# (htslib's ``BGZF_EOF``). Its absence on a BGZF file means the writer died
# mid-stream.
BGZF_EOF_MARKER: Final[bytes] = bytes.fromhex(
    "1f8b08040000000000ff0600424302001b0003000000000000000000",
)

# A BGZF block starts with the gzip+deflate magic (1f 8b 08) and sets the
# FEXTRA flag (0x04) for its per-block size subfield. Plain gzip leaves FEXTRA
# unset, so this distinguishes real Beagle BGZF output from plain gzip and
# limits the EOF-marker check to the former.
_GZIP_DEFLATE_MAGIC: Final[bytes] = b"\x1f\x8b\x08"
_GZIP_FLG_FEXTRA: Final[int] = 0x04


def is_bgzf(path: Path) -> bool:
    """Return True if ``path`` is a BGZF file (vs. plain gzip / uncompressed).

    Beagle writes BGZF; our synthetic test fixtures and the prepare-step upload
    VCFs use plain ``gzip.open``. Only BGZF carries the EOF marker checked by
    :func:`has_bgzf_eof`, so plain gzip is exempt from the truncation guard.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    return (
        head.startswith(_GZIP_DEFLATE_MAGIC)
        and len(head) > len(_GZIP_DEFLATE_MAGIC)
        and bool(head[3] & _GZIP_FLG_FEXTRA)
    )


def has_bgzf_eof(path: Path) -> bool:
    """Return True if BGZF ``path`` ends with the canonical 28-byte EOF marker."""
    marker_len = len(BGZF_EOF_MARKER)
    try:
        if path.stat().st_size < marker_len:
            return False
        with path.open("rb") as fh:
            fh.seek(-marker_len, os.SEEK_END)
            return fh.read(marker_len) == BGZF_EOF_MARKER
    except OSError:
        return False


def is_truncated_bgzf(path: Path) -> bool:
    """Return True if ``path`` is a BGZF file missing its EOF marker.

    The single definition of "truncated" shared by the import guard
    (:func:`genome.imputation.ingest._assert_result_vcf_intact`) and the runner's
    resumable skip-existing check. A non-BGZF file — plain gzip, uncompressed, or
    absent — is never "truncated" by this definition: plain gzip carries no EOF
    marker to be missing, which is exactly why the synthetic fixtures and upload
    VCFs are exempt from the guard.
    """
    return is_bgzf(path) and not has_bgzf_eof(path)


__all__ = [
    "BGZF_EOF_MARKER",
    "has_bgzf_eof",
    "is_bgzf",
    "is_truncated_bgzf",
]

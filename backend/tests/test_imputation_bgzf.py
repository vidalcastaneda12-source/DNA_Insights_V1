"""Tests for :mod:`genome.imputation.bgzf` — the shared BGZF truncation helpers.

The import guard and the runner's resumable skip-existing check both depend on
the single definition of "truncated BGZF" living here (finding-008 #2), so this
pins the primitive against hand-built byte fixtures and, when available, against
real ``bgzip`` output.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from genome.imputation.bgzf import (
    BGZF_EOF_MARKER,
    has_bgzf_eof,
    is_bgzf,
    is_truncated_bgzf,
)

if TYPE_CHECKING:
    from pathlib import Path

# A BGZF block opens with the gzip+deflate magic (1f 8b 08) and the FEXTRA flag
# (0x04). These four bytes are all :func:`is_bgzf` inspects, so they are enough
# to make a fixture "look like" BGZF; whether it is *truncated* then turns on
# the trailing 28-byte EOF marker.
_BGZF_HEAD = b"\x1f\x8b\x08\x04"


def test_eof_marker_is_a_minimal_valid_bgzf() -> None:
    """The canonical EOF marker is itself a complete (empty-block) BGZF file."""
    assert BGZF_EOF_MARKER[:4] == _BGZF_HEAD
    assert len(BGZF_EOF_MARKER) == 28


def test_is_bgzf_true_for_bgzf_head(tmp_path: Path) -> None:
    path = tmp_path / "looks_like_bgzf.gz"
    path.write_bytes(_BGZF_HEAD + b"\x00" * 64)
    assert is_bgzf(path) is True


def test_is_bgzf_false_for_plain_gzip(tmp_path: Path) -> None:
    path = tmp_path / "plain.gz"
    with gzip.open(path, "wt", encoding="ascii") as out:
        out.write("hello\n")
    # Plain gzip leaves the FEXTRA flag unset, so it is not BGZF.
    assert is_bgzf(path) is False
    assert is_truncated_bgzf(path) is False


def test_is_bgzf_false_for_uncompressed_and_missing(tmp_path: Path) -> None:
    text = tmp_path / "plain.txt"
    text.write_text("not gzip at all")
    assert is_bgzf(text) is False
    missing = tmp_path / "nope.gz"
    assert is_bgzf(missing) is False
    assert has_bgzf_eof(missing) is False
    assert is_truncated_bgzf(missing) is False


def test_valid_bgzf_has_eof_and_is_not_truncated(tmp_path: Path) -> None:
    path = tmp_path / "valid.bgz"
    path.write_bytes(_BGZF_HEAD + b"\x00" * 64 + BGZF_EOF_MARKER)
    assert is_bgzf(path) is True
    assert has_bgzf_eof(path) is True
    assert is_truncated_bgzf(path) is False


def test_truncated_bgzf_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "truncated.bgz"
    # BGZF magic but no trailing EOF marker — the finding-008 #2 shape.
    path.write_bytes(_BGZF_HEAD + b"\x00" * 64)
    assert is_bgzf(path) is True
    assert has_bgzf_eof(path) is False
    assert is_truncated_bgzf(path) is True


def test_bgzf_shorter_than_marker_is_truncated(tmp_path: Path) -> None:
    path = tmp_path / "tiny.bgz"
    path.write_bytes(_BGZF_HEAD)  # 4 bytes — smaller than the 28-byte marker
    assert is_bgzf(path) is True
    assert has_bgzf_eof(path) is False
    assert is_truncated_bgzf(path) is True


@pytest.mark.skipif(shutil.which("bgzip") is None, reason="bgzip not installed")
def test_real_bgzip_output_round_trips(tmp_path: Path) -> None:
    """A file produced by the real ``bgzip`` binary validates as complete BGZF."""
    bgzip = shutil.which("bgzip")
    assert bgzip is not None
    src = tmp_path / "payload.txt"
    src.write_text("the quick brown fox\n" * 200)
    proc = subprocess.run(  # noqa: S603 — bgzip path resolved via shutil.which
        [bgzip, "-c", str(src)],
        capture_output=True,
        check=True,
    )
    out = tmp_path / "payload.txt.gz"
    out.write_bytes(proc.stdout)
    assert is_bgzf(out) is True
    assert has_bgzf_eof(out) is True
    assert is_truncated_bgzf(out) is False

    # Lopping the 28-byte EOF marker off turns it into a truncated BGZF.
    truncated = tmp_path / "payload.trunc.gz"
    truncated.write_bytes(out.read_bytes()[: -len(BGZF_EOF_MARKER)])
    assert is_truncated_bgzf(truncated) is True

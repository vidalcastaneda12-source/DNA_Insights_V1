"""Tests for :mod:`genome.par_regions` — GRCh38 chrX PAR boundaries (PR 5a).

The view-predicate parity (the DDL view's non-PAR predicate must equal
``is_nonpar``) is asserted in the corrected-dosage view test, where the view
exists; here we pin the Python predicate, including the telomeric slivers.
"""

from __future__ import annotations

import pytest

from genome.par_regions import (
    PAR1_END,
    PAR1_START,
    PAR2_END,
    PAR2_START,
    is_nonpar,
    is_par,
)


@pytest.mark.parametrize(
    ("pos", "expected_par"),
    [
        # PAR1 boundaries.
        (PAR1_START, True),
        (PAR1_END, True),
        (PAR1_START - 1, False),  # 10,000 — the lower telomeric sliver
        (PAR1_END + 1, False),  # 2,781,480 — first non-PAR core position
        # PAR2 boundaries.
        (PAR2_START, True),
        (PAR2_END, True),
        (PAR2_START - 1, False),  # last non-PAR core position
        (PAR2_END + 1, False),  # 156,030,896 — the upper telomeric sliver
        # Non-PAR core interior.
        (50_000_000, False),
        # Telomeric slivers (outside the PAR windows) are non-PAR.
        (1, False),
        (5_000, False),
        (156_040_895, False),  # chrX length on GRCh38
    ],
)
def test_is_par_boundaries_and_slivers(pos: int, expected_par: bool) -> None:  # noqa: FBT001
    assert is_par(pos) is expected_par
    assert is_nonpar(pos) is (not expected_par)


def test_slivers_are_nonpar_not_par() -> None:
    """The two telomeric slivers must be non-PAR (hemizygous in males)."""
    assert is_nonpar(1) is True
    assert is_nonpar(PAR1_START - 1) is True
    assert is_nonpar(PAR2_END + 1) is True
    assert is_nonpar(156_040_895) is True


def test_is_nonpar_is_strict_complement_of_is_par() -> None:
    for pos in (
        1,
        10_000,
        PAR1_START,
        1_000_000,
        PAR1_END,
        2_781_480,
        80_000_000,
        PAR2_START,
        PAR2_END,
        156_030_896,
        156_040_895,
    ):
        assert is_nonpar(pos) is (not is_par(pos))

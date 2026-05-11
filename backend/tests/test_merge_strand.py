"""Unit tests for the strand helpers.

These are pure-Python functions so they can be exercised independently of
DuckDB. The pipeline-level coverage in ``test_merge_pipeline.py`` re-tests
them in context.
"""

from __future__ import annotations

import pytest

from genome.merge.strand import (
    complement,
    complement_pair,
    is_palindromic_site,
    sorted_pair,
)


@pytest.mark.parametrize(
    ("base", "expected"),
    [("A", "T"), ("T", "A"), ("C", "G"), ("G", "C")],
)
def test_complement_dna_bases(base: str, expected: str) -> None:
    assert complement(base) == expected


@pytest.mark.parametrize("token", ["N", "I", "D", ""])
def test_complement_passes_through_non_dna_tokens(token: str) -> None:
    """Non-DNA tokens have no defined complement; the helper returns them as-is."""
    assert complement(token) == token


@pytest.mark.parametrize(
    ("ref", "alt"),
    [("A", "T"), ("T", "A"), ("C", "G"), ("G", "C")],
)
def test_is_palindromic_site_true_for_at_and_cg(ref: str, alt: str) -> None:
    assert is_palindromic_site(ref, alt) is True


@pytest.mark.parametrize(
    ("ref", "alt"),
    [("A", "G"), ("A", "C"), ("G", "T"), ("C", "T")],
)
def test_is_palindromic_site_false_for_non_palindromic(ref: str, alt: str) -> None:
    assert is_palindromic_site(ref, alt) is False


def test_is_palindromic_site_false_when_alleles_identical() -> None:
    """A monomorphic site is not a palindromic discrepancy candidate."""
    assert is_palindromic_site("A", "A") is False


def test_sorted_pair_orders_alphabetically() -> None:
    assert sorted_pair("G", "A") == ("A", "G")
    assert sorted_pair("A", "G") == ("A", "G")
    assert sorted_pair("T", "T") == ("T", "T")


def test_complement_pair_resolves_non_palindromic_flip() -> None:
    """``A/G`` on plus strand == ``C/T`` on minus strand after complement + sort."""
    assert complement_pair("C", "T") == ("A", "G")
    assert complement_pair("T", "C") == ("A", "G")


def test_complement_pair_round_trips_palindromic_sites() -> None:
    """``A/T`` and ``C/G`` complement to themselves — that's why they're ambiguous."""
    assert complement_pair("A", "T") == ("A", "T")
    assert complement_pair("C", "G") == ("C", "G")

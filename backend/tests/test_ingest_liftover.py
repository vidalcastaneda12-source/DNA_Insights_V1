"""Tests for the lift-over engines.

Covers :class:`IdentityLiftover` semantics (already exercised in
test_ingest_normalize but kept here for completeness), :class:`BcftoolsLiftover`
correctness against a synthetic chain, ``make_liftover`` engine selection,
and a 100K-variant benchmark that asserts completion within 60 seconds. The
benchmark pins the bcftools path against future regressions to per-variant
subprocess invocations.
"""

from __future__ import annotations

import shutil
import time
from typing import TYPE_CHECKING

import pytest

from genome.ingest.liftover import (
    BcftoolsLiftover,
    IdentityLiftover,
    PyLiftover,
    make_liftover,
)

if TYPE_CHECKING:
    from pathlib import Path


bcftools_required = pytest.mark.skipif(
    shutil.which("bcftools") is None,
    reason="bcftools not on $PATH",
)


def _write_identity_chain(
    path: Path,
    *,
    chrom: str = "chr1",
    size: int = 1_000_000,
) -> None:
    """Write a single-block identity-mapping chain (source pos == destination pos)."""
    path.write_text(
        f"chain {size} {chrom} {size} + 0 {size} {chrom} {size} + 0 {size} 1\n{size}\n\n",
    )


def _write_multi_chrom_chain(path: Path, contigs: dict[str, int]) -> None:
    """Write one identity chain entry per ``(name, size)`` pair."""
    blocks: list[str] = []
    for i, (name, size) in enumerate(contigs.items(), start=1):
        blocks.append(
            f"chain {size} {name} {size} + 0 {size} {name} {size} + 0 {size} {i}\n{size}\n\n",
        )
    path.write_text("".join(blocks))


# --- IdentityLiftover ---


def test_identity_liftover_returns_input_unchanged() -> None:
    lo = IdentityLiftover(chain_label="test_label")
    assert lo.chain_label == "test_label"
    assert lo.lift("1", 100) == ("1", 100)
    assert lo.lift("X", 1_000_000) == ("X", 1_000_000)


# --- BcftoolsLiftover ---


@bcftools_required
def test_bcftools_liftover_identity_chain_round_trips(tmp_path: Path) -> None:
    chain = tmp_path / "identity.chain"
    _write_identity_chain(chain)
    lo = BcftoolsLiftover(chain, chain_label="identity_test")
    coords = [("1", 1), ("1", 1_000), ("1", 999_999)]
    lo.prepare(coords)
    for chrom, pos in coords:
        assert lo.lift(chrom, pos) == (chrom, pos), f"{chrom}:{pos} did not round-trip"
    assert lo.chain_label == "identity_test"


@bcftools_required
def test_bcftools_liftover_handles_multiple_chroms(tmp_path: Path) -> None:
    chain = tmp_path / "multi.chain"
    _write_multi_chrom_chain(
        chain,
        {"chr1": 100_000, "chr2": 100_000, "chrX": 100_000, "chrM": 16_569},
    )
    lo = BcftoolsLiftover(chain)
    coords = [("1", 50), ("2", 1_000), ("X", 99_999), ("MT", 100)]
    lo.prepare(coords)
    for c in coords:
        assert lo.lift(*c) == c


@bcftools_required
def test_bcftools_liftover_returns_none_for_unknown_contig(tmp_path: Path) -> None:
    """A coordinate on a contig that's not in the chain → ``None`` (lift failed)."""
    chain = tmp_path / "chr1_only.chain"
    _write_identity_chain(chain, chrom="chr1", size=1_000_000)
    lo = BcftoolsLiftover(chain)
    lo.prepare([("3", 100)])
    assert lo.lift("3", 100) is None


@bcftools_required
def test_bcftools_liftover_returns_none_for_position_outside_chain(
    tmp_path: Path,
) -> None:
    chain = tmp_path / "small.chain"
    _write_identity_chain(chain, size=1_000)
    lo = BcftoolsLiftover(chain)
    lo.prepare([("1", 500), ("1", 1_000_000)])
    # The first coord is in range; the second is past the chain end.
    assert lo.lift("1", 500) == ("1", 500)
    assert lo.lift("1", 1_000_000) is None


@bcftools_required
def test_bcftools_liftover_caches_across_prepare_calls(tmp_path: Path) -> None:
    chain = tmp_path / "identity.chain"
    _write_identity_chain(chain)
    lo = BcftoolsLiftover(chain)
    lo.prepare([("1", 100)])
    lo.prepare([("1", 200)])
    # Both queries answer from the cache without a third bcftools call.
    assert lo.lift("1", 100) == ("1", 100)
    assert lo.lift("1", 200) == ("1", 200)


@bcftools_required
def test_bcftools_liftover_lift_without_prepare_falls_back(tmp_path: Path) -> None:
    """``lift()`` without a prior ``prepare()`` runs a one-shot subprocess.

    Slower than prepare()-then-lift() but correct, so it's the documented
    fallback for non-batched callers.
    """
    chain = tmp_path / "identity.chain"
    _write_identity_chain(chain)
    lo = BcftoolsLiftover(chain)
    assert lo.lift("1", 42) == ("1", 42)


@bcftools_required
def test_bcftools_liftover_rejects_missing_chain_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="chain file"):
        BcftoolsLiftover(tmp_path / "does_not_exist.chain")


@bcftools_required
def test_bcftools_liftover_rejects_empty_chain_file(tmp_path: Path) -> None:
    chain = tmp_path / "empty.chain"
    chain.write_text("# no chain headers in here\n")
    with pytest.raises(ValueError, match="no parseable chain headers"):
        BcftoolsLiftover(chain)


def test_bcftools_liftover_rejects_when_bcftools_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = tmp_path / "identity.chain"
    _write_identity_chain(chain)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match=r"not on \$PATH"):
        BcftoolsLiftover(chain)


# --- make_liftover engine selection ---


def test_make_liftover_grch38_returns_identity() -> None:
    lo = make_liftover("GRCh38")
    assert isinstance(lo, IdentityLiftover)
    assert lo.chain_label == "native_grch38"


def test_make_liftover_grch37_requires_chain_file() -> None:
    with pytest.raises(ValueError, match="chain file"):
        make_liftover("GRCh37")


def test_make_liftover_unsupported_build_raises() -> None:
    with pytest.raises(ValueError, match="unsupported native build"):
        make_liftover("GRCh36")


@bcftools_required
def test_make_liftover_grch37_default_picks_bcftools(tmp_path: Path) -> None:
    chain = tmp_path / "identity.chain"
    _write_identity_chain(chain)
    lo = make_liftover("GRCh37", chain_file=chain)
    assert isinstance(lo, BcftoolsLiftover)
    assert lo.chain_label == "hg19_to_hg38"


@bcftools_required
def test_make_liftover_grch37_explicit_bcftools(tmp_path: Path) -> None:
    chain = tmp_path / "identity.chain"
    _write_identity_chain(chain)
    lo = make_liftover("GRCh37", chain_file=chain, engine="bcftools")
    assert isinstance(lo, BcftoolsLiftover)


def test_make_liftover_grch37_explicit_pyliftover(tmp_path: Path) -> None:
    chain = tmp_path / "identity.chain"
    _write_identity_chain(chain)
    lo = make_liftover("GRCh37", chain_file=chain, engine="pyliftover")
    assert isinstance(lo, PyLiftover)


def test_make_liftover_auto_falls_back_when_bcftools_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = tmp_path / "identity.chain"
    _write_identity_chain(chain)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    lo = make_liftover("GRCh37", chain_file=chain, engine="auto")
    assert isinstance(lo, PyLiftover)


def test_make_liftover_unsupported_engine_raises(tmp_path: Path) -> None:
    chain = tmp_path / "identity.chain"
    _write_identity_chain(chain)
    with pytest.raises(ValueError, match="unsupported liftover engine"):
        make_liftover("GRCh37", chain_file=chain, engine="crossmap")  # type: ignore[arg-type]


# --- Benchmark ---


@bcftools_required
def test_bcftools_liftover_100k_variants_under_60s(tmp_path: Path) -> None:
    """Lift 100K synthetic variants in well under 60 seconds.

    Catches a regression to per-variant subprocess invocations (which would
    take ~minutes for 100K calls) or to a chain-parse codepath that's
    accidentally O(N²).
    """
    chain = tmp_path / "benchmark.chain"
    _write_identity_chain(chain, chrom="chr1", size=100_000_000)
    lo = BcftoolsLiftover(chain, chain_label="benchmark")
    coords = [("1", i * 100 + 1) for i in range(100_000)]

    start = time.monotonic()
    lo.prepare(coords)
    elapsed = time.monotonic() - start

    assert elapsed < 60, (
        f"BcftoolsLiftover.prepare(100K coords) took {elapsed:.1f}s (>= 60s budget)"
    )
    # Spot-check correctness — identity chain must round-trip every coord.
    sample = coords[::1000]
    for chrom, pos in sample:
        assert lo.lift(chrom, pos) == (chrom, pos)


@bcftools_required
def test_bcftools_liftover_chrom_label_round_trip(tmp_path: Path) -> None:
    """Internal ``MT``/``X``/``Y`` labels survive UCSC ↔ internal conversion."""
    chain = tmp_path / "multi.chain"
    _write_multi_chrom_chain(
        chain,
        {"chrM": 16_569, "chrX": 100_000, "chrY": 100_000, "chr1": 100_000},
    )
    lo = BcftoolsLiftover(chain)
    lo.prepare([("MT", 100), ("X", 50_000), ("Y", 50_000), ("1", 1_000)])
    assert lo.lift("MT", 100) == ("MT", 100)
    assert lo.lift("X", 50_000) == ("X", 50_000)
    assert lo.lift("Y", 50_000) == ("Y", 50_000)
    assert lo.lift("1", 1_000) == ("1", 1_000)

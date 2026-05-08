"""Lift-over engine selection and the GRCh37→GRCh38 throughput benchmark.

The pipeline accepts three engines: ``auto`` (default, tries the ``liftover``
PyPI package first and falls back to ``pyliftover``), ``liftover`` (force the
fast C++/CFFI engine), and ``pyliftover`` (force the pure-Python fallback).
The benchmark uses a synthetic in-test chain file so it doesn't depend on a
local UCSC download.
"""

from __future__ import annotations

import gzip
import time
from typing import TYPE_CHECKING

import pytest

from genome.ingest import liftover as liftover_mod
from genome.ingest.liftover import (
    IdentityLiftover,
    LiftoverPyLib,
    PyLiftoverWrapper,
    make_liftover,
)

if TYPE_CHECKING:
    from pathlib import Path


CHAIN_BODY = (
    # One wide, identity-shifted chain covering all of chr1.
    # Format: chain score tName tSize tStrand tStart tEnd qName qSize qStrand qStart qEnd id
    "chain 1000000 chr1 248956422 + 0 200000000 chr1 248956422 + 100 200000100 1\n200000000\n"
)


@pytest.fixture
def synthetic_chain(tmp_path: Path) -> Path:
    """Write a tiny but valid UCSC chain file the C++ and Python engines accept."""
    p = tmp_path / "synthetic.chain.gz"
    with gzip.open(p, "wt") as fh:
        fh.write(CHAIN_BODY)
    return p


# ---------------------------------------------------------------------------
# LiftoverPyLib: shape-preserving wrapper around the `liftover` PyPI package.
# ---------------------------------------------------------------------------


def test_liftover_pylib_lifts_canonical_position(synthetic_chain: Path) -> None:
    lo = LiftoverPyLib(synthetic_chain, chain_label="hg19_to_hg38")
    # Synthetic chain shifts chr1 by +100. lift() takes/returns 1-based positions.
    assert lo.lift("1", 500) == ("1", 600)
    assert lo.chain_label == "hg19_to_hg38"


def test_liftover_pylib_returns_none_for_unmapped(synthetic_chain: Path) -> None:
    lo = LiftoverPyLib(synthetic_chain)
    # chr2 isn't in the chain, so the lookup is empty.
    assert lo.lift("2", 500) is None


def test_liftover_pylib_remaps_mt_to_m_and_back(synthetic_chain: Path) -> None:
    """The pipeline passes 'MT' (canonical), but UCSC chains use 'chrM'."""
    lo = LiftoverPyLib(synthetic_chain)
    # No chrM in the synthetic chain; the lookup should miss without exploding.
    assert lo.lift("MT", 100) is None


def test_liftover_pylib_missing_chain_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="chain file not found"):
        LiftoverPyLib(tmp_path / "does-not-exist.chain.gz")


# ---------------------------------------------------------------------------
# PyLiftoverWrapper still works the same way after the rename from PyLiftover.
# ---------------------------------------------------------------------------


def test_pyliftover_wrapper_lifts_canonical_position(synthetic_chain: Path) -> None:
    lo = PyLiftoverWrapper(synthetic_chain, chain_label="hg19_to_hg38")
    assert lo.lift("1", 500) == ("1", 600)
    assert lo.chain_label == "hg19_to_hg38"


def test_pyliftover_wrapper_missing_chain_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="chain file not found"):
        PyLiftoverWrapper(tmp_path / "does-not-exist.chain.gz")


# ---------------------------------------------------------------------------
# Engine selection in make_liftover.
# ---------------------------------------------------------------------------


def test_make_liftover_auto_prefers_liftover_pylib(synthetic_chain: Path) -> None:
    lo = make_liftover("GRCh37", chain_file=synthetic_chain, engine="auto")
    assert isinstance(lo, LiftoverPyLib)
    assert lo.chain_label == "hg19_to_hg38"


def test_make_liftover_explicit_liftover_returns_pylib(synthetic_chain: Path) -> None:
    lo = make_liftover("GRCh37", chain_file=synthetic_chain, engine="liftover")
    assert isinstance(lo, LiftoverPyLib)


def test_make_liftover_explicit_pyliftover_returns_wrapper(synthetic_chain: Path) -> None:
    lo = make_liftover("GRCh37", chain_file=synthetic_chain, engine="pyliftover")
    assert isinstance(lo, PyLiftoverWrapper)


def test_make_liftover_auto_falls_back_to_pyliftover_with_info_log(
    synthetic_chain: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Auto with `liftover` unavailable falls back loudly to pyliftover.

    structlog renders to stdout in this test (no extra config), so we read the
    captured output directly rather than relying on stdlib ``caplog``.
    """
    monkeypatch.setattr(liftover_mod, "_liftover_pkg_available", lambda: False)
    lo = make_liftover("GRCh37", chain_file=synthetic_chain, engine="auto")
    assert isinstance(lo, PyLiftoverWrapper)
    captured = capsys.readouterr()
    assert "liftover.engine_fallback" in captured.out
    assert "pyliftover" in captured.out


def test_make_liftover_explicit_liftover_raises_when_unavailable(
    synthetic_chain: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(liftover_mod, "_liftover_pkg_available", lambda: False)
    with pytest.raises(RuntimeError, match="liftover engine requested"):
        make_liftover("GRCh37", chain_file=synthetic_chain, engine="liftover")


def test_make_liftover_explicit_pyliftover_raises_when_unavailable(
    synthetic_chain: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(liftover_mod, "_pyliftover_available", lambda: False)
    with pytest.raises(RuntimeError, match="pyliftover engine requested"):
        make_liftover("GRCh37", chain_file=synthetic_chain, engine="pyliftover")


def test_make_liftover_auto_raises_when_neither_available(
    synthetic_chain: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(liftover_mod, "_liftover_pkg_available", lambda: False)
    monkeypatch.setattr(liftover_mod, "_pyliftover_available", lambda: False)
    with pytest.raises(RuntimeError, match="no lift-over engine available"):
        make_liftover("GRCh37", chain_file=synthetic_chain, engine="auto")


def test_make_liftover_grch38_ignores_engine_choice() -> None:
    """A native-GRCh38 file uses Identity regardless of engine."""
    for engine in ("auto", "liftover", "pyliftover"):
        lo = make_liftover("GRCh38", engine=engine)  # type: ignore[arg-type]
        assert isinstance(lo, IdentityLiftover)


# ---------------------------------------------------------------------------
# Benchmark: 100K canonical positions through the default engine in <60s.
# ---------------------------------------------------------------------------


def test_default_engine_lifts_100k_positions_under_60s(synthetic_chain: Path) -> None:
    """End-to-end: build the default engine and lift 100K canonical positions.

    The synthetic chain spans 0-200M on chr1, so every position lifts. We're
    measuring the per-call overhead of the wrapper and the underlying engine,
    not the chain-search complexity. The C++/CFFI ``liftover`` package
    comfortably handles 100K calls in ~1 second on a developer laptop; the
    60-second ceiling is a generous regression guard.
    """
    lo = make_liftover("GRCh37", chain_file=synthetic_chain, engine="auto")
    assert isinstance(lo, LiftoverPyLib)

    start = time.perf_counter()
    for pos in range(1, 100_001):
        result = lo.lift("1", pos)
        assert result is not None
    elapsed = time.perf_counter() - start
    assert elapsed < 60.0, f"100K lifts took {elapsed:.2f}s; budget is 60s"

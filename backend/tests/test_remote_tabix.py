"""Tests for :mod:`genome.annotate.remote_tabix`.

Covers the pure helpers (``coalesce_positions``, ``_scan_for_htslib_errors``),
the libcurl pre-flight probe (``check_libcurl_available``), and the
open -> iterate -> detect -> reopen -> retry generator
(``iter_remote_vcf_regions``) including the re-yield-on-reopen behaviour the
caller is expected to dedup. The generator is exercised with an injected
``open_fn`` (a fake VCF factory) so the tests need neither cyvcf2 nor the
network; the corrupting fake writes the htslib error tokens to the live fd 2
exactly as htslib does, so the real ``_StderrTap`` corruption detector runs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from genome.annotate.remote_tabix import (
    MAX_REMOTE_REGION_ATTEMPTS,
    RemoteTabixIterationError,
    RemoteTabixLibcurlMissingError,
    _scan_for_htslib_errors,
    check_libcurl_available,
    coalesce_positions,
    iter_remote_vcf_regions,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# Real htslib BGZF / libcurl HTTP/2 framing error lines, written to fd 2 by the
# corrupting fake so the _StderrTap detector trips exactly as in production.
_HTSLIB_TOKENS: bytes = (
    b"[E::easy_errno] Libcurl reported error 16 (Error in the HTTP2 framing layer)\n"
    b"[E::bgzf_read_block] Failed to read BGZF block data at offset 100\n"
)


@dataclass
class _Rec:
    """Minimal stand-in for a cyvcf2 record — the generator only re-yields it."""

    pos: int


@dataclass
class _FakeVCF:
    """Fake cyvcf2.VCF whose ``__call__`` returns records inside the region.

    A region in ``corrupt_regions`` writes the htslib tokens to fd 2 and yields
    nothing (htslib's silent-after-corruption behaviour). A region in
    ``partial_then_corrupt`` yields the first matching record, then writes the
    tokens — simulating a mid-stream corruption that leaves a partial yield.
    """

    records: list[_Rec]
    corrupt_regions: set[str] = field(default_factory=set)
    partial_then_corrupt: set[str] = field(default_factory=set)
    opened_regions: list[str] = field(default_factory=list)

    def __call__(self, region: str) -> Iterable[_Rec]:
        self.opened_regions.append(region)
        matching = self._match(region)
        if region in self.partial_then_corrupt:

            def _gen() -> Iterator[_Rec]:
                if matching:
                    yield matching[0]
                os.write(2, _HTSLIB_TOKENS)

            return _gen()
        if region in self.corrupt_regions:
            os.write(2, _HTSLIB_TOKENS)
            return iter(())
        return iter(matching)

    def _match(self, region: str) -> list[_Rec]:
        m = re.match(r"[^:]+:(\d+)-(\d+)", region)
        if m is None:
            return []
        start, end = int(m.group(1)), int(m.group(2))
        return [r for r in self.records if start <= r.pos <= end]

    def close(self) -> None:
        return


class _Factory:
    """Builds :class:`_FakeVCF`, applying corruption only on the first open.

    ``corrupt_first`` / ``partial_first`` configure the first open's fake;
    subsequent opens (reopens) return clean fakes so the generator's retry
    recovers. ``always_corrupt`` corrupts on every open (recovery never
    succeeds). ``opens`` counts opens so tests can pin the reopen budget.
    """

    def __init__(
        self,
        records: list[_Rec],
        *,
        corrupt_first: set[str] | None = None,
        always_corrupt: set[str] | None = None,
        partial_first: set[str] | None = None,
    ) -> None:
        self.records = records
        self.corrupt_first = corrupt_first or set()
        self.always_corrupt = always_corrupt or set()
        self.partial_first = partial_first or set()
        self.opens = 0

    def __call__(self, _url: str) -> _FakeVCF:
        is_first = self.opens == 0
        self.opens += 1
        corrupt = set(self.always_corrupt)
        if is_first:
            corrupt |= self.corrupt_first
        return _FakeVCF(
            records=self.records,
            corrupt_regions=corrupt,
            partial_then_corrupt=self.partial_first if is_first else set(),
        )


# ---------------------------------------------------------------------------
# coalesce_positions
# ---------------------------------------------------------------------------


def test_coalesce_positions_merges_within_gap() -> None:
    assert coalesce_positions([100, 500, 1000, 1500, 5000], 1000) == [(100, 1500), (5000, 5000)]


def test_coalesce_positions_custom_threshold_splits() -> None:
    assert coalesce_positions([100, 500, 1000], 100) == [(100, 100), (500, 500), (1000, 1000)]


def test_coalesce_positions_empty() -> None:
    assert coalesce_positions([], 50000) == []


def test_coalesce_positions_single() -> None:
    assert coalesce_positions([42], 50000) == [(42, 42)]


# ---------------------------------------------------------------------------
# _scan_for_htslib_errors
# ---------------------------------------------------------------------------


def test_scan_for_htslib_errors_recognizes_tokens() -> None:
    assert _scan_for_htslib_errors(b"") is False
    assert _scan_for_htslib_errors(b"benign output") is False
    assert _scan_for_htslib_errors(b"[E::easy_errno] Libcurl error 16") is True
    assert _scan_for_htslib_errors(b"[E::bgzf_read_block] Failed at offset 100") is True
    assert _scan_for_htslib_errors(b"[E::hts_itr_next] Failed to seek: Illegal seek") is True


# ---------------------------------------------------------------------------
# check_libcurl_available
# ---------------------------------------------------------------------------


def test_check_libcurl_available_pass() -> None:
    """A successful open + iter does not raise."""
    check_libcurl_available(
        "https://example/vcf.gz",
        "chr1:1-2",
        open_fn=lambda _url: _FakeVCF(records=[]),
    )


def test_check_libcurl_available_fail_raises_clear_error() -> None:
    """An open failure surfaces as RemoteTabixLibcurlMissingError mentioning libcurl."""

    def _boom(_url: str) -> _FakeVCF:
        msg = "tabix index failed (htslib without libcurl)"
        raise RuntimeError(msg)

    with pytest.raises(RemoteTabixLibcurlMissingError, match="libcurl"):
        check_libcurl_available("https://example/vcf.gz", "chr1:1-2", open_fn=_boom)


# ---------------------------------------------------------------------------
# iter_remote_vcf_regions
# ---------------------------------------------------------------------------


def test_iter_remote_vcf_regions_yields_all_records() -> None:
    """No corruption -> every record in the region is yielded once, one open."""
    factory = _Factory([_Rec(100), _Rec(200)])
    out = list(
        iter_remote_vcf_regions(
            "https://example/vcf.gz",
            ["r:100-200"],
            event_prefix="test",
            open_fn=factory,
        ),
    )
    assert [r.pos for r in out] == [100, 200]
    assert factory.opens == 1


def test_iter_remote_vcf_regions_recovers_from_corruption() -> None:
    """First open corrupts the region -> reopen + retry lands the records."""
    factory = _Factory([_Rec(100), _Rec(200)], corrupt_first={"r:100-200"})
    out = list(
        iter_remote_vcf_regions(
            "https://example/vcf.gz",
            ["r:100-200"],
            event_prefix="test",
            open_fn=factory,
        ),
    )
    assert [r.pos for r in out] == [100, 200]
    # initial open (corrupt) + at least one reopen.
    assert factory.opens >= 2


def test_iter_remote_vcf_regions_reyields_partial_on_reopen() -> None:
    """A partial yield before corruption is re-yielded after reopen.

    The generator does NOT dedup — it re-yields the first record on the retry.
    Caller-side dedup is what collapses the duplicate; this test pins that the
    re-yield reaches the caller (so dedup is genuinely needed).
    """
    factory = _Factory([_Rec(100), _Rec(200)], partial_first={"r:100-200"})
    out = list(
        iter_remote_vcf_regions(
            "https://example/vcf.gz",
            ["r:100-200"],
            event_prefix="test",
            open_fn=factory,
        ),
    )
    # First attempt yielded 100 then corrupted; retry re-yielded 100 and 200.
    assert [r.pos for r in out] == [100, 100, 200]
    assert factory.opens >= 2


def test_iter_remote_vcf_regions_raises_after_max_attempts() -> None:
    """Persistent corruption on a region exhausts the budget and raises.

    The open count equals MAX_REMOTE_REGION_ATTEMPTS exactly: one initial open
    plus (MAX - 1) reopens; the final attempt does not reopen.
    """
    factory = _Factory([_Rec(100)], always_corrupt={"r:100-100"})
    with pytest.raises(RemoteTabixIterationError, match="region"):
        list(
            iter_remote_vcf_regions(
                "https://example/vcf.gz",
                ["r:100-100"],
                event_prefix="test",
                open_fn=factory,
            ),
        )
    assert factory.opens == MAX_REMOTE_REGION_ATTEMPTS

"""Shared remote-tabix machinery for streaming bgzipped+tabix VCFs over HTTP.

Extracted from :mod:`genome.annotate.loaders.gnomad` at sub-phase 5.6
(finding-012 #11: the second remote-tabix source — dbSNP — is the sanctioned
trigger to lift the generic htslib/HTTP-2 plumbing into a reusable module).
The extraction is behaviour-preserving for gnomAD: ``gnomad.py`` re-exports
every symbol it used to define here, and the structlog event names are
parameterised by ``event_prefix`` so gnomAD keeps emitting ``gnomad.*`` and
dbSNP emits ``dbsnp.*``.

Two remote sources (gnomAD v4.1, dbSNP build 157) share the same wire-level
reality: their public bgzipped+tabix VCFs are served over HTTPS with HTTP/2
multiplexing, and cyvcf2's bundled htslib opens one libcurl easy handle per
``VCF``. Firing many small tabix range requests in rapid succession on one
handle eventually trips ``CURLE_HTTP2`` (16) on a BGZF block read; htslib's
iterator can't recover (subsequent ``vcf(region)`` calls silently return zero
records and stderr fills with ``[E::hts_itr_next] ... Illegal seek`` lines
whose offsets are garbage memory). The only recovery is to close + reopen the
``VCF``. This module owns:

* :class:`_StderrTap` + :func:`_scan_for_htslib_errors` — fd-2 capture and the
  token scan that detects the silent-corruption state.
* :func:`iter_remote_vcf_regions` — the per-region
  open → iterate → detect → close+reopen → retry generator. It **yields each
  record** (re-yields after a reopen are expected; the *caller* dedups, exactly
  as gnomAD does).
* :func:`coalesce_positions` — merge sorted positions into tabix ranges.
* :func:`audited_head` — the audited-HEAD paper-trail helper.
* :func:`check_libcurl_available` — the pre-flight remote-open probe.
* :class:`RemoteTabixLibcurlMissingError` / :class:`RemoteTabixIterationError`.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, Self, cast

import structlog

from genome.privacy.external_client import ExternalCallError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from genome.privacy.external_client import ExternalClient

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Custom errors.
# ---------------------------------------------------------------------------


class RemoteTabixLibcurlMissingError(RuntimeError):
    """Raised when the cyvcf2-bundled htslib cannot reach a remote VCF URL.

    The pre-flight check (:func:`check_libcurl_available`) opens a
    known-tiny tabix range against the source URL. If the open fails the
    most likely cause is that the installed htslib was built without
    libcurl support, which means remote-tabix reads (the loader's whole
    filtering strategy) cannot work. The message points the operator at
    the rebuild instructions rather than swallowing the failure and
    falling back to a full download.

    gnomAD re-exports this as ``GnomadLibcurlMissingError`` (alias) for
    backwards compatibility.
    """


class RemoteTabixIterationError(RuntimeError):
    """Raised when a single tabix region exhausts its reopen retry budget.

    The loader detects htslib BGZF / libcurl transient errors by
    capturing C-level stderr writes and reopening the VCF on the next
    region. When the *same* region keeps tripping the corruption
    detector across many reopens, something more durable than a
    transient HTTP/2 framing blip is at play (e.g. the upstream URL was
    rotated, or the local network is degraded enough that no region
    completes). The chromosome is failed in that case so the operator
    sees a real error rather than a silent low-row-count run.

    gnomAD re-exports this as ``GnomadRemoteIterationError`` (alias).
    """


# ---------------------------------------------------------------------------
# htslib transient-error recovery.
# ---------------------------------------------------------------------------

_HTSLIB_ERROR_TOKENS: Final[tuple[bytes, ...]] = (
    b"easy_errno",
    b"bgzf_read_block",
    b"hts_itr_next",
)

MAX_REMOTE_REGION_ATTEMPTS: Final[int] = 5
"""Maximum VCF reopens per region before the chromosome is failed.

Each attempt closes the corrupted handle, reopens against the same
URL, and re-iterates the region. The caller's seen-keys dedup makes
record re-yields idempotent (same key → skipped on retry), so a
successful attempt produces no duplicates regardless of how many
partial yields the prior attempts emitted before tripping the detector.

5 is generous: in practice corruption recovers on the first reopen
because the transient libcurl HTTP/2 framing error is a connection-
level event, not a server-side condition. A region that fails 5
times in a row signals something more durable (network outage, URL
rotation), which is properly surfaced as a chromosome failure.

Note the exact open accounting the gnomAD recovery tests pin: an
always-corrupting region opens the URL exactly
:data:`MAX_REMOTE_REGION_ATTEMPTS` times (one initial open + four
reopens; the final attempt does not reopen because there is no attempt
left to use the handle).
"""


def _scan_for_htslib_errors(buf: bytes) -> bool:
    """Return True when ``buf`` contains one of the htslib error tokens.

    Module-level helper so tests can exercise the scan logic without
    standing up an fd-redirecting tap. The token list intentionally
    covers libcurl's ``easy_errno`` line (the original-cause signal),
    BGZF's ``bgzf_read_block`` line (htslib's framing-layer signal),
    and the iterator's ``hts_itr_next`` line (the post-corruption
    seek-failure signal). Any one is sufficient evidence that the VCF
    handle is in a state where subsequent region queries will silently
    return no records.
    """
    return any(token in buf for token in _HTSLIB_ERROR_TOKENS)


class _StderrTap:
    """Capture and forward htslib's C-level stderr writes.

    Used to detect cyvcf2/htslib BGZF + libcurl errors that don't
    surface as Python exceptions. On enter the tap replaces fd 2 with
    a non-blocking pipe; :meth:`check` drains the pipe, forwards the
    bytes through to the saved real-stderr fd (so structlog output
    and other warnings remain visible to the operator), and returns
    True when one of :data:`_HTSLIB_ERROR_TOKENS` appears in the
    drained bytes. On exit the tap restores the original fd 2 after
    a final drain.

    The pipe is sized to 1 MiB on Linux (``F_SETPIPE_SZ``) so a few
    seconds of unread output cannot back up htslib's writer thread.
    The drain loop is bounded by the pipe contents, not by a timer,
    so :meth:`check` returns promptly between regions.

    The tap is a thin context manager; constructing one outside a
    ``with`` block does nothing and consuming :meth:`check` outside
    the block silently returns ``False``. This makes it safe to keep
    a reference in test fixtures that mock the loader's iteration.
    """

    _PIPE_SIZE: Final[int] = 1024 * 1024
    _READ_CHUNK: Final[int] = 65536

    def __init__(self) -> None:
        self._original_fd: int | None = None
        self._pipe_read: int | None = None

    def __enter__(self) -> Self:
        sys.stderr.flush()
        self._original_fd = os.dup(2)
        read_fd, write_fd = os.pipe()
        fcntl.fcntl(read_fd, fcntl.F_SETFL, os.O_NONBLOCK)
        # Best-effort enlargement of the pipe buffer so a slow consumer
        # doesn't block htslib. F_SETPIPE_SZ is Linux-only; absence is
        # not fatal (the default 64 KiB still works, just with more
        # frequent drain calls).
        f_setpipe_sz = getattr(fcntl, "F_SETPIPE_SZ", None)
        if f_setpipe_sz is not None:
            with contextlib.suppress(OSError):
                fcntl.fcntl(read_fd, f_setpipe_sz, self._PIPE_SIZE)
        os.dup2(write_fd, 2)
        os.close(write_fd)
        self._pipe_read = read_fd
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._original_fd is None or self._pipe_read is None:
            return
        # Final drain so anything written between the last check() and
        # close reaches the operator's stderr.
        self._drain_and_forward()
        sys.stderr.flush()
        os.dup2(self._original_fd, 2)
        try:
            os.close(self._pipe_read)
        finally:
            os.close(self._original_fd)
            self._pipe_read = None
            self._original_fd = None

    def check(self) -> bool:
        """Return True when an htslib error token appeared since the last call.

        Drains the captured stderr buffer (forwarding every byte to
        the operator's real stderr) and scans the drained bytes for
        any of :data:`_HTSLIB_ERROR_TOKENS`. Subsequent calls only
        see new output; the scan is monotonic per-call, not
        cumulative.
        """
        return _scan_for_htslib_errors(self._drain_and_forward())

    def _drain_and_forward(self) -> bytes:
        if self._pipe_read is None or self._original_fd is None:
            return b""
        chunks: list[bytes] = []
        while True:
            try:
                data = os.read(self._pipe_read, self._READ_CHUNK)
            except BlockingIOError:
                break
            if not data:
                break
            chunks.append(data)
        buf = b"".join(chunks)
        if buf:
            os.write(self._original_fd, buf)
        return buf


# ---------------------------------------------------------------------------
# Position coalescing.
# ---------------------------------------------------------------------------


def coalesce_positions(
    positions: list[int],
    max_gap: int,
) -> list[tuple[int, int]]:
    """Merge sorted positions into inclusive ranges when within ``max_gap``.

    Adjacent positions whose gap (``next - current``) is less than or
    equal to ``max_gap`` merge into one ``(start, end)`` tuple.
    Positions farther apart than ``max_gap`` produce separate ranges.

    Returns ``[]`` for an empty input. A single position ``p`` returns
    ``[(p, p)]``.

    Pure function — exercised independently by unit tests without a
    DuckDB connection or network. Tabix accepts ``start:end`` ranges
    inclusively on both ends, so the merged range covers every
    original position; records inside the gaps (sites we don't care
    about) are still seen by the iterator and filtered by the caller.
    """
    if not positions:
        return []
    out: list[tuple[int, int]] = []
    start = positions[0]
    end = positions[0]
    for pos in positions[1:]:
        if pos - end <= max_gap:
            end = pos
        else:
            out.append((start, end))
            start = pos
            end = pos
    out.append((start, end))
    return out


# ---------------------------------------------------------------------------
# Audited HEAD paper trail.
# ---------------------------------------------------------------------------


def audited_head(  # noqa: PLR0913 — audited-HEAD config is irreducible
    client: ExternalClient,
    url: str,
    *,
    resource_id: str,
    event_prefix: str,
    resource_type: str = "annotation_source",
    log: structlog.stdlib.BoundLogger | None = None,
) -> None:
    """Issue an audited HEAD against ``url`` for the audit-log paper trail.

    The remote-tabix VCF open is the actual data fetch, but it does
    not flow through the audited HTTP client (cyvcf2 talks to htslib's
    libcurl directly). To keep the audit-log invariant — every external
    call is logged — the loader issues an audited HEAD against the
    same URL. The HEAD's response headers carry no genome content; the
    audit row records that the URL was opened.

    A non-200 HEAD does not abort the load — some hosts occasionally
    reject HEAD against the canonical URL even when GET works fine. The
    helper logs ``{event_prefix}.audited_head_non_fatal`` and proceeds;
    the real error path is the cyvcf2 open inside
    :func:`iter_remote_vcf_regions`.
    """
    out_log = log if log is not None else logger
    try:
        client.request(
            "HEAD",
            url,
            resource_type=resource_type,
            resource_id=resource_id,
        )
    except ExternalCallError as exc:
        out_log.info(f"{event_prefix}.audited_head_non_fatal", url=url, error=str(exc))


# ---------------------------------------------------------------------------
# Pre-flight.
# ---------------------------------------------------------------------------


class _RemoteVCF(Protocol):
    """Structural type of the handle returned by ``open_fn`` (cyvcf2.VCF)."""

    def __call__(self, region: str) -> Iterable[object]: ...

    def close(self) -> None: ...


def _resolve_open_fn(open_fn: Callable[[str], _RemoteVCF] | None) -> Callable[[str], _RemoteVCF]:
    """Return ``open_fn`` or resolve ``cyvcf2.VCF`` at call time.

    Resolving at call time (rather than binding ``cyvcf2.VCF`` as a
    default argument at import time) is load-bearing: the loaders' tests
    monkeypatch ``cyvcf2.VCF``, and a default bound at import time would
    capture the original and miss the patch.
    """
    if open_fn is not None:
        return open_fn
    from cyvcf2 import VCF  # noqa: PLC0415

    return cast("Callable[[str], _RemoteVCF]", VCF)


def check_libcurl_available(
    url: str,
    probe_region: str,
    *,
    open_fn: Callable[[str], _RemoteVCF] | None = None,
) -> None:
    """Confirm cyvcf2 can open ``url`` and iterate ``probe_region``.

    Opens a known-tiny tabix range against the source URL. The query
    fetches at most one record from a tight position window (negligible
    server-side cost). A success means htslib was built with libcurl
    support and the remote host is reachable.

    Raises :class:`RemoteTabixLibcurlMissingError` with an actionable
    message when the open or initial fetch fails for any reason. The
    error preserves the underlying exception via ``__cause__`` so a log
    reader can see the root cause (typically a libcurl-missing htslib
    build).
    """
    resolved = _resolve_open_fn(open_fn)
    try:
        vcf = resolved(url)
        try:
            # Pull the iterator from a tight tabix range; the open path
            # is what exercises libcurl, but materialising the iterator
            # makes the failure mode unambiguous if the bucket changes
            # shape.
            iter(vcf(probe_region))
        finally:
            vcf.close()
    except Exception as exc:
        msg = (
            f"Remote VCF open failed for {url!r}. "
            "htslib must be built with libcurl support; this environment "
            "doesn't have it (or the remote host is unreachable). "
            "Rebuild htslib (and cyvcf2 against it) with libcurl enabled."
        )
        raise RemoteTabixLibcurlMissingError(msg) from exc


# ---------------------------------------------------------------------------
# Per-region iteration with htslib reopen/retry.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RemoteReadStats:
    """Mutable out-param accumulator for :func:`iter_remote_vcf_regions` reopens.

    An out-param (rather than a return value) because
    :func:`iter_remote_vcf_regions` is a *generator*: its ``for record in …``
    consumers drive it by iteration, so a returned scalar count would only
    reach them via ``StopIteration.value``, which they discard. Mutating a
    caller-owned instance is how the reopen count gets back out.

    NON-frozen by design — ``.reopens`` is incremented (``+= 1``) in-place at
    each htslib close+reopen. One instance reused across successive calls sums
    their reopens (a chromosome's exomes+genomes streams, or a whole sequential
    run), so the caller aggregates a run-total htslib-reopen drift sentinel
    (finding-012 #12) without re-plumbing the transient per-reopen events.
    ``reopen_stats`` defaults to ``None`` on the seam, so an existing caller
    that passes no accumulator is byte-identical.
    """

    reopens: int = 0


def iter_remote_vcf_regions(  # noqa: PLR0913 — irreducible retry loop + telemetry config
    url: str,
    regions: list[str],
    *,
    event_prefix: str,
    log_context: dict[str, object] | None = None,
    open_fn: Callable[[str], _RemoteVCF] | None = None,
    log: structlog.stdlib.BoundLogger | None = None,
    reopen_stats: RemoteReadStats | None = None,
) -> Iterator[object]:
    """Open ``url`` and yield every record across ``regions``, recovering reopens.

    ``regions`` are pre-formatted tabix region strings (e.g.
    ``"chr22:1000-2000"`` for gnomAD's ``chr``-prefixed labels or
    ``"NC_000022.11:1000-2000"`` for dbSNP's RefSeq accessions). The
    caller formats them so this helper stays agnostic to chromosome
    naming.

    Per region the generator runs an
    open → iterate → detect → close+reopen → retry loop bounded by
    :data:`MAX_REMOTE_REGION_ATTEMPTS`:

    1. Iterate the region, **yielding each record** to the caller. The
       caller is responsible for projection, the precise position
       filter, and dedup — re-yields after a reopen are expected and
       must be idempotent on the caller's side (gnomAD's per-chrom
       ``seen_keys`` set; dbSNP's per-run ``rsid`` set).
    2. After the inner iterator exhausts, :meth:`_StderrTap.check`
       reports whether libcurl tripped a transient HTTP/2 framing error
       during the read (the failure mode that silently corrupts
       htslib's iterator state and turns every subsequent
       ``vcf(region)`` call into an empty yield).
    3. When detected, close + reopen the VCF against the same URL and
       re-iterate the region. The final attempt does not reopen (no
       attempt left to use the handle).
    4. If the region never recovers, raise
       :class:`RemoteTabixIterationError` (message contains ``region``).

    Emits ``{event_prefix}.remote_open`` at the initial open,
    ``{event_prefix}.chrom.htslib_recover`` on each reopen, and
    ``{event_prefix}.chrom.htslib_recover_summary`` once at the end if
    any reopen occurred. ``log_context`` fields (e.g. ``chrom`` /
    ``data_type``) are merged into every event so the names + field sets
    match what gnomAD emitted before the extraction.

    When ``reopen_stats`` is supplied, each reopen also increments
    ``reopen_stats.reopens`` in place (finding-012 #12) so the caller can
    aggregate a run-total reopen count that outlives these transient
    events. Passing one accumulator across successive calls sums their
    reopens; ``None`` (the default) leaves the call byte-identical for
    callers that don't want the sentinel.
    """
    ctx = log_context or {}
    out_log = log if log is not None else logger
    resolved = _resolve_open_fn(open_fn)

    out_log.info(f"{event_prefix}.remote_open", url=url, **ctx)
    reopens = 0
    with _StderrTap() as detector:
        # Discard any stderr written during VCF construction; tests mock
        # VCF and write nothing, the real cyvcf2 may emit benign warnings
        # (e.g. about index recency) that should not look like a
        # corruption event to the first region's post-iteration check.
        vcf = resolved(url)
        detector.check()
        try:
            for region in regions:
                recovered = False
                for attempt in range(MAX_REMOTE_REGION_ATTEMPTS):
                    yield from vcf(region)
                    if not detector.check():
                        recovered = True
                        break
                    # Transient htslib error during read. Close the
                    # corrupted handle and reopen against the same URL
                    # only if there is another attempt left — the final
                    # attempt's failure leads straight to raise, no point
                    # spending a reopen on a handle that will never be
                    # used. The caller's dedup makes record re-yields on
                    # the next attempt idempotent.
                    if attempt + 1 == MAX_REMOTE_REGION_ATTEMPTS:
                        break
                    # Corrupt-handle close may raise; the open call that
                    # follows discards the handle anyway, so a close
                    # failure here is not actionable.
                    with contextlib.suppress(Exception):
                        vcf.close()
                    vcf = resolved(url)
                    # Drain stderr produced during reopen so the next
                    # region's check starts clean.
                    detector.check()
                    reopens += 1
                    # Accumulate the run-total reopen sentinel out-param
                    # (finding-012 #12). Incremented here — at the reopen
                    # site, before the possible max-attempts raise below —
                    # so a failed chromosome still surfaces its partial
                    # reopen count into the caller's run-level accumulator.
                    if reopen_stats is not None:
                        reopen_stats.reopens += 1
                    out_log.warning(
                        f"{event_prefix}.chrom.htslib_recover",
                        region=region,
                        attempt=attempt + 1,
                        reopens=reopens,
                        **ctx,
                    )
                if not recovered:
                    msg = (
                        f"remote region {region!r} failed after "
                        f"{MAX_REMOTE_REGION_ATTEMPTS} attempts — "
                        "persistent htslib transient errors"
                    )
                    raise RemoteTabixIterationError(msg)
        finally:
            # Final close of whichever VCF handle is current. The last
            # handle may be corrupt; suppress close errors so the outer
            # summary log still runs.
            with contextlib.suppress(Exception):
                vcf.close()
    if reopens:
        out_log.info(
            f"{event_prefix}.chrom.htslib_recover_summary",
            reopens=reopens,
            **ctx,
        )


__all__ = [
    "MAX_REMOTE_REGION_ATTEMPTS",
    "RemoteReadStats",
    "RemoteTabixIterationError",
    "RemoteTabixLibcurlMissingError",
    "audited_head",
    "check_libcurl_available",
    "coalesce_positions",
    "iter_remote_vcf_regions",
]

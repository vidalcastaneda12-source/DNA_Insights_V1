"""Path resolver + audited-download wrapper for annotation sources.

Mirrors the Phase 4 ``reference_panel`` patterns:

* :func:`default_annotations_root` — paths-only. Touches no filesystem.
* :func:`source_download_dir` — paths-only resolver that *also* creates
  the source-specific directory tree with ``0700`` permissions when
  called. Callers that only need the path (e.g. ``genome annotate
  status``) compute it via :func:`default_annotations_root` instead so
  no cache directory is created as a side effect.
* :func:`download_to_cache` — idempotent skip-if-exists download via the
  audited :class:`genome.privacy.external_client.ExternalClient`.

The cache lives under ``~/.cache/genome/annotations/`` by default,
sibling to ``~/.cache/genome/imputation/``. The location is outside the
project ``data/`` directory so a ``rm -rf data/`` schema rebuild does
not force the user to re-download large reference archives.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
import structlog

from genome.config import get_settings
from genome.privacy.external_client import _DEFAULT_TIMEOUT_S, ExternalClient

logger = structlog.get_logger(__name__)

_OWNER_RW_ONLY: Final[int] = stat.S_IRUSR | stat.S_IWUSR
_OWNER_RWX_ONLY: Final[int] = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR

_RESOURCE_TYPE: Final[str] = "annotation_source"
"""Value used for ``audit_log.resource_type`` on every annotation download."""

_ENDPOINT_LABEL_PREFIX: Final[str] = "annotations_"
"""Prefix for the audited-client endpoint label. The source_db is
appended so audit-log queries can group all clinvar downloads, all
gwas downloads, etc."""


def default_annotations_root() -> Path:
    """Resolve the annotations cache root directory.

    Reads ``settings.annotations_download_root`` first; falls back to
    ``~/.cache/genome/annotations/`` when unset. Mirrors
    :func:`genome.imputation.reference_panel.default_panel_root`. Does
    not touch the filesystem.
    """
    settings = get_settings()
    if settings.annotations_download_root is not None:
        return Path(settings.annotations_download_root)
    return Path.home() / ".cache" / "genome" / "annotations"


def source_download_dir(source_db: str) -> Path:
    """Return ``<root>/<source_db>/`` and create it with ``0700`` perms.

    The cache directory tree is created on demand here so neither
    :func:`default_annotations_root` nor ``genome annotate status``
    materializes ``~/.cache/genome/annotations/`` as a side effect.
    """
    root = default_annotations_root()
    target = root / source_db
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(_OWNER_RWX_ONLY)
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(_OWNER_RWX_ONLY)
    return target


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Outcome of a single :func:`download_to_cache` call.

    ``from_cache`` distinguishes a skip-if-exists cache hit (the bytes
    were already on disk, no external call was made) from a fresh
    download. ``cached_version_label`` carries the label read back from
    the ``<dest>.version`` sidecar on a cache hit (``None`` when the
    caller wrote no sidecar on the original download or the sidecar is
    unreadable). Together they let a loader bind its version label to
    the *bytes actually loaded* rather than the label it resolved from
    live upstream — the fix for the ``rm -rf data/`` rebuild-relabel
    defect (finding-043 / finding-022 #4). Both fields are trailing and
    defaulted so every existing keyword-construction call site is
    unaffected.
    """

    path: Path
    sha256: str
    size_bytes: int
    from_cache: bool = False
    cached_version_label: str | None = None


def _sidecar_path(dest: Path) -> Path:
    """Return the ``<dest>.version`` sidecar path.

    Uses ``dest.with_name(dest.name + '.version')`` (a string append)
    rather than ``dest.with_suffix('.version')`` so a compound extension
    like ``variant_summary.txt.gz`` yields
    ``variant_summary.txt.gz.version`` instead of clobbering the ``.gz``
    into ``variant_summary.txt.version``.
    """
    return dest.with_name(dest.name + ".version")


def _read_version_sidecar(dest: Path) -> str | None:
    """Read the version label from ``<dest>.version``; ``None`` when absent.

    Best-effort: a missing or unreadable sidecar (or an empty one) maps
    to ``None`` so the caller reads it as "the cached bytes carry no
    known version label" and can fall back to its live-resolved label.
    """
    try:
        label = _sidecar_path(dest).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return label or None


def _write_version_sidecar(dest: Path, label: str) -> None:
    """Write ``label`` to ``<dest>.version`` at ``0600``, best-effort.

    A failure to persist the sidecar must never abort the download: the
    bytes are already on disk and are the load's payload. We log the
    failure and continue so a hostile filesystem state (e.g. the sidecar
    path already taken by a directory) degrades to "no version label
    cached" rather than a failed refresh.
    """
    sidecar = _sidecar_path(dest)
    try:
        sidecar.write_text(label, encoding="utf-8")
        sidecar.chmod(_OWNER_RW_ONLY)
    except OSError:
        logger.warning(
            "annotate.download.version_sidecar_write_failed",
            sidecar=str(sidecar),
            exc_info=True,
        )


def download_to_cache(  # noqa: PLR0913 — trailing version_label kwarg (finding-043 sidecar bind)
    source_db: str,
    url: str,
    filename: str,
    *,
    resource_id: str,
    force: bool = False,
    version_label: str | None = None,
) -> DownloadResult:
    """Download ``url`` to ``<annotations_root>/<source_db>/<filename>``.

    Idempotent: when ``dest`` already exists and ``force`` is ``False``,
    re-hashes the local file and returns a :class:`DownloadResult` with
    the cached file's metadata — no external call is made. This is the
    equivalent of ``reference_panel._install_panel_vcf``'s
    skip-if-exists behaviour, and is what lets the on-disk cache survive
    a schema-change ``rm -rf data/`` rebuild without forcing
    re-downloads.

    On a fresh download: streams the body via
    :meth:`ExternalClient.download` (so the response never lives in
    memory), chmods the saved file to ``0600``, and returns the
    SHA-256 the client computed plus the byte size from ``stat()``.

    ``version_label`` binds a version label to the *bytes on disk*: on a
    fresh download (and only then) it is persisted to a ``<dest>.version``
    sidecar (``0600``, best-effort — a write failure is logged and never
    aborts the download). A later cache hit reads that sidecar back into
    :attr:`DownloadResult.cached_version_label` so a loader can bind its
    version label to the cached bytes rather than a drifted live-resolved
    label (finding-043 / finding-022 #4). When ``version_label`` is
    ``None`` no sidecar is written and cache-hit callers see
    ``cached_version_label=None``.

    The endpoint label is ``f"annotations_{source_db}"`` — embedding the
    source_db lets audit-log queries group every download for one
    source. ``resource_type`` on the audit row is the literal
    ``"annotation_source"``; ``resource_id`` is the caller-supplied
    label (e.g. ``'clinvar_full'``, ``'pharmgkb_clinical_ann'``).

    Public dataset distribution endpoints (PharmGKB, ClinVar, GWAS
    Catalog, dbSNP, gnomAD) routinely 303-redirect to signed S3 / CDN
    URLs. We inject an ``httpx.Client(follow_redirects=True)`` here so
    the scaffold abstracts that detail away from per-source loaders:
    every loader gets to write the canonical upstream URL into its
    constants and rely on the scaffold to land the actual file on
    disk. :class:`ExternalClient` itself stays redirect-agnostic
    because it serves both annotation downloads (where redirects are
    expected) and other workflows — e.g. Phase 4 reference-panel
    downloads — where the upstream URL is final and silently
    following a redirect would mask a misconfiguration. The injected
    client's timeout mirrors :data:`_DEFAULT_TIMEOUT_S` so behaviour
    matches the un-injected path.
    """
    dest_dir = source_download_dir(source_db)
    dest = dest_dir / filename

    if dest.exists() and not force:
        size = dest.stat().st_size
        digest = _hash_file(dest)
        cached_label = _read_version_sidecar(dest)
        logger.debug(
            "annotate.download.skip_existing",
            source_db=source_db,
            filename=filename,
            sha256=digest[:12],
            size_bytes=size,
            cached_version_label=cached_label,
        )
        return DownloadResult(
            path=dest,
            sha256=digest,
            size_bytes=size,
            from_cache=True,
            cached_version_label=cached_label,
        )

    endpoint_label = f"{_ENDPOINT_LABEL_PREFIX}{source_db}"
    log = logger.bind(source_db=source_db, filename=filename, url=url)
    log.info("annotate.download.start")
    with (
        httpx.Client(
            follow_redirects=True,
            timeout=_DEFAULT_TIMEOUT_S,
        ) as http_client,
        ExternalClient(endpoint_label, client=http_client) as client,
    ):
        digest = client.download(
            url,
            str(dest),
            resource_type=_RESOURCE_TYPE,
            resource_id=resource_id,
        )
    dest.chmod(_OWNER_RW_ONLY)
    size = dest.stat().st_size
    if version_label is not None:
        _write_version_sidecar(dest, version_label)
    log.info("annotate.download.complete", sha256=digest[:12], size_bytes=size)
    return DownloadResult(path=dest, sha256=digest, size_bytes=size, from_cache=False)


def _hash_file(path: Path) -> str:
    """SHA-256 hex of a file, streamed so memory stays bounded."""
    import hashlib  # noqa: PLC0415 — local import to keep module-level surface small

    h = hashlib.sha256()
    chunk_size = 1 << 20
    with open(path, "rb") as f:  # noqa: PTH123 — explicit open keeps the type narrow
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


__all__ = [
    "DownloadResult",
    "default_annotations_root",
    "download_to_cache",
    "source_download_dir",
]

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
    """Outcome of a single :func:`download_to_cache` call."""

    path: Path
    sha256: str
    size_bytes: int


def download_to_cache(
    source_db: str,
    url: str,
    filename: str,
    *,
    resource_id: str,
    force: bool = False,
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
        logger.debug(
            "annotate.download.skip_existing",
            source_db=source_db,
            filename=filename,
            sha256=digest[:12],
            size_bytes=size,
        )
        return DownloadResult(path=dest, sha256=digest, size_bytes=size)

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
    log.info("annotate.download.complete", sha256=digest[:12], size_bytes=size)
    return DownloadResult(path=dest, sha256=digest, size_bytes=size)


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

"""Phase 5 — reference annotation loader scaffold.

Public entry points for sub-phase 5.0:

* :data:`KNOWN_SOURCE_DBS` — canonical set of ``source_db`` labels.
* :class:`SourceVersion`, :func:`upsert_source_version`,
  :func:`get_current_version` — the ``annotation_source_versions`` CRUD.
* :func:`default_annotations_root`, :func:`source_download_dir`,
  :func:`download_to_cache`, :class:`DownloadResult` — the on-disk
  cache layout under ``~/.cache/genome/annotations/`` and the audited
  download wrapper.
* :func:`deactivate_prior_versions` — generic supersession helper for
  the evolving-source tables (ClinVar / GWAS Catalog / PharmGKB / CPIC
  / PGS Catalog).
* :class:`RefreshResult`, :data:`RefreshFn`, :func:`register_loader`,
  :func:`get_loader`, :func:`known_loaders` — the per-source loader
  registry. Empty in 5.0; sub-phase 5.1+ each register one entry.
* :data:`annotate_app` — Typer subcommand surface
  (``genome annotate status``, ``genome annotate refresh --source ...``).
"""

from __future__ import annotations

from genome.annotate.cli import annotate_app
from genome.annotate.downloads import (
    DownloadResult,
    default_annotations_root,
    download_to_cache,
    source_download_dir,
)
from genome.annotate.registry import (
    RefreshFn,
    RefreshResult,
    get_loader,
    known_loaders,
    register_loader,
)
from genome.annotate.source_versions import (
    KNOWN_SOURCE_DBS,
    SourceVersion,
    get_current_version,
    upsert_source_version,
)
from genome.annotate.supersession import deactivate_prior_versions

__all__ = [
    "KNOWN_SOURCE_DBS",
    "DownloadResult",
    "RefreshFn",
    "RefreshResult",
    "SourceVersion",
    "annotate_app",
    "deactivate_prior_versions",
    "default_annotations_root",
    "download_to_cache",
    "get_current_version",
    "get_loader",
    "known_loaders",
    "register_loader",
    "source_download_dir",
    "upsert_source_version",
]

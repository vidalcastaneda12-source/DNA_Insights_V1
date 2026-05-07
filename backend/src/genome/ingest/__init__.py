"""Phase 2 — raw export ingestion (parse, normalize, lift-over, persist).

The public entry point for the rest of the app is :func:`ingest_file`. The CLI
calls into it; tests exercise the same surface.
"""

from __future__ import annotations

from genome.ingest.models import (
    IngestResult,
    NormalizedCall,
    RawCall,
    RawFileMeta,
    Source,
)
from genome.ingest.pipeline import PIPELINE_VERSION, ingest_file

__all__ = [
    "PIPELINE_VERSION",
    "IngestResult",
    "NormalizedCall",
    "RawCall",
    "RawFileMeta",
    "Source",
    "ingest_file",
]

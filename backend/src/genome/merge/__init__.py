"""Phase 3 — merge and discrepancy detection.

The public entry point is :func:`merge_all`. The CLI calls it; tests exercise
the same surface. The Python helpers (``resolve``, ``strand`` utilities) are
exported for the test suite and for any future caller that needs to reason
about a single pair without going through the full DB-roundtrip.
"""

from __future__ import annotations

from genome.merge.consensus import resolve
from genome.merge.models import (
    MERGE_VERSION,
    CallView,
    ConsensusRow,
    DiscrepancyRow,
    MergeResult,
    VariantPair,
)
from genome.merge.pipeline import merge_all
from genome.merge.strand import (
    complement,
    complement_pair,
    is_palindromic_site,
    sorted_pair,
)

__all__ = [
    "MERGE_VERSION",
    "CallView",
    "ConsensusRow",
    "DiscrepancyRow",
    "MergeResult",
    "VariantPair",
    "complement",
    "complement_pair",
    "is_palindromic_site",
    "merge_all",
    "resolve",
    "sorted_pair",
]

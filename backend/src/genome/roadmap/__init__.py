"""Source-of-truth gate tooling — the ``genome roadmap`` surface (finding-042 / DEC-0125).

The fail-closed gate that keeps ``ROADMAP.md`` the single source of truth for scope: every
top-level checklist item carries a unique ``RM-<7 hex>`` id, and every ``RM-`` id cited in
``docs/findings/*.md`` / ``MEMORY.md`` / ``CHANGELOG.md`` resolves to one defined in ROADMAP.

**This package imports no** :mod:`genome.db`. ``python -c "import genome.roadmap"`` and
``genome roadmap check`` must run on a fresh checkout with no DuckDB / SQLCipher built (mirrors
:mod:`genome.docs` and :mod:`genome.workflows`). Do not add a database import here or in any
module it pulls in.
"""

from __future__ import annotations

from genome.roadmap.cli import roadmap_app
from genome.roadmap.model import (
    DANGLING_REF,
    DUPLICATE_ID,
    MISSING_ID,
    ROADMAP_FILE_MISSING,
    GateReport,
    GateViolation,
)
from genome.roadmap.validator import check

__all__ = [
    "DANGLING_REF",
    "DUPLICATE_ID",
    "MISSING_ID",
    "ROADMAP_FILE_MISSING",
    "GateReport",
    "GateViolation",
    "check",
    "roadmap_app",
]

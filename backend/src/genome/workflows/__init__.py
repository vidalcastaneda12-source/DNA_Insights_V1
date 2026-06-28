"""Engine-primary workflow tooling — the ``genome workflows`` surface (C2+D Phase 2 / finding-034).

The fail-closed **reversal-gate** over the three self-contained per-scope-team dynamic workflows
(``.claude/workflows/{plan-phase,implement-review,close}.js``): seam-drift (the duplicated
``agent()``/retry seam stays logically identical under GT-1) + schema-validity (every ``SCHEMAS``
entry declares ``type: 'object'``, locking the PR 1 400-fix).

**This package imports no** :mod:`genome.db`. ``python -c "import genome.workflows"`` and
``genome workflows check`` must run on a fresh checkout with no DuckDB / SQLCipher built. Do not add
a database import here or in any module it pulls in.
"""

from __future__ import annotations

from genome.workflows.cli import workflows_app
from genome.workflows.model import (
    SCHEMA_MISSING_TYPE,
    SCHEMAS_NOT_LOCATED,
    SEAM_DRIFT,
    SEAM_NOT_LOCATED,
    WORKFLOW_FILE_MISSING,
    GateReport,
    GateViolation,
)
from genome.workflows.schemas import extract_schemas_block, schema_entry_keys_without_type
from genome.workflows.seam import extract_seam, normalize_seam
from genome.workflows.validator import check

__all__ = [
    "SCHEMAS_NOT_LOCATED",
    "SCHEMA_MISSING_TYPE",
    "SEAM_DRIFT",
    "SEAM_NOT_LOCATED",
    "WORKFLOW_FILE_MISSING",
    "GateReport",
    "GateViolation",
    "check",
    "extract_schemas_block",
    "extract_seam",
    "normalize_seam",
    "schema_entry_keys_without_type",
    "workflows_app",
]

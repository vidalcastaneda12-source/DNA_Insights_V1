"""Data model for the ``genome workflows check`` reversal-gate (C2+D Phase 2 / finding-034).

The fail-closed gate runs over the three self-contained per-scope-team dynamic workflows
(``.claude/workflows/{plan-phase,implement-review,close}.js``). This module imports **no**
:mod:`genome.db` — the whole package is pure filesystem text inspection, so
``genome workflows check`` runs on a fresh checkout with no DuckDB / SQLCipher built (mirrors
:mod:`genome.docs`).
"""

from __future__ import annotations

from dataclasses import dataclass

# Stable machine codes, printed by the CLI. An undecidable signal (file missing, seam or
# SCHEMAS block not locatable) is a FAILURE code, never a silent pass — the gate is fail-closed.
SEAM_DRIFT = "SEAM_DRIFT"
SEAM_NOT_LOCATED = "SEAM_NOT_LOCATED"
SCHEMA_MISSING_TYPE = "SCHEMA_MISSING_TYPE"
SCHEMAS_NOT_LOCATED = "SCHEMAS_NOT_LOCATED"
WORKFLOW_FILE_MISSING = "WORKFLOW_FILE_MISSING"


@dataclass(frozen=True, slots=True)
class GateViolation:
    """A single hard-fail emitted by :func:`genome.workflows.validator.check`."""

    code: str
    """One of the module-level code constants (e.g. :data:`SEAM_DRIFT`)."""
    location: str
    """Where it lives — a ``<stem>.js`` file, optionally ``:<entry-key>``."""
    message: str
    """Human-readable explanation printed by ``genome workflows check``."""


@dataclass(frozen=True, slots=True)
class GateReport:
    """The outcome of a ``genome workflows check`` run."""

    violations: tuple[GateViolation, ...]

    @property
    def ok(self) -> bool:
        """True iff there are no violations (drives the CLI exit code)."""
        return not self.violations

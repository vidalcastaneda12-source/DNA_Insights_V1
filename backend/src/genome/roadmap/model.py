"""Data model for the ``genome roadmap check`` source-of-truth gate (finding-042 / DEC-0125).

The fail-closed gate runs over ``ROADMAP.md`` + the docs that reference its ``RM-`` ids
(``docs/findings/*.md``, ``MEMORY.md``, ``CHANGELOG.md``). This module imports **no**
:mod:`genome.db` — the whole package is pure filesystem text inspection, so
``genome roadmap check`` runs on a fresh checkout with no DuckDB / SQLCipher built (mirrors
:mod:`genome.docs` and :mod:`genome.workflows`).
"""

from __future__ import annotations

from dataclasses import dataclass

# Stable machine codes, printed by the CLI. An undecidable signal (ROADMAP.md missing) is a
# FAILURE code, never a silent pass — the gate is fail-closed.
MISSING_ID = "MISSING_ID"
DUPLICATE_ID = "DUPLICATE_ID"
DANGLING_REF = "DANGLING_REF"
ROADMAP_FILE_MISSING = "ROADMAP_FILE_MISSING"


@dataclass(frozen=True, slots=True)
class GateViolation:
    """A single hard-fail emitted by :func:`genome.roadmap.validator.check`."""

    code: str
    """One of the module-level code constants (e.g. :data:`MISSING_ID`)."""
    location: str
    """Where it lives — ``ROADMAP.md:<line>`` or a referencing ``<file>``."""
    message: str
    """Human-readable explanation printed by ``genome roadmap check``."""


@dataclass(frozen=True, slots=True)
class GateReport:
    """The outcome of a ``genome roadmap check`` run."""

    violations: tuple[GateViolation, ...]

    @property
    def ok(self) -> bool:
        """True iff there are no violations (drives the CLI exit code)."""
        return not self.violations

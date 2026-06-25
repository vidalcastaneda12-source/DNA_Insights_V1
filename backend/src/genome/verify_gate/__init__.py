"""Agentic verify-and-merge gate — the ``genome verify-gate`` surface (``finding-037``).

A fail-closed, three-valued, unit-tested core that carries every decidable verification
check off the bash skill and into Python: a closed :class:`Verdict` (``GREEN`` / ``BLOCKED``
/ ``UNKNOWN``), a :class:`StepStatus` exit-code parser under it, frozen evidence records
whose every flag defaults to its non-affirmative value, and a reduction that turns any
non-affirmative input into ``BLOCKED`` / ``UNKNOWN`` rather than a false ``GREEN``. The
``verify-and-merge`` skill is faithful plumbing whose only gate is "``verify-gate verdict``
exited non-zero → stop".

**This package imports no** :mod:`genome.db`. ``python -c "import genome.verify_gate"`` must
run on a fresh checkout with no DuckDB / SQLCipher built (plan §4.1). Do not add a database
import here or in any module it pulls in — the two-row merge audit
(:func:`genome.privacy.external_client.write_merge_audit`) deliberately lives in the privacy
package, not here, so the core never reaches a DB.
"""

from __future__ import annotations

from genome.verify_gate.cli import verify_gate_app
from genome.verify_gate.formatter import NO_ANCHORS_SENTINEL, format_evidence
from genome.verify_gate.model import (
    CHANGE_CLASS_VOCAB,
    AnchorCheck,
    ChangeClass,
    CheckSet,
    EvidencePackage,
    IntegrityFlags,
    StepStatus,
    Verdict,
    assemble_check_set,
    parse_step,
)
from genome.verify_gate.verdict import reduce_verdict

__all__ = [
    "CHANGE_CLASS_VOCAB",
    "NO_ANCHORS_SENTINEL",
    "AnchorCheck",
    "ChangeClass",
    "CheckSet",
    "EvidencePackage",
    "IntegrityFlags",
    "StepStatus",
    "Verdict",
    "assemble_check_set",
    "format_evidence",
    "parse_step",
    "reduce_verdict",
    "verify_gate_app",
]

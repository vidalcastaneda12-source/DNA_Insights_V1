"""The fail-closed verdict reduction for the agentic verify-and-merge gate (plan §4.3).

Reduces an :class:`~genome.verify_gate.model.EvidencePackage` to a single three-valued
:class:`~genome.verify_gate.model.Verdict`. This is where every decidable check is graded,
so the bash skill's only gate can be "core exited non-zero → stop". **No** :mod:`genome.db`
import — the reduction is pure data → verdict.

Reduction rule (``UNKNOWN`` dominates ``BLOCKED`` dominates ``GREEN``):

* :attr:`~genome.verify_gate.model.Verdict.GREEN` **iff** every step is ``PASS`` ∧ every
  non-deferred anchor matches (``expected`` and ``actual`` both present and equal) ∧
  ``changelog_present`` ∧ ``docs_check_clean`` ∧ ``¬weakened_or_removed_test`` ∧
  ``¬gate_fill_survivor`` ∧ the test count is decided and non-decreasing ∧
  ``¬rebuild_pending``. An empty anchor set with everything else affirmative is GREEN (the
  N/A path).
* Any decided failure (step ``FAIL``, anchor mismatch, weakened test, surviving gate-fill,
  missing CHANGELOG, dirty docs check, negative test delta) → ``BLOCKED``.
* Any undecidable signal (step ``UNKNOWN``, non-deferred anchor with a ``None`` side, a
  ``None`` test count, ``rebuild_pending``) → ``UNKNOWN``.
* When both an ``UNKNOWN`` and a ``BLOCKED`` condition fire, ``UNKNOWN`` wins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genome.verify_gate.model import StepStatus, Verdict

if TYPE_CHECKING:
    from genome.verify_gate.model import AnchorCheck, EvidencePackage, IntegrityFlags


def _anchor_is_unknown(anchor: AnchorCheck) -> bool:
    """A non-deferred anchor missing either side is undecidable (DB absent / not captured)."""
    if anchor.deferred:
        return False
    return anchor.expected is None or anchor.actual is None


def _anchor_is_blocked(anchor: AnchorCheck) -> bool:
    """A non-deferred anchor with both sides present but unequal is a decided mismatch."""
    if anchor.deferred:
        return False
    if anchor.expected is None or anchor.actual is None:
        return False
    return anchor.expected != anchor.actual


def _count_is_unknown(integrity: IntegrityFlags) -> bool:
    """An undecidable test count (either side ``None``) cannot prove a non-negative delta."""
    return integrity.test_count_before is None or integrity.test_count_after is None


def _count_is_blocked(integrity: IntegrityFlags) -> bool:
    """A decided net test loss (``after < before``) is an integrity failure."""
    before = integrity.test_count_before
    after = integrity.test_count_after
    if before is None or after is None:
        return False
    return after < before


def reduce_verdict(pkg: EvidencePackage) -> Verdict:
    """Reduce an evidence package to its fail-closed three-valued verdict (plan §4.3).

    See the module docstring for the full truth table. ``UNKNOWN`` dominates ``BLOCKED``
    dominates ``GREEN``; only a fully-affirmative package returns
    :attr:`~genome.verify_gate.model.Verdict.GREEN`.
    """
    integrity = pkg.integrity

    # Undecidable signals → UNKNOWN (dominates everything below).
    any_unknown = (
        any(status is StepStatus.UNKNOWN for _name, status in pkg.steps)
        or any(_anchor_is_unknown(a) for a in pkg.anchors)
        or _count_is_unknown(integrity)
        or pkg.rebuild_pending
    )

    # Decided failures → BLOCKED.
    any_blocked = (
        any(status is StepStatus.FAIL for _name, status in pkg.steps)
        or any(_anchor_is_blocked(a) for a in pkg.anchors)
        or integrity.weakened_or_removed_test
        or integrity.gate_fill_survivor
        or not integrity.changelog_present
        or not integrity.docs_check_clean
        or _count_is_blocked(integrity)
    )

    if any_unknown:
        return Verdict.UNKNOWN
    if any_blocked:
        return Verdict.BLOCKED
    return Verdict.GREEN

"""Reconciliation guard for the independent ``GUARD_CLASS_VOCAB`` (plan refinement A1).

``genome.fast_follow.model`` deliberately defines its OWN ``GUARD_CLASS_VOCAB`` rather than
importing :data:`genome.verify_gate.model.CHANGE_CLASS_VOCAB` — the two consumers use the same
labels with *opposite polarity* (verify_gate: a positive check-set selector; fast_follow: a
guard whose membership forces EJECT), so a raw shared frozenset would be action-at-a-distance on
B's *safety* classifier (a verify_gate vocab edit would silently re-route the guard).

Decoupling without drift requires this reconciliation test: ``GUARD_CLASS_VOCAB`` must stay a
subset of verify_gate's ``CHANGE_CLASS_VOCAB``, and the guarded subset the classifier EJECTs on
must be exactly the non-``core`` classes. If Sub-A's vocab changes, this test fails loudly rather
than the guard re-routing silently — the single-source-of-truth benefit kept, the coupling trap
removed.
"""

from __future__ import annotations

from genome.fast_follow.model import GUARD_CLASS_VOCAB, GUARDED_CLASSES
from genome.verify_gate.model import CHANGE_CLASS_VOCAB


def test_guard_class_vocab_reconciles_with_verify_gate() -> None:
    """fast_follow's independent vocab stays a subset of verify_gate's change-class vocab."""
    assert GUARD_CLASS_VOCAB <= CHANGE_CLASS_VOCAB


def test_guarded_classes_are_the_non_core_change_classes() -> None:
    """The EJECT-forcing guarded subset is exactly the change classes other than ``core``."""
    assert GUARD_CLASS_VOCAB - {"core"} == GUARDED_CLASSES
    assert "core" not in GUARDED_CLASSES

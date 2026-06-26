"""The auto-tune asymmetry invariant — only a covered, clean TIGHTEN can ever auto-apply.

from: §5 test #10 (test_calibration_asymmetry.py) + §6:
  * over a set of LOOSEN scenarios AND a set of uncovered-knob-TIGHTEN scenarios,
    ``propose_ratchet`` NEVER returns AUTO_COMMIT (``auto_applicable`` False) — the test FAILS the
    instant either auto-applies;
  * the invariant ``auto_applicable <=> (TIGHTEN AND backtest_clean AND knob_covered)`` holds for
    every decision.

The biconditional is a STRUCTURAL property of each ``RatchetDecision`` — checkable on whatever
the ratchet returns, independent of how the knob was attributed — so it is the robust core of the
safety guarantee (Decisions 2/3: under-tiering is the irreversible error, so tightening may
auto-apply; loosening is the one dangerous direction and stays human-gated). RED until
``propose_ratchet`` lands. test->spec provenance is stamped per test.
"""

from __future__ import annotations

import _calibration_ledger as cl
from genome.calibration.model import Direction, Disposition, RatchetDecision
from genome.calibration.ratchet import propose_ratchet

#: Scenarios that must NEVER auto-commit. ``L0-covered`` is the load-bearing one (Stage-3 D): a
#: systematic over-tier whose dominant sub-score is ``b_buckets.small`` — a COVERED knob — so it is
#: the loosen guard (direction == LOOSEN -> PARK), NOT the coverage gate, that stops it; delete the
#: loosen->PARK guard and it auto-commits a covered, back-test-clean loosen. ``L1`` / ``L2`` are
#: loosens too but target the UNCOVERED ``c_map.pipeline`` (coverage would PARK them regardless).
#: ``L3`` is actually a clean-by-vacuity TIGHTEN — the ``c=2``/``b=2`` tie attributes to the
#: uncovered ``b_buckets.moderate``, lowering which drops no unfloored probe (-> TIGHTEN -> PARK by
#: vacuity) — kept only as a "must not auto-commit" case, not a true loosen.
_COVERED_LOOSEN_LEDGER = cl.ledger(
    12, 2, cl.breakdown(b=1), cl.actual_mild_friction(), prefix="L0-covered"
)
_LOOSEN_LEDGERS = (
    _COVERED_LOOSEN_LEDGER,
    cl.ledger(12, 2, cl.breakdown(c=3, b=1, p=1), cl.actual_mild_friction(), prefix="L1"),
    cl.ledger(12, 2, cl.breakdown(c=3, b=2), cl.actual_mild_friction(), prefix="L2"),
    cl.ledger(12, 2, cl.breakdown(c=2, b=2, p=1), cl.actual_mild_friction(), prefix="L3"),
)

#: UNCOVERED-knob TIGHTEN scenarios — under-tiering whose dominant sub-score is a PARK-ONLY knob
#: (pipeline C=3, moderate B=2, large B=3, correction P=2): a clean tighten but clean-by-vacuity.
_UNCOVERED_TIGHTEN_LEDGERS = (
    cl.ledger(12, 1, cl.breakdown(c=3), cl.actual_blocked(), prefix="U-pipeline"),
    cl.ledger(12, 1, cl.breakdown(b=2), cl.actual_blocked(), prefix="U-moderate"),
    cl.ledger(12, 1, cl.breakdown(b=3), cl.actual_blocked(), prefix="U-large"),
    cl.ledger(12, 1, cl.breakdown(p=2), cl.actual_blocked(), prefix="U-correction"),
)


def _decisions_across_the_matrix() -> list[RatchetDecision]:
    """One decision from every disposition family — for the structural biconditional check."""
    scenarios = [
        *_LOOSEN_LEDGERS,
        *_UNCOVERED_TIGHTEN_LEDGERS,
        cl.ledger(12, 1, cl.breakdown(c=1), cl.actual_blocked(), prefix="AC"),  # AUTO_COMMIT
        cl.ledger(12, 1, cl.breakdown(b=1), cl.actual_blocked(), prefix="SUP"),  # SUPPRESSED
        cl.ledger(9, 1, cl.breakdown(c=1), cl.actual_blocked(), prefix="THIN"),  # NO_OP (thin)
    ]
    return [propose_ratchet(s, cl.enabled(), merges_since_last=5) for s in scenarios]


def test_no_loosen_or_uncovered_tighten_ever_auto_commits() -> None:
    """from: §6 (LOOSEN + uncovered-knob-TIGHTEN scenarios NEVER auto-commit).

    The asymmetry teeth: across every loosen scenario AND every uncovered-knob tighten scenario,
    the verdict is never AUTO_COMMIT and ``auto_applicable`` is never True. The test fails the
    instant either family auto-applies.
    """
    for outcomes in (*_LOOSEN_LEDGERS, *_UNCOVERED_TIGHTEN_LEDGERS):
        decision = propose_ratchet(outcomes, cl.enabled(), merges_since_last=5)
        assert decision.disposition is not Disposition.AUTO_COMMIT
        assert decision.auto_applicable is False


def test_auto_applicable_iff_covered_clean_tighten() -> None:
    """from: §6 (auto_applicable <=> (TIGHTEN AND backtest_clean AND knob_covered)).

    The derived ``auto_applicable`` flag is exactly the conjunction the AUTO_COMMIT path requires,
    on every decision the ratchet can produce — a structural invariant that no attribution detail
    can violate. A loosen (direction != TIGHTEN), a dirty back-test, or an uncovered knob each
    forces the flag False; only all three together makes it True.
    """
    for decision in _decisions_across_the_matrix():
        expected = (
            decision.direction is Direction.TIGHTEN
            and decision.backtest_clean
            and decision.knob_covered
        )
        assert decision.auto_applicable == expected
        assert (decision.disposition is Disposition.AUTO_COMMIT) == decision.auto_applicable


def test_covered_clean_loosen_parks_via_the_loosen_guard_not_coverage() -> None:
    """from: Stage-3 D — the loosen->PARK guard has teeth only on a COVERED knob.

    A systematic over-tier on ``b_buckets.small`` (covered, AND back-test-clean when lowered)
    classifies as a LOOSEN by tier-delta and must PARK. Coverage cannot mask it (the knob IS
    covered) and the back-test is clean — so this is exactly the case that goes red if the
    ``direction is LOOSEN -> PARK`` guard in ``propose_ratchet`` is deleted (it would AUTO_COMMIT a
    covered, clean loosen — an under-scrutiny tune applied without a human).
    """
    decision = propose_ratchet(_COVERED_LOOSEN_LEDGER, cl.enabled(), merges_since_last=5)
    assert decision.knob == "b_buckets.small"
    assert decision.direction is Direction.LOOSEN
    assert decision.knob_covered is True
    assert decision.backtest_clean is True
    assert decision.disposition is Disposition.PARK_FOR_APPROVAL
    assert decision.auto_applicable is False

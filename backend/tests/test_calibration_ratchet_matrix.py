"""The asymmetric ratchet disposition matrix — every gate + the four terminal verdicts.

from: §5 test #9 (test_calibration_ratchet_matrix.py) + §6:
  * a covered, back-test-clean TIGHTEN under-tiering ledger -> AUTO_COMMIT;
  * an over-tiering ledger -> PARK_FOR_APPROVAL;
  * a back-test-flipping TIGHTEN candidate -> SUPPRESSED;
  * thin-data (len < 10) -> NO_OP;
  * kill-switch (auto_tuning_enabled=False) -> NO_OP even on a clean covered tighten;
  * cadence (merges_since_last < 5) -> NO_OP;
  * hysteresis (< 3 same-direction misses) -> NO_OP;
  * floors never appear as a ratchet knob.

Each ledger is built from one auditable place (``_calibration_ledger``) and shaped so the
targeted knob is identifiable from the breakdown the OutcomeRecord carries: C=1 -> cli/tests
(both clean + covered -> AUTO_COMMIT), B=1 -> b_buckets.small (a +1 flips PR-7 -> SUPPRESSED).
The global gates (kill-switch / thin-data / cadence) are attribution-independent. RED until
``propose_ratchet`` lands. test->spec provenance is stamped per test.
"""

from __future__ import annotations

import _calibration_ledger as cl
from genome.calibration.model import KNOB_COVERAGE, Direction, Disposition
from genome.calibration.ratchet import (
    CADENCE_MIN_MERGES,
    HYSTERESIS_MIN_RUNS,
    THIN_DATA_MIN_OUTCOMES,
    propose_ratchet,
)

#: The full tunable-knob namespace (additive knobs + the two thresholds). A floor is NOT in here.
_TUNABLE_KNOBS = set(KNOB_COVERAGE) | {"t1", "t2"}


def test_covered_clean_tighten_under_tiering_ledger_auto_commits() -> None:
    """from: §6 (a covered, back-test-clean TIGHTEN under-tiering ledger -> AUTO_COMMIT).

    A ledger of Tier-1 predictions the gate blocked (hindsight Tier 2) whose only nonzero
    sub-score is C=1 targets a value-1 c_map knob (cli/tests) — both back-test-clean AND
    unfloored-covered — the one path that auto-applies.
    """
    under = cl.ledger(12, 1, cl.breakdown(c=1), cl.actual_blocked())
    decision = propose_ratchet(under, cl.enabled(), merges_since_last=5)
    assert decision.disposition is Disposition.AUTO_COMMIT
    assert decision.auto_applicable is True
    assert decision.knob in {"c_map.cli", "c_map.tests"}


def test_over_tiering_ledger_parks_for_approval() -> None:
    """from: §6 (an over-tiering ledger -> PARK_FOR_APPROVAL).

    Systematic over-tiering (unfloored Tier-2 predictions, mild-friction actuals -> hindsight
    Tier 1) is a loosen — always human-gated, never auto-applied.
    """
    over = cl.ledger(12, 2, cl.breakdown(c=3, b=1, p=1), cl.actual_mild_friction())
    decision = propose_ratchet(over, cl.enabled(), merges_since_last=5)
    assert decision.disposition is Disposition.PARK_FOR_APPROVAL


def test_back_test_flipping_tighten_is_suppressed() -> None:
    """from: §6 (a back-test-flipping TIGHTEN candidate -> SUPPRESSED).

    A ledger whose only nonzero sub-score is B=1 targets ``b_buckets.small``; its +1 tighten
    (small 1->2) flips PR-7 (1->2) off its settled tier, so the candidate is back-test-DIRTY and
    is suppressed (before any coverage check) — a tighten that would overturn history never lands.
    """
    under = cl.ledger(12, 1, cl.breakdown(b=1), cl.actual_blocked())
    decision = propose_ratchet(under, cl.enabled(), merges_since_last=5)
    assert decision.disposition is Disposition.SUPPRESSED


def test_thin_data_is_a_no_op() -> None:
    """from: §6 (thin-data (len < 10) -> NO_OP).

    Below the overfitting threshold the ratchet refuses to act regardless of signal — 9 strongly
    under-tiering outcomes still produce NO_OP.
    """
    under = cl.ledger(9, 1, cl.breakdown(c=1), cl.actual_blocked())
    decision = propose_ratchet(under, cl.enabled(), merges_since_last=5)
    assert decision.disposition is Disposition.NO_OP


def test_kill_switch_is_a_no_op_even_on_a_clean_covered_tighten() -> None:
    """from: §6 (kill-switch (auto_tuning_enabled=False) -> NO_OP even on a clean covered tighten).

    The exact ledger that AUTO_COMMITs under enabled weights becomes a NO_OP under the seed
    (``auto_tuning_enabled = False``) — the report-only ship is dark by the outermost gate.
    """
    under = cl.ledger(12, 1, cl.breakdown(c=1), cl.actual_blocked())
    decision = propose_ratchet(under, cl.SEED_RISK_WEIGHTS, merges_since_last=5)
    assert decision.disposition is Disposition.NO_OP


def test_cadence_not_met_is_a_no_op() -> None:
    """from: §6 (cadence (merges_since_last < 5) -> NO_OP).

    With only 4 merges since the last pass the cadence gate holds the ratchet at NO_OP even on a
    clean covered tighten.
    """
    under = cl.ledger(12, 1, cl.breakdown(c=1), cl.actual_blocked())
    decision = propose_ratchet(under, cl.enabled(), merges_since_last=4)
    assert decision.disposition is Disposition.NO_OP


def test_hysteresis_not_met_is_a_no_op() -> None:
    """from: §6 (hysteresis (< 3 same-direction misses) -> NO_OP).

    A 12-outcome ledger with only 2 under-tiering misses on the knob (the other 10 clean) has not
    cleared the 3-in-one-direction hysteresis gate, so no change is proposed — NO_OP.
    """
    under = cl.ledger(2, 1, cl.breakdown(c=1), cl.actual_blocked(), prefix="U")
    clean = cl.ledger(10, 1, cl.breakdown(c=1), cl.actual_clean(), prefix="C")
    decision = propose_ratchet([*under, *clean], cl.enabled(), merges_since_last=5)
    assert decision.disposition is Disposition.NO_OP


def test_floors_never_appear_as_a_ratchet_knob() -> None:
    """from: §6 (floors never appear as a ratchet knob).

    Across the AUTO_COMMIT / SUPPRESSED / PARK ledgers, any proposed ``knob`` is one of the
    tunable additive knobs or the two thresholds — never a floor mechanism (schema|ddl / anchors
    are immutable and not representable as a knob).
    """
    ledgers = (
        cl.ledger(12, 1, cl.breakdown(c=1), cl.actual_blocked(), prefix="A"),
        cl.ledger(12, 1, cl.breakdown(b=1), cl.actual_blocked(), prefix="S"),
        cl.ledger(12, 2, cl.breakdown(c=3, b=1, p=1), cl.actual_mild_friction(), prefix="P"),
    )
    for outcomes in ledgers:
        decision = propose_ratchet(outcomes, cl.enabled(), merges_since_last=5)
        if decision.knob is not None:
            assert decision.knob in _TUNABLE_KNOBS, decision.knob
            assert "floor" not in decision.knob


def test_covered_clean_loosen_parks_not_auto_commits() -> None:
    """from: Stage-3 D (a COVERED, back-test-clean loosen must PARK, never AUTO_COMMIT).

    An over-tiering ledger whose dominant sub-score is ``b_buckets.small`` (a COVERED knob) drives a
    ``-1`` candidate that lowers an unfloored ladder probe (LOOSEN) and flips no back-test row
    (clean). Only the loosen guard stops it — coverage cannot — so this is the asymmetry's teeth on
    the loosen path: it goes red if ``direction is LOOSEN -> PARK`` is removed.
    """
    over = cl.ledger(12, 2, cl.breakdown(b=1), cl.actual_mild_friction())
    decision = propose_ratchet(over, cl.enabled(), merges_since_last=5)
    assert decision.knob == "b_buckets.small"
    assert decision.direction is Direction.LOOSEN
    assert decision.knob_covered is True
    assert decision.backtest_clean is True
    assert decision.disposition is Disposition.PARK_FOR_APPROVAL


def test_thin_data_boundary_clears_at_exactly_k_outcomes() -> None:
    """from: Stage-3 G ptest-7 (the thin-data gate first CLEARS at exactly K=10 outcomes).

    9 under-tiering outcomes are a NO_OP (tested above); at exactly ``THIN_DATA_MIN_OUTCOMES`` the
    gate first lets the pass through — a covered-tighten ledger of 10 auto-commits, proving the
    boundary is ``len < K`` (not ``<=``).
    """
    assert THIN_DATA_MIN_OUTCOMES == 10
    under = cl.ledger(THIN_DATA_MIN_OUTCOMES, 1, cl.breakdown(c=1), cl.actual_blocked())
    decision = propose_ratchet(under, cl.enabled(), merges_since_last=CADENCE_MIN_MERGES)
    assert decision.disposition is Disposition.AUTO_COMMIT


def test_hysteresis_boundary_clears_at_exactly_three_misses() -> None:
    """from: Stage-3 G ptest-8 (the hysteresis gate first CLEARS at exactly HYST=3 same-direction).

    With 2 same-direction misses on the knob the ratchet is a NO_OP (tested above); at exactly
    ``HYSTERESIS_MIN_RUNS`` misses (padded with clean outcomes to clear the thin-data gate) it first
    proposes a change — the boundary is ``|misses| < HYST`` (not ``<=``).
    """
    assert HYSTERESIS_MIN_RUNS == 3
    misses = cl.ledger(HYSTERESIS_MIN_RUNS, 1, cl.breakdown(c=1), cl.actual_blocked(), prefix="M")
    clean = cl.ledger(10, 1, cl.breakdown(c=1), cl.actual_clean(), prefix="C")
    decision = propose_ratchet(
        [*misses, *clean], cl.enabled(), merges_since_last=CADENCE_MIN_MERGES
    )
    assert decision.disposition is Disposition.AUTO_COMMIT

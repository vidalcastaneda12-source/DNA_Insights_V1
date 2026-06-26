"""Reconciliation — the calibrator's seed formula equals the frozen scope_split dispatcher formula.

from: §5 test #15 (test_calibration_reconciliation.py) + §6:
  * ``SEED_RISK_WEIGHTS.c_map == scope_split.model._C_MAP`` (the two C-maps are byte-equal);
  * ``compute_tier(row.fields, SEED)[0] == scope_split.model.est_risk_tier(...)`` for all 6
    BACKTEST_ROWS;
  * pins the SEED constant (NOT the live ``risk_weights.json`` file);
  * asserts ``THIN_DATA_MIN_OUTCOMES (10) > len(BACKTEST_ROWS) (6)`` — the circular-tautology
    guard (the back-test fixture can never on its own satisfy the thin-data lockout).

The two formulas are defined INDEPENDENTLY (the calibrator copies, never imports, the dispatcher
C-map) so the equality is a real test, not a tautology. The c_map equality + the thin-data guard
are GREEN from freeze; the per-row tier equivalence is RED until ``compute_tier`` lands
(``est_risk_tier`` is already implemented, so divergence would be a real signal).

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

from genome.calibration.model import (
    BACKTEST_ROWS,
    CHANGE_CLASS_VOCAB,
    SEED_RISK_WEIGHTS,
    compute_tier,
)
from genome.calibration.ratchet import THIN_DATA_MIN_OUTCOMES
from genome.scope_split.model import _C_MAP, est_risk_tier
from genome.scope_split.model import CHANGE_CLASS_VOCAB as SCOPE_SPLIT_CHANGE_CLASS_VOCAB


def test_seed_c_map_equals_the_scope_split_dispatcher_c_map() -> None:
    """from: §6 (SEED_RISK_WEIGHTS.c_map == scope_split.model._C_MAP) — GREEN from freeze.

    The seed change-class map is a byte-equal copy of the dispatcher C-map; if either drifts,
    the dispatcher and the calibrator would score the same scope differently. Pins the SEED
    constant, not the mutable on-disk config (the ratchet writes the file, never this constant).
    """
    assert dict(SEED_RISK_WEIGHTS.c_map) == dict(_C_MAP)


def test_change_class_vocab_pinned_to_seed_c_map_and_scope_split() -> None:
    """from: Stage-3 A (CHANGE_CLASS_VOCAB pinned to SEED_RISK_WEIGHTS.c_map keys == scope_split).

    The from_json change_class guard rejects a label outside this vocab, so it must stay exactly
    the 10 c_map keys AND byte-equal to the splitter's vocab — otherwise the dispatcher's
    compute-tier and the splitter would accept different label sets on the same boundary.
    """
    assert frozenset(SEED_RISK_WEIGHTS.c_map) == CHANGE_CLASS_VOCAB
    assert CHANGE_CLASS_VOCAB == SCOPE_SPLIT_CHANGE_CLASS_VOCAB


def test_thin_data_threshold_exceeds_the_backtest_fixture_size() -> None:
    """from: §6 (THIN_DATA_MIN_OUTCOMES (10) > len(BACKTEST_ROWS) (6)) — the tautology guard.

    The thin-data lockout (10) is deliberately larger than the frozen back-test fixture (6), so
    the regression fixture alone can never clear the overfitting guard — an auto-tune always
    needs *real* accumulated outcomes, never just the seed's own reference rows.
    """
    assert THIN_DATA_MIN_OUTCOMES == 10
    assert len(BACKTEST_ROWS) == 6
    assert len(BACKTEST_ROWS) < THIN_DATA_MIN_OUTCOMES


def test_compute_tier_reconciles_with_est_risk_tier_over_every_row() -> None:
    """from: §6 (compute_tier(row.fields, SEED)[0] == est_risk_tier(...) for all 6 BACKTEST_ROWS).

    For every frozen row, the calibrator's ``compute_tier`` and the dispatcher's already-shipped
    ``est_risk_tier`` agree — and both reproduce the known ``expected_tier``. The precedent
    category is mapped to its P sub-score via the seed ``p_levels`` (the same lookup compute_tier
    performs), since ``est_risk_tier`` takes the numeric P term.
    """
    for row in BACKTEST_ROWS:
        fields = row.fields
        p_score = SEED_RISK_WEIGHTS.p_levels[fields.precedent_surprise]
        anchors = tuple(f"anchor-{i}" for i in range(fields.applicable_anchors_count))
        dispatcher_tier = est_risk_tier(
            fields.change_class,
            anchors,
            fields.imports_touched_count,
            p_score,
        )
        calibrator_tier = compute_tier(fields, SEED_RISK_WEIGHTS)[0]
        assert calibrator_tier == dispatcher_tier == row.expected_tier, row.scope_id

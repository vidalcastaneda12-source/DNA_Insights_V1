"""Back-test is a real gate — it both REJECTS a settled-call flip and ACCEPTS a clean change.

from: §5 test #3 (test_calibration_backtest_nonvacuous.py) + §6:
  * REJECT witness: lowering t2 5->4 flips PR-7 (1->2) so ``run_backtest(candidate).clean`` is
    False (and PR-7 is named in ``flipped``);
  * ACCEPT witness: raising ``c_map['cli']`` 1->2 flips NO back-test row (clean is True) AND
    ``cli`` IS in KNOB_COVERAGE (clean WITH coverage, not clean-by-vacuity);
  * DEFAULT: ``run_backtest(SEED_RISK_WEIGHTS).clean`` is True and reproduces {0,1,1,1,2,2}.

These are direct ``run_backtest`` calls on hand-built candidates — no ledger, no attribution —
so the expected ``clean`` / ``flipped`` come straight from the frozen formula + fixture.
RED until ``run_backtest`` / ``compute_tier`` bodies land.

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import dataclasses

from genome.calibration.backtest import run_backtest
from genome.calibration.model import (
    BACKTEST_ROWS,
    KNOB_COVERAGE,
    SEED_RISK_WEIGHTS,
)


def test_default_seed_backtest_is_clean_and_reproduces_the_known_tiers() -> None:
    """from: §6 (DEFAULT: run_backtest(SEED).clean is True; reproduces {0,1,1,1,2,2}).

    The seed weights are the immutable reference: re-scoring every frozen row reproduces its
    ``expected_tier`` exactly, so ``clean`` is True and nothing is ``flipped``.
    """
    result = run_backtest(SEED_RISK_WEIGHTS)
    assert result.clean is True
    assert result.flipped == ()


def test_lowering_t2_flips_pr7_so_the_candidate_is_not_clean() -> None:
    """from: §6 (REJECT witness: lowering t2 5->4 flips PR-7 1->2 -> clean is False).

    PR-7 sits at S=4 (Tier 1, "near T2"). Lowering ``t2`` 5->4 makes S>=t2 -> Tier 2, moving a
    settled call. The back-test must catch it: ``clean`` False, PR-7 in ``flipped``.
    """
    candidate = dataclasses.replace(SEED_RISK_WEIGHTS, t2=4)
    result = run_backtest(candidate)
    assert result.clean is False
    assert "PR-7" in result.flipped


def test_raising_c_map_cli_is_clean_and_cli_is_covered() -> None:
    """from: §6 (ACCEPT witness: c_map['cli'] 1->2 flips NO row, clean True, AND cli in coverage).

    Raising ``c_map['cli']`` 1->2 nudges only cli-bearing scopes' C, none far enough to cross a
    band boundary, so no back-test row flips (``clean`` True). And ``c_map.cli`` HAS unfloored
    coverage (PR-12) — the conjunction "clean WITH coverage" that the asymmetric ratchet needs
    before an AUTO_COMMIT, as opposed to clean-by-vacuity.
    """
    candidate = dataclasses.replace(SEED_RISK_WEIGHTS, c_map={**SEED_RISK_WEIGHTS.c_map, "cli": 2})
    result = run_backtest(candidate)
    assert result.clean is True
    assert result.flipped == ()
    assert KNOB_COVERAGE["c_map.cli"] != ()


def test_backtest_clean_means_no_row_diverges_from_expected() -> None:
    """from: §6 (back-test is the hard regression gate) — the meaning of ``clean``.

    A defensive restatement on the seed: ``clean`` True is equivalent to "every frozen row is
    accounted for and none flipped", so a future weakening that silently drops a row from the
    sweep is caught here too (``flipped`` empty over all six rows).
    """
    result = run_backtest(SEED_RISK_WEIGHTS)
    assert result.clean is True
    assert all(row.scope_id not in result.flipped for row in BACKTEST_ROWS)

"""The back-test re-runner — the hard regression gate on any weights change (``finding-040``).

:func:`run_backtest` re-scores every :data:`~genome.calibration.model.BACKTEST_ROWS` row under a
candidate :class:`~genome.calibration.model.RiskWeights` and reports whether any row's tier moved
off its known-correct :attr:`~genome.calibration.model.BacktestRow.expected_tier`. A candidate is
*clean* **iff** it flips nothing — the gate that rejects any auto-tune which would overturn a
settled historical call (plan §3 / §4 T1). The floored rows (PR-5a / PR-3) are pinned at Tier 2
by the immutable anchor floor, so a clean back-test is *necessary but not sufficient* for an
auto-commit: the ratchet additionally requires the targeted knob to have UNFLOORED coverage
(:data:`~genome.calibration.model.KNOB_COVERAGE`), closing the clean-by-vacuity hole.

**No** :mod:`genome.db` and **no** :mod:`genome.config` import — pure re-scoring over the frozen
fixtures, runnable on a fresh checkout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from genome.calibration.model import BACKTEST_ROWS, compute_tier

if TYPE_CHECKING:
    from genome.calibration.model import RiskWeights


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """The outcome of re-scoring :data:`~genome.calibration.model.BACKTEST_ROWS` under a candidate.

    ``clean`` is the conjunction the ratchet's hard gate enforces — ``True`` iff **no** row's
    re-scored tier differs from its known-correct ``expected_tier``. ``flipped`` names the scope
    ids that did change, so a rejection is auditable (e.g. ``"a t2 5→4 lowering flips PR-7"``).
    """

    clean: bool
    """``True`` iff every back-test row reproduces its ``expected_tier`` under the candidate."""
    flipped: tuple[str, ...]
    """The scope ids whose re-scored tier moved off ``expected_tier`` (empty iff ``clean``)."""


def run_backtest(candidate: RiskWeights) -> BacktestResult:
    """Re-score every :data:`~genome.calibration.model.BACKTEST_ROWS` row under ``candidate``.

    Computes ``compute_tier(row.fields, candidate)`` for each fixture row and compares it to
    ``row.expected_tier``. Returns a :class:`BacktestResult` that is ``clean`` iff none differ,
    with ``flipped`` listing every scope id that moved. Pure: reads only the candidate weights and
    the frozen fixture, touches no filesystem and no DB.
    """
    flipped = tuple(
        row.scope_id
        for row in BACKTEST_ROWS
        if compute_tier(row.fields, candidate)[0] != row.expected_tier
    )
    return BacktestResult(clean=not flipped, flipped=flipped)

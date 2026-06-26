"""Shared outcome-ledger builders for the calibration ratchet / accuracy tests.

NOT a test module (leading underscore -> pytest does not collect it). It builds *realistic*
``OutcomeRecord`` ledgers from the FROZEN data model so the behavioral tests
(test_calibration_ratchet_matrix / _asymmetry / _coverage_park / _accuracy /
_direction_from_deltas) construct their scenarios from one auditable place rather than six
subtly-different copies.

The records are shaped to the spec's hindsight rules (``tier_in_hindsight``), never to any
implementation:
  * a ``blocked`` gate verdict drives hindsight >= Tier 2 (an under-tier signal vs a low predict);
  * a clean ``pass`` confirms the predicted tier (no error);
  * mild friction (>=2 revise cycles, otherwise clean) drives the hindsight cycle branch
    (Tier 1) — an over-tier signal vs a Tier-2 predict.

Sub-scores live on the ``TierBreakdown`` (the only knob signal an OutcomeRecord carries — it
holds no raw change_class), so a "ledger targeting knob K" is one whose dominant sub-score is
K's: C=1 -> a c_map value-1 knob (cli/tests), C=3 -> c_map.pipeline (unique), B=1 ->
b_buckets.small (unique band value), etc.
"""

from __future__ import annotations

import dataclasses

from genome.calibration.model import (
    SEED_RISK_WEIGHTS,
    ActualBlock,
    OutcomeRecord,
    PredictedBlock,
    RiskWeights,
    TierBreakdown,
)


def breakdown(  # noqa: PLR0913 — a keyword-only sub-score builder; each knob is a named arg
    *,
    c: int = 0,
    b: int = 0,
    p: int = 0,
    a: int = 0,
    floor: int = 0,
    deep_t2: bool = False,
) -> TierBreakdown:
    """A TierBreakdown with ``s = c + b + p`` (A folds into Tier 2 only, never into S)."""
    return TierBreakdown(c=c, b=b, p=p, a=a, s=c + b + p, floor=floor, deep_t2=deep_t2)


def actual_blocked() -> ActualBlock:
    """A merge-time block — drives ``tier_in_hindsight`` to >= Tier 2 (under-tier signal)."""
    return ActualBlock(gate_verdict="blocked")


def actual_clean() -> ActualBlock:
    """A clean pass with no friction — ``tier_in_hindsight`` confirms the predicted tier."""
    return ActualBlock(gate_verdict="pass")


def actual_mild_friction() -> ActualBlock:
    """A clean pass that took 2 revise cycles — the hindsight cycle branch (Tier 1).

    Against a Tier-2 prediction this is an over-tier signal (hindsight 1 < predicted 2); against
    a Tier-0 prediction it is a mild under-tier signal (hindsight 1 > predicted 0).
    """
    return ActualBlock(gate_verdict="pass", revise_cycles=2)


def outcome(
    scope_id: str,
    predicted_tier: int,
    bd: TierBreakdown,
    actual: ActualBlock,
    *,
    weights_version: str = "rw-1",
) -> OutcomeRecord:
    """One realistic outcome-ledger datum pairing a prediction with merge-time ground truth."""
    return OutcomeRecord(
        scope_id=scope_id,
        merged_sha=f"sha-{scope_id}",
        date="2026-06-25",
        risk_weights_version=weights_version,
        predicted=PredictedBlock(tier=predicted_tier, breakdown=bd),
        actual=actual,
    )


def ledger(
    n: int,
    predicted_tier: int,
    bd: TierBreakdown,
    actual: ActualBlock,
    *,
    prefix: str = "PR-X",
) -> list[OutcomeRecord]:
    """``n`` distinct outcomes that share a prediction shape + a merge-time outcome kind."""
    return [outcome(f"{prefix}-{i}", predicted_tier, bd, actual) for i in range(n)]


def enabled(weights: RiskWeights = SEED_RISK_WEIGHTS) -> RiskWeights:
    """The seed (or a candidate) with the auto-tuning kill switch flipped ON.

    The seed ships ``auto_tuning_enabled = False`` (report-only), so the ratchet matrix tests that
    need to reach past the kill-switch gate use this.
    """
    return dataclasses.replace(weights, auto_tuning_enabled=True)

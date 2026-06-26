"""Floors are immutable by construction — not representable in the tunable RiskWeights.

from: §5 test #6 (test_calibration_floors_immutable.py) + §6:
  * PR-5a / PR-3 stay Tier 2 under a swept set of candidate weights (any c_map / b / p change);
  * ``RiskWeights.from_json`` REJECTS a payload containing a ``"floor"`` key;
  * there is NO ``floor`` field on ``RiskWeights``.

Decision #4 of the C1 plan: the trip-wire floor (schema|ddl OR anchors -> Tier 2) is hard-coded
in ``compute_tier``, so no auto-tune can ever weaken it. The no-floor-field assertion is GREEN
from freeze (a structural fact of the frozen dataclass); the sweep + the from_json rejection are
RED until the bodies land.

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import dataclasses

import pytest

from genome.calibration.model import (
    BACKTEST_ROWS,
    SEED_RISK_WEIGHTS,
    RiskWeights,
    TierFields,
    compute_tier,
)


def _floored_fields() -> list[tuple[str, TierFields]]:
    """The two frozen anchor-floored rows (PR-5a / PR-3) as (scope_id, TierFields) pairs."""
    floored = {"PR-5a", "PR-3"}
    return [(row.scope_id, row.fields) for row in BACKTEST_ROWS if row.scope_id in floored]


def _seed_payload() -> dict[str, object]:
    """Reconstruct a valid ``risk_weights.json``-shaped mapping from the seed's public fields.

    Built from the frozen constant (not ``to_json``, which is stubbed) so the only thing under
    test in the floor-rejection case is the forbidden ``"floor"`` key.
    """
    return {
        "weights_version": SEED_RISK_WEIGHTS.weights_version,
        "c_map": dict(SEED_RISK_WEIGHTS.c_map),
        "b_buckets": dict(SEED_RISK_WEIGHTS.b_buckets),
        "p_levels": dict(SEED_RISK_WEIGHTS.p_levels),
        "t1": SEED_RISK_WEIGHTS.t1,
        "t2": SEED_RISK_WEIGHTS.t2,
        "auto_tuning_enabled": SEED_RISK_WEIGHTS.auto_tuning_enabled,
        "provenance": dict(SEED_RISK_WEIGHTS.provenance),
    }


def _sweep_candidates() -> list[RiskWeights]:
    """A spread of candidate weights that each try (and must fail) to lower the floored tier."""
    zero_c = dict.fromkeys(SEED_RISK_WEIGHTS.c_map, 0)
    zero_b = dict.fromkeys(SEED_RISK_WEIGHTS.b_buckets, 0)
    zero_p = dict.fromkeys(SEED_RISK_WEIGHTS.p_levels, 0)
    return [
        dataclasses.replace(SEED_RISK_WEIGHTS, c_map=zero_c),
        dataclasses.replace(SEED_RISK_WEIGHTS, b_buckets=zero_b),
        dataclasses.replace(SEED_RISK_WEIGHTS, p_levels=zero_p),
        dataclasses.replace(SEED_RISK_WEIGHTS, t1=0, t2=999),
        dataclasses.replace(
            SEED_RISK_WEIGHTS, c_map=zero_c, b_buckets=zero_b, p_levels=zero_p, t2=999
        ),
    ]


def test_floored_rows_stay_tier_2_under_every_swept_candidate() -> None:
    """from: §6 (PR-5a / PR-3 stay Tier 2 under a swept set of candidate weights).

    PR-5a and PR-3 are floored by anchor exposure. Across a sweep that zeroes every additive
    knob and pushes ``t2`` out of reach, the immutable floor still pins them at Tier 2 — no
    tunable change can demote a settled anchor-exposed scope.
    """
    for candidate in _sweep_candidates():
        for scope_id, fields in _floored_fields():
            tier, breakdown = compute_tier(fields, candidate)
            assert tier == 2, f"{scope_id} demoted under a candidate weights sweep"
            assert breakdown.floor == 2, f"{scope_id} lost its immutable floor"


def test_from_json_rejects_a_floor_key() -> None:
    """from: §6 (RiskWeights.from_json REJECTS a payload containing a "floor" key).

    The floor is not representable, so a config that smuggles in a ``"floor"`` knob is malformed
    and fails closed (ValueError) — an auto-tune can never re-introduce a tunable floor.
    """
    payload = _seed_payload()
    payload["floor"] = 2
    with pytest.raises(ValueError, match="floor"):
        RiskWeights.from_json(payload)


def test_risk_weights_has_no_floor_field() -> None:
    """from: §6 ("there is no floor field on RiskWeights") — GREEN from freeze.

    The tunable surface is exactly weights_version / c_map / b_buckets / p_levels / t1 / t2 /
    auto_tuning_enabled / provenance. ``floor`` is absent by construction, so even a buggy
    ``from_json`` has nowhere to bind it.
    """
    field_names = {f.name for f in dataclasses.fields(RiskWeights)}
    assert "floor" not in field_names
    assert field_names == {
        "weights_version",
        "c_map",
        "b_buckets",
        "p_levels",
        "t1",
        "t2",
        "auto_tuning_enabled",
        "provenance",
    }

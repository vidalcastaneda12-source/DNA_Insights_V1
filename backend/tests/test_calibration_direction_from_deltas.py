"""Direction is by tier-delta over the ladder, never the knob's numeric sign (FIX-2).

from: §5 test #5 (test_calibration_direction_from_deltas.py) + §6:
  * a t2-RAISE and a t1-RAISE are EACH a LOOSEN (they lower a ladder probe's tier) and route to
    PARK; a ``c_map['cli']`` raise is a TIGHTEN (it lowers no probe);
  * the direction is derived from the SIGN of tier deltas over DIRECTION_WITNESS_LADDER, never
    the knob's numeric direction;
  * the synthetic unfloored S>=t2 witness in the ladder is what makes the t2-raise register as a
    loosen — assert the ladder contains it.

Predicted-surprise guard (WITNESS-LADDER S>=t2): without the synthetic unfloored S>=t2 probe a
t2-raise yields ZERO tier deltas and is mislabeled a tighten. We assert (a) the ladder carries
that probe and (b) a t2-raise actually lowers its tier. The tier-delta semantics are tested
directly through ``compute_tier`` + the frozen ladder; the Direction enum + PARK routing through
``propose_ratchet`` on over/under-tiering ledgers.

The over-tiering -> LOOSEN -> PARK case depends on the spec's hindsight cycle branch (a Tier-2
predict with mild friction -> hindsight Tier 1); it is RED until ``tier_in_hindsight`` +
``propose_ratchet`` land. test->spec provenance is stamped per test.
"""

from __future__ import annotations

import dataclasses

import _calibration_ledger as cl
from genome.calibration.model import (
    DIRECTION_WITNESS_LADDER,
    SEED_RISK_WEIGHTS,
    Direction,
    Disposition,
    TierFields,
    compute_tier,
)
from genome.calibration.ratchet import propose_ratchet

#: The synthetic unfloored S=5 (== t2) witness: pipeline + small footprint + minor precedent,
#: NO anchors -> unfloored Tier 2. This is the probe a t2-raise must demote.
_WITNESS_S5 = TierFields(
    change_class=("pipeline",),
    imports_touched_count=3,
    precedent_surprise="minor",
    applicable_anchors_count=0,
)

#: The synthetic unfloored S=1 (== t1) witness: a lone cli concern -> unfloored Tier 1. A t1
#: raise demotes it to Tier 0.
_WITNESS_S1 = TierFields(
    change_class=("cli",),
    imports_touched_count=0,
    precedent_surprise="clean",
    applicable_anchors_count=0,
)


def test_ladder_contains_the_synthetic_unfloored_s_ge_t2_witness() -> None:
    """from: §6 (assert the ladder contains the synthetic unfloored S>=t2 witness) — GREEN.

    The load-bearing probe (an unfloored S=5 row) IS in DIRECTION_WITNESS_LADDER. Without it,
    no real row sits at S>=t2 unfloored (PR-7 is S4; PR-5a/PR-3 are floored), so a t2-raise would
    produce zero deltas and be mislabeled a tighten.
    """
    assert _WITNESS_S5 in DIRECTION_WITNESS_LADDER


def test_t2_raise_lowers_the_unfloored_s5_witness_a_loosen_by_delta() -> None:
    """from: §6 (a t2-RAISE is a LOOSEN — it lowers an unfloored ladder probe's tier).

    Under the seed the S=5 witness is unfloored Tier 2; raising ``t2`` 5->6 demotes it to Tier 1
    — a negative tier delta, i.e. the loosen signal. (Floor stays 0 throughout: it is the *tier*
    that drops, proving the probe is genuinely unfloored.)
    """
    seed_tier, seed_breakdown = compute_tier(_WITNESS_S5, SEED_RISK_WEIGHTS)
    raised = dataclasses.replace(SEED_RISK_WEIGHTS, t2=6)
    raised_tier, _ = compute_tier(_WITNESS_S5, raised)
    assert seed_breakdown.floor == 0
    assert seed_tier == 2
    assert raised_tier < seed_tier


def test_t1_raise_lowers_the_unfloored_s1_witness_a_loosen_by_delta() -> None:
    """from: §6 (a t1-RAISE is a LOOSEN — it lowers an unfloored ladder probe's tier).

    The S=1 witness is unfloored Tier 1 under the seed; raising ``t1`` 1->2 demotes it to Tier 0
    — the same negative-delta loosen signal at the lower band boundary.
    """
    seed_tier, _ = compute_tier(_WITNESS_S1, SEED_RISK_WEIGHTS)
    raised = dataclasses.replace(SEED_RISK_WEIGHTS, t1=2)
    raised_tier, _ = compute_tier(_WITNESS_S1, raised)
    assert seed_tier == 1
    assert raised_tier < seed_tier


def test_c_map_cli_raise_lowers_no_ladder_probe_a_tighten_by_delta() -> None:
    """from: §6 (a c_map['cli'] raise is a TIGHTEN — it lowers no ladder probe).

    Raising ``c_map['cli']`` 1->2 can only hold or raise S, so no probe's tier drops over the
    whole ladder -> not a loosen -> TIGHTEN (the "otherwise" branch). This is the numeric-sign
    decoupling: a knob value going UP is a tighten, a threshold going up is a loosen.
    """
    raised = dataclasses.replace(SEED_RISK_WEIGHTS, c_map={**SEED_RISK_WEIGHTS.c_map, "cli": 2})
    for probe in DIRECTION_WITNESS_LADDER:
        assert compute_tier(probe, raised)[0] >= compute_tier(probe, SEED_RISK_WEIGHTS)[0]


def test_over_tiering_ledger_is_classified_loosen_and_parked() -> None:
    """from: §6 (a loosen routes to PARK) + Decision 2/3 (loosening is always human-gated).

    An over-tiering ledger (Tier-2 predictions whose actuals show only mild friction -> hindsight
    Tier 1) makes the ratchet want LESS scrutiny: ``direction == LOOSEN`` and the disposition is
    PARK_FOR_APPROVAL — a loosen never auto-applies.
    """
    over = cl.ledger(12, 2, cl.breakdown(c=3, b=1, p=1), cl.actual_mild_friction())
    decision = propose_ratchet(over, cl.enabled(), merges_since_last=5)
    assert decision.direction is Direction.LOOSEN
    assert decision.disposition is Disposition.PARK_FOR_APPROVAL


def test_under_tiering_ledger_is_classified_tighten() -> None:
    """from: §6 (a c_map raise is a TIGHTEN) — the Direction enum on a real ledger.

    An under-tiering ledger (Tier-1 predictions the gate blocked -> hindsight Tier 2) makes the
    ratchet want MORE scrutiny: ``direction == TIGHTEN``.
    """
    under = cl.ledger(12, 1, cl.breakdown(c=1), cl.actual_blocked())
    decision = propose_ratchet(under, cl.enabled(), merges_since_last=5)
    assert decision.direction is Direction.TIGHTEN

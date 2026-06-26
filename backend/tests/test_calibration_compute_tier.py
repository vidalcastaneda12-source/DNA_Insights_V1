"""Deterministic loop-closure — ``compute_tier`` is the single risk-tier source of truth.

from: §5 test #2 (test_calibration_compute_tier.py — the headline) + §6:
  * reproduces tiers {PR-8:0, PR-12:1, PR-6:1, PR-7:1, PR-5a:2, PR-3:2} over BACKTEST_ROWS;
  * a c_map raise that pushes a scope's S across t2 yields tier 2 (output RESPONDS to weights);
  * lowering t2 yields tier 2 (output RESPONDS to weights);
  * v2.1 amendment: deep_T2 True for S>=7 AND for A>=3; the dispatch-time +1 bump applies
    (base tier 1 -> final 2) when has_open_questions OR human_bump; a tier-2-already scope is
    UNCHANGED by the bump (min(2, .)).

Every expected value is taken from the FROZEN spec (the ``compute_tier`` docstring formula +
the ``BACKTEST_ROWS`` fixture's ``expected_tier``), never reverse-engineered from a body — the
bodies ``raise NotImplementedError`` so the file is honestly RED until the implementer lands
``compute_tier``.

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


def _row_fields(scope_id: str) -> TierFields:
    """The frozen ``TierFields`` for a back-test scope id (independent oracle: the fixture)."""
    for row in BACKTEST_ROWS:
        if row.scope_id == scope_id:
            return row.fields
    msg = f"no BACKTEST_ROW with scope_id {scope_id!r}"
    raise AssertionError(msg)


def _with_c_map(label: str, value: int) -> RiskWeights:
    """A candidate copy of the seed with a single ``c_map`` entry overridden."""
    return dataclasses.replace(SEED_RISK_WEIGHTS, c_map={**SEED_RISK_WEIGHTS.c_map, label: value})


# ── The headline: reproduce {0,1,1,1,2,2} over the frozen fixture ──────────────


def test_compute_tier_reproduces_every_backtest_tier() -> None:
    """from: §6 (compute_tier reproduces {PR-8:0, PR-12:1, PR-6:1, PR-7:1, PR-5a:2, PR-3:2}).

    The deterministic formula returns each frozen row's known-correct ``expected_tier`` under
    the seed weights — the loop-closure the dispatcher RUNS via ``compute-tier`` and consumes.
    """
    got = {row.scope_id: compute_tier(row.fields, SEED_RISK_WEIGHTS)[0] for row in BACKTEST_ROWS}
    assert got == {"PR-8": 0, "PR-12": 1, "PR-6": 1, "PR-7": 1, "PR-5a": 2, "PR-3": 2}


# ── Output RESPONDS to a risk_weights change (not hard-coded) ──────────────────


def test_c_map_raise_pushing_s_across_t2_yields_tier_2() -> None:
    """from: §6 ("a c_map raise that pushes a scope's S across t2 yields tier 2").

    PR-6 is S=3 (Tier 1) under the seed. Raising ``c_map['data-backfill']`` 2->4 lifts its
    C so S=5 == t2 -> Tier 2: the tier RESPONDS to the weight, proving the formula reads the
    tunable knobs rather than returning a hard-coded answer.
    """
    candidate = _with_c_map("data-backfill", 4)
    assert compute_tier(_row_fields("PR-6"), candidate)[0] == 2


def test_lowering_t2_yields_tier_2_for_an_unfloored_mid_scope() -> None:
    """from: §6 ("lowering t2 yields tier 2 (output RESPONDS to a risk_weights change)").

    PR-6 is S=3. Lowering the upper threshold ``t2`` 5->3 makes S>=t2 -> Tier 2 with the
    SAME fields — a second, independent proof the output tracks the weights.
    """
    candidate = dataclasses.replace(SEED_RISK_WEIGHTS, t2=3)
    assert compute_tier(_row_fields("PR-6"), candidate)[0] == 2


# ── v2.1 amendment: deep_T2 selector (S>=7 OR A>=3) ───────────────────────────


def test_deep_t2_true_when_s_at_least_7_even_with_no_anchors() -> None:
    """from: §6 (deep_T2 emitted True for S>=7) + the breakdown formula deep_t2 = (S>=7) or (A>=3).

    A pipeline + large-footprint + correction scope with NO anchors scores S=8, A=0 -> deep_t2
    True is driven by S alone (the anchor leg is dead here), and the tier is 2.
    """
    fields = TierFields(
        change_class=("pipeline",),
        imports_touched_count=20,
        precedent_surprise="correction",
        applicable_anchors_count=0,
    )
    tier, breakdown = compute_tier(fields, SEED_RISK_WEIGHTS)
    assert breakdown.s == 8
    assert breakdown.a == 0
    assert breakdown.deep_t2 is True
    assert tier == 2


def test_deep_t2_true_when_a_at_least_3_even_with_low_s() -> None:
    """from: §6 (deep_T2 emitted True for A>=3) + deep_t2 = (S>=7) or (A>=3).

    A docs scope with 3 anchors scores S=0 but A=3 -> deep_t2 True is driven by the anchor
    depth alone, and the immutable floor pins the tier at 2.
    """
    fields = TierFields(
        change_class=("docs",),
        imports_touched_count=0,
        precedent_surprise="clean",
        applicable_anchors_count=3,
    )
    tier, breakdown = compute_tier(fields, SEED_RISK_WEIGHTS)
    assert breakdown.s == 0
    assert breakdown.a == 3
    assert breakdown.deep_t2 is True
    assert tier == 2


def test_deep_t2_false_when_neither_s7_nor_a3() -> None:
    """from: §6 (deep_T2 is the S>=7 OR A>=3 selector — the negative case).

    PR-6 (S=3, A=0) clears neither leg -> deep_t2 False, distinguishing standard from deep T2.
    """
    _tier, breakdown = compute_tier(_row_fields("PR-6"), SEED_RISK_WEIGHTS)
    assert breakdown.deep_t2 is False


# ── v2.1 amendment: the dispatch-time conservative +1 bump ────────────────────


def test_open_questions_bump_lifts_base_tier_1_to_final_2() -> None:
    """from: §6 ("the dispatch-time bump applies (base tier 1 -> final 2) when has_open_questions").

    A PR-12-shaped scope is base Tier 1; ``has_open_questions=True`` applies the conservative
    bump ``min(2, base+1)`` -> final Tier 2.
    """
    fields = TierFields(
        change_class=("cli", "tests"),
        imports_touched_count=1,
        precedent_surprise="clean",
        applicable_anchors_count=0,
        has_open_questions=True,
    )
    assert compute_tier(fields, SEED_RISK_WEIGHTS)[0] == 2


def test_human_bump_lifts_base_tier_1_to_final_2() -> None:
    """from: §6 ("... OR human_bump").

    The same base-Tier-1 scope with ``human_bump=True`` (the operator-forced conservative bump)
    also reaches final Tier 2.
    """
    fields = TierFields(
        change_class=("cli", "tests"),
        imports_touched_count=1,
        precedent_surprise="clean",
        applicable_anchors_count=0,
        human_bump=True,
    )
    assert compute_tier(fields, SEED_RISK_WEIGHTS)[0] == 2


def test_bump_is_idempotent_on_a_tier_2_already_scope() -> None:
    """from: §6 ("a tier-2-already scope is UNCHANGED by the bump (min(2, .))").

    A PR-5a-shaped (floored Tier 2) scope carrying open questions stays Tier 2 — the bump is
    ``min(2, 2+1) == 2``, never Tier 3.
    """
    fields = TierFields(
        change_class=("pipeline",),
        imports_touched_count=10,
        precedent_surprise="correction",
        applicable_anchors_count=2,
        has_open_questions=True,
    )
    assert compute_tier(fields, SEED_RISK_WEIGHTS)[0] == 2


# ── BLOCKER A backstop: a directly-constructed out-of-vocab label fails loud, never under-tiers ──


def test_compute_tier_raises_on_out_of_vocab_label_under_direct_construction() -> None:
    """from: Stage-3 BLOCKER A repro (compute_tier on a bad-label TierFields must not score tier 0).

    The confirmed repro: ``compute_tier(TierFields(change_class=('DDL',), ...))`` previously scored
    C=0, floor=0 -> tier 0 (a structural change silently under-tiered). ``_c_score`` now raises on
    a label absent from the c_map — the direct-construction backstop to the from_json vocab guard.
    (``change_class`` is typed ``tuple[ChangeClass, ...]``, so this is also a mypy error in src; the
    test is not type-checked and exercises the runtime guard.)
    """
    bad = TierFields(
        change_class=("DDL",),
        imports_touched_count=0,
        precedent_surprise="clean",
        applicable_anchors_count=0,
    )
    with pytest.raises(ValueError, match=r"DDL|unknown change_class"):
        compute_tier(bad, SEED_RISK_WEIGHTS)

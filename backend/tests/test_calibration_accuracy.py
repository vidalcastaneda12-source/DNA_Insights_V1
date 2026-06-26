"""Per-knob accuracy + tier-in-hindsight — the calibration math the ratchet reduces.

from: §5 test #8 (test_calibration_accuracy.py) + §6:
  * ``tier_in_hindsight`` over fixtures with explicit hindsight (blocked verdict / materialized
    surprise / needed_deep / anchor-moved / review-blocker -> >= T2; >=2 revise-or-fix cycles ->
    >= T1; clean -> predicted.tier);
  * the per-knob systematic-error tally SIGN (+ under-tiered, - over-tiered);
  * precision/recall is report-only (returns numbers, drives no knob).

Every hindsight expectation comes from the FROZEN ``tier_in_hindsight`` spec (built only from
human-confirmed ``actual`` facts, never a self-grade). RED until the accuracy / ratchet bodies
land. test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import _calibration_ledger as cl
from genome.calibration.accuracy import (
    _BLOCKING_VERDICTS,
    per_knob_tally,
    premortem_precision_recall,
    tier_in_hindsight,
)
from genome.calibration.model import (
    GATE_VERDICT_VOCAB,
    SEED_RISK_WEIGHTS,
    ActualBlock,
    OutcomeRecord,
    PredictedBlock,
    TierBreakdown,
)
from genome.calibration.ratchet import propose_ratchet


def _outcome(predicted_tier: int, actual: ActualBlock) -> OutcomeRecord:
    """An outcome with an explicit predicted tier + merge-time actual (breakdown unused)."""
    return cl.outcome("PR-H", predicted_tier, cl.breakdown(), actual)


def _premortem_outcome(
    predicted_surprises: tuple[str, ...],
    materialized: tuple[str, ...],
    missed: tuple[str, ...],
) -> OutcomeRecord:
    """An outcome carrying explicit pre-mortem predicted vs materialized/missed surprises."""
    return OutcomeRecord(
        scope_id="PR-PM",
        merged_sha="sha-pm",
        date="2026-06-25",
        risk_weights_version="rw-1",
        predicted=PredictedBlock(
            tier=1,
            breakdown=TierBreakdown(c=1, b=0, p=0, a=0, s=1, floor=0, deep_t2=False),
            premortem_surprises=predicted_surprises,
        ),
        actual=ActualBlock(
            gate_verdict="pass",
            surprises_materialized=materialized,
            surprises_missed=missed,
        ),
    )


# ── tier_in_hindsight: the >= T2 triggers ─────────────────────────────────────


def test_hindsight_at_least_t2_on_a_blocked_gate_verdict() -> None:
    """from: §6 (blocked verdict -> >= T2).

    A blocked merge is hard evidence the scope needed more scrutiny than its low predicted tier.
    """
    assert tier_in_hindsight(_outcome(0, ActualBlock(gate_verdict="blocked"))) >= 2


def test_hindsight_at_least_t2_when_a_surprise_materialized() -> None:
    """from: §6 (materialized surprise -> >= T2)."""
    actual = ActualBlock(gate_verdict="pass", surprises_materialized=("anchor drift",))
    assert tier_in_hindsight(_outcome(0, actual)) >= 2


def test_hindsight_at_least_t2_when_deep_review_was_needed() -> None:
    """from: §6 (needed_deep -> >= T2)."""
    assert tier_in_hindsight(_outcome(0, ActualBlock(gate_verdict="pass", needed_deep=True))) >= 2


def test_hindsight_at_least_t2_when_an_anchor_moved_unexpectedly() -> None:
    """from: §6 (>= T2 triggers incl. an unexpected anchor move)."""
    actual = ActualBlock(gate_verdict="pass", anchors_moved_unexpected=("gnomad_matches",))
    assert tier_in_hindsight(_outcome(0, actual)) >= 2


def test_hindsight_at_least_t2_on_a_review_blocker() -> None:
    """from: §6 (>= T2 triggers incl. a materialized review blocker)."""
    actual = ActualBlock(gate_verdict="pass", review_blockers=("schema mismatch",))
    assert tier_in_hindsight(_outcome(0, actual)) >= 2


# ── tier_in_hindsight: the cycle branch + the clean branch ────────────────────


def test_hindsight_is_tier_1_on_two_revise_cycles_otherwise_clean() -> None:
    """from: §6 (>=2 revise-or-fix cycles -> >= T1).

    A predicted-Tier-0 scope that took 2 revise cycles but otherwise passed clean lands at Tier 1
    in hindsight — friction without a T2 trigger reveals a mild under-tier, never escalates to T2.
    """
    assert tier_in_hindsight(_outcome(0, ActualBlock(gate_verdict="pass", revise_cycles=2))) == 1


def test_hindsight_confirms_the_predicted_tier_on_a_clean_run() -> None:
    """from: §6 (clean -> predicted.tier).

    A clean pass with no surprises / blockers / cycles confirms the call: hindsight equals the
    originally predicted tier, at each tier level (no spurious error signal on a clean run).
    """
    for predicted in (0, 1, 2):
        assert tier_in_hindsight(_outcome(predicted, ActualBlock(gate_verdict="pass"))) == predicted


# ── per_knob_tally: the systematic-error SIGN ─────────────────────────────────


def test_per_knob_tally_is_positive_on_a_systematic_under_tiering_ledger() -> None:
    """from: §6 (per-knob tally sign: + = systematically under-tiered).

    A ledger of Tier-1 predictions the gate blocked (hindsight Tier 2) is systematic
    under-tiering: the tally carries a positive (under-tier) signal.
    """
    under = cl.ledger(8, 1, cl.breakdown(c=1), cl.actual_blocked())
    tally = per_knob_tally(under, SEED_RISK_WEIGHTS)
    assert any(v > 0 for v in tally.values())
    assert sum(tally.values()) > 0


def test_per_knob_tally_is_negative_on_a_systematic_over_tiering_ledger() -> None:
    """from: §6 (per-knob tally sign: - = systematically over-tiered).

    A ledger of unfloored Tier-2 predictions whose actuals show only mild friction (hindsight
    Tier 1) is systematic over-tiering: the tally carries a negative (over-tier) signal.
    """
    over = cl.ledger(8, 2, cl.breakdown(c=3, b=1, p=1), cl.actual_mild_friction())
    tally = per_knob_tally(over, SEED_RISK_WEIGHTS)
    assert any(v < 0 for v in tally.values())
    assert sum(tally.values()) < 0


# ── premortem precision/recall: report-only ───────────────────────────────────


def test_premortem_precision_recall_rates_a_perfect_and_a_crying_wolf_ledger() -> None:
    """from: §6 (precision/recall returns numbers).

    A pre-mortem that predicted exactly what happened scores (1.0, 1.0); one that predicted two
    surprises, none of which materialized, scores precision 0.0 (pure crying-wolf) with a zero
    recall denominator yielding 0.0.
    """
    perfect = [_premortem_outcome(("X",), ("X",), ())]
    assert premortem_precision_recall(perfect) == (1.0, 1.0)

    crying_wolf = [_premortem_outcome(("X", "Y"), (), ())]
    precision, recall = premortem_precision_recall(crying_wolf)
    assert precision == 0.0
    assert recall == 0.0


def test_premortem_accuracy_does_not_drive_the_ratchet() -> None:
    """from: §6 (precision/recall is report-only — drives no knob).

    Two under-tiering ledgers identical in their tier facts but differing ONLY in pre-mortem
    surprise accuracy yield the SAME ratchet disposition — the pre-mortem rates inform the report,
    never the auto-tune gate.
    """
    base_bd = cl.breakdown(c=1)
    good_pm = [
        OutcomeRecord(
            scope_id=f"PR-G-{i}",
            merged_sha=f"sha-g-{i}",
            date="2026-06-25",
            risk_weights_version="rw-1",
            predicted=PredictedBlock(tier=1, breakdown=base_bd, premortem_surprises=("X",)),
            actual=ActualBlock(gate_verdict="blocked", surprises_materialized=("X",)),
        )
        for i in range(12)
    ]
    bad_pm = [
        OutcomeRecord(
            scope_id=f"PR-B-{i}",
            merged_sha=f"sha-b-{i}",
            date="2026-06-25",
            risk_weights_version="rw-1",
            predicted=PredictedBlock(tier=1, breakdown=base_bd, premortem_surprises=("Q", "R")),
            actual=ActualBlock(gate_verdict="blocked", surprises_missed=("Z",)),
        )
        for i in range(12)
    ]
    good = propose_ratchet(good_pm, cl.enabled(), merges_since_last=5)
    bad = propose_ratchet(bad_pm, cl.enabled(), merges_since_last=5)
    assert good.disposition is bad.disposition


# ── WARN B: the blocking verdicts are a subset of the closed gate-verdict vocab ──


def test_blocking_verdicts_are_a_subset_of_the_gate_verdict_vocab() -> None:
    """from: Stage-3 WARN B (``_BLOCKING_VERDICTS`` ⊆ ``GATE_VERDICT_VOCAB``).

    The hindsight ladder's Tier-2 triggers must be drawn from the same closed vocab
    ``ActualBlock.from_json`` validates against — otherwise a "blocking" verdict the guard rejects
    could never reach the ladder, or a typo could slip past both.
    """
    assert _BLOCKING_VERDICTS <= GATE_VERDICT_VOCAB

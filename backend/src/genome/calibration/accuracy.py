"""Per-knob accuracy + tier-in-hindsight computation (``finding-040``; plan §4 T2).

The calibration math the slow-loop ratchet and the ``/calibrate report`` consume:

* :func:`tier_in_hindsight` derives, from an :class:`~genome.calibration.model.OutcomeRecord`'s
  **human-confirmed actual** facts, the tier the scope *should* have carried — the ground truth
  the per-knob systematic-error tally compares the prediction against. This is the PROVISIONAL
  default (plan ESC-2): it is one of several teeth (coverage + delta-direction + the deterministic
  loop-closure test add the others), and the calibrator ships REPORT-ONLY until VSC-User confirms
  it.
* :func:`per_knob_tally` attributes systematic under/over-tiering to each tunable knob over the
  outcome ledger — the signal the ratchet's hysteresis gate accumulates.
* :func:`premortem_precision_recall` computes the pre-mortem's crying-wolf vs. blind-spot rates
  (report-only).

**No** :mod:`genome.db` and **no** :mod:`genome.config` import — pure computation over the
ledger, runnable on a fresh checkout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from genome.calibration.model import OutcomeRecord, RiskWeights, TierBreakdown

#: Gate verdicts that, on their own, prove the scope needed Tier-2 scrutiny in hindsight.
_BLOCKING_VERDICTS: frozenset[str] = frozenset({"blocked", "escalate"})

#: At or above this many combined revise + fix-first cycles, an otherwise-clean scope reads as a
#: **hard** Tier 1 in hindsight (a mild under/over-tier signal, never an escalation to Tier 2).
_HINDSIGHT_CYCLE_THRESHOLD: int = 2


def _label_for_value(mapping: Mapping[str, int], value: int) -> str | None:
    """The first knob label in ``mapping`` whose seed weight equals ``value`` (insertion order)."""
    for key, val in mapping.items():
        if val == value:
            return key
    return None


def dominant_knob(breakdown: TierBreakdown, weights: RiskWeights) -> str | None:
    """Attribute a breakdown to the single tunable knob that dominated its score.

    The :class:`~genome.calibration.model.OutcomeRecord` carries no raw ``change_class`` (the
    ledger is PHI-slim) — only the :class:`~genome.calibration.model.TierBreakdown` sub-scores. So
    a knob is identified by mapping the **dominant** nonzero sub-score back to the knob whose seed
    weight equals it: ``C → c_map``, ``B → b_buckets``, ``P → p_levels``. The dominant component is
    the one with the largest value; a value tie is broken in ``B → C → P`` order so an ambiguous
    ``C == B`` case attributes to the (typically uncovered, hence human-gated) band knob rather
    than auto-committing a covered ``c_map`` change. Returns ``None`` for an all-zero breakdown.
    """
    candidates: list[tuple[int, int, str]] = []
    b_label = _label_for_value(weights.b_buckets, breakdown.b)
    if b_label is not None and breakdown.b > 0:
        candidates.append((0, breakdown.b, f"b_buckets.{b_label}"))
    c_label = _label_for_value(weights.c_map, breakdown.c)
    if c_label is not None and breakdown.c > 0:
        candidates.append((1, breakdown.c, f"c_map.{c_label}"))
    p_label = _label_for_value(weights.p_levels, breakdown.p)
    if p_label is not None and breakdown.p > 0:
        candidates.append((2, breakdown.p, f"p_levels.{p_label}"))
    if not candidates:
        return None
    candidates.sort(key=lambda triple: (-triple[1], triple[0]))
    return candidates[0][2]


def tier_in_hindsight(outcome: OutcomeRecord) -> int:
    """Derive the tier the scope *should* have carried, from its human-confirmed actual facts.

    The PROVISIONAL default (plan §4 T2 / ESC-2), built only from ``outcome.actual`` ground truth
    (never a self-grade):

    * ``>= Tier 2`` if the gate verdict was ``blocked`` / ``escalate``, **or** ≥1 surprise
      materialized, **or** ≥1 anchor moved unexpectedly, **or** deep review was needed, **or**
      there was ≥1 review blocker;
    * else ``>= Tier 1`` if it took ≥2 revise/fix cycles;
    * else the originally predicted tier (``outcome.predicted.tier``) — a clean run confirms the
      call.

    Returns the hindsight tier in ``{0, 1, 2}``.
    """
    actual = outcome.actual
    if (
        actual.gate_verdict in _BLOCKING_VERDICTS
        or actual.review_blockers
        or actual.surprises_materialized
        or actual.anchors_moved_unexpected
        or actual.needed_deep
    ):
        return 2
    if (actual.revise_cycles + actual.fix_first_cycles) >= _HINDSIGHT_CYCLE_THRESHOLD:
        return 1
    return outcome.predicted.tier


def per_knob_tally(
    outcomes: Sequence[OutcomeRecord],
    weights: RiskWeights,
) -> Mapping[str, int]:
    """Tally systematic tier error per tunable knob over the outcome ledger.

    For each outcome, compares the predicted tier to :func:`tier_in_hindsight` and attributes the
    signed error (``+`` = systematically *under*-tiered, ``-`` = *over*-tiered) to the knob(s) that
    drove the scope's sub-scores under ``weights``. The keys are the
    :data:`~genome.calibration.model.KNOB_COVERAGE` knob ids; the magnitude is the count of
    same-direction misses the ratchet's hysteresis gate (``HYST >= 3``) accumulates.
    """
    tally: dict[str, int] = {}
    for outcome in outcomes:
        error = tier_in_hindsight(outcome) - outcome.predicted.tier
        if error == 0:
            continue
        knob = dominant_knob(outcome.predicted.breakdown, weights)
        if knob is None:
            continue
        tally[knob] = tally.get(knob, 0) + (1 if error > 0 else -1)
    return tally


def premortem_precision_recall(outcomes: Sequence[OutcomeRecord]) -> tuple[float, float]:
    """Compute the pre-mortem's ``(precision, recall)`` over the outcome ledger (report-only).

    Precision = fraction of predicted surprises that materialized (crying-wolf is low precision);
    recall = fraction of materialized surprises that were predicted (blind spots are low recall).
    Returns ``(precision, recall)``; a zero denominator yields ``0.0`` for that term. Advisory —
    it informs the report, never the auto-tune gate.
    """
    predicted_total = 0
    hit_total = 0
    happened_total = 0
    for outcome in outcomes:
        predicted = set(outcome.predicted.premortem_surprises)
        materialized = set(outcome.actual.surprises_materialized)
        missed = set(outcome.actual.surprises_missed)
        predicted_total += len(predicted)
        hit_total += len(predicted & materialized)
        happened_total += len(materialized | missed)
    precision = hit_total / predicted_total if predicted_total else 0.0
    recall = hit_total / happened_total if happened_total else 0.0
    return precision, recall

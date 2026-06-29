"""The asymmetric L3 auto-tuning ratchet — the slow-loop decision core (``finding-040``; T3).

:func:`propose_ratchet` reduces the outcome ledger + the live weights into a single
:class:`~genome.calibration.model.RatchetDecision`. The asymmetry (plan §2 decisions 2/3):
*tightening* (more scrutiny) can auto-apply because under-tiering is the irreversible/expensive
error; *loosening* (less scrutiny) is always human-gated. Direction is decided by **tier delta
over the direction-witness ladder**, never the knob's numeric sign — a ``t1`` / ``t2`` raise is a
LOOSEN (plan §4 T3 FIX-2).

LAYERED fail-closed gates, each reducing to ``NO_OP`` / ``SUPPRESSED`` / ``PARK_FOR_APPROVAL``
before an ``AUTO_COMMIT`` is ever reachable:

* kill-switch (``not weights.auto_tuning_enabled``) → ``NO_OP``;
* thin data (``len(outcomes) < THIN_DATA_MIN_OUTCOMES``) → ``NO_OP``;
* cadence (``merges_since_last < CADENCE_MIN_MERGES``) → ``NO_OP``;
* hysteresis (a knob's systematic error has not cleared ``HYSTERESIS_MIN_RUNS`` in one
  direction; reset after an apply) → ``NO_OP``;
* build a ±1-step candidate, classify its direction over the ladder:
  LOOSEN → ``PARK_FOR_APPROVAL``;
  TIGHTEN ∧ not back-test-clean → ``SUPPRESSED``;
  TIGHTEN ∧ clean ∧ knob has **no** unfloored coverage → ``PARK_FOR_APPROVAL`` (clean-by-vacuity);
  TIGHTEN ∧ clean ∧ covered → ``AUTO_COMMIT``.

INVARIANTS: a loosen NEVER auto-commits; an uncovered-knob tighten NEVER auto-commits;
``auto_applicable`` iff (TIGHTEN ∧ backtest_clean ∧ knob_covered).

**No** :mod:`genome.db` and **no** :mod:`genome.config` import — the decision core is pure and
stays runnable on a fresh checkout.
"""

from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING

from genome.calibration.accuracy import dominant_knob, per_knob_tally, tier_in_hindsight
from genome.calibration.backtest import run_backtest
from genome.calibration.model import (
    DIRECTION_WITNESS_LADDER,
    KNOB_COVERAGE,
    Direction,
    Disposition,
    RatchetDecision,
    compute_tier,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from genome.calibration.model import OutcomeRecord, RiskWeights

#: Thin-data lockout (plan OQ-4 ``K``): no auto-tune under this many outcomes (overfitting guard).
THIN_DATA_MIN_OUTCOMES: int = 10

#: Cadence gate (plan OQ-4 ``N``): the ratchet is a ``NO_OP`` until this many merges have
#: accumulated since the last pass.
CADENCE_MIN_MERGES: int = 5

#: Hysteresis (plan OQ-4 ``HYST``): a knob's systematic error must persist in the same direction
#: across at least this many outcomes before a change is proposed; the counter resets after apply.
HYSTERESIS_MIN_RUNS: int = 3


def _no_op(reason: str) -> RatchetDecision:
    """A fail-closed ``NO_OP`` verdict — no knob, no candidate, never auto-applicable."""
    return RatchetDecision(
        disposition=Disposition.NO_OP,
        knob=None,
        direction=None,
        candidate_weights=None,
        backtest_clean=True,
        knob_covered=False,
        cited_merged_shas=(),
        rationale=reason,
        auto_applicable=False,
    )


def _bump_version(version: str) -> str:
    """Increment a trailing integer in the weights version (``rw-1`` → ``rw-2``)."""
    match = re.match(r"^(.*?)(\d+)$", version)
    if match is None:
        return f"{version}-2"
    return f"{match.group(1)}{int(match.group(2)) + 1}"


def _build_candidate(weights: RiskWeights, knob: str, delta: int) -> RiskWeights:
    """A ±1-step candidate with ``knob`` adjusted by ``delta`` and the version bumped."""
    component, _, label = knob.partition(".")
    new_version = _bump_version(weights.weights_version)
    if component == "c_map":
        new_map = {**weights.c_map, label: weights.c_map[label] + delta}
        return dataclasses.replace(weights, c_map=new_map, weights_version=new_version)
    if component == "b_buckets":
        new_map = {**weights.b_buckets, label: weights.b_buckets[label] + delta}
        return dataclasses.replace(weights, b_buckets=new_map, weights_version=new_version)
    if component == "p_levels":
        new_map = {**weights.p_levels, label: weights.p_levels[label] + delta}
        return dataclasses.replace(weights, p_levels=new_map, weights_version=new_version)
    if component == "t1":
        return dataclasses.replace(weights, t1=weights.t1 + delta, weights_version=new_version)
    if component == "t2":
        return dataclasses.replace(weights, t2=weights.t2 + delta, weights_version=new_version)
    msg = f"unknown knob {knob!r}"
    raise ValueError(msg)


def classify_direction(base: RiskWeights, candidate: RiskWeights) -> Direction:
    """LOOSEN iff the candidate lowers ANY ladder probe's tier (never the knob's numeric sign).

    Public so the ``apply-parked`` CLI can re-run the direction check against the **current** live
    weights (the TOCTOU guard) before applying a previously-parked candidate.
    """
    for probe in DIRECTION_WITNESS_LADDER:
        if compute_tier(probe, candidate)[0] < compute_tier(probe, base)[0]:
            return Direction.LOOSEN
    return Direction.TIGHTEN


def _flatten_knobs(weights: RiskWeights) -> dict[str, int]:
    """Flatten the tunable additive maps + the two thresholds to one ``{knob_id: value}`` dict.

    The knob ids match :data:`~genome.calibration.model.KNOB_COVERAGE` /
    :attr:`~genome.calibration.model.RatchetDecision.knob` (``c_map.cli`` / ``b_buckets.small`` /
    ``t2`` …). ``weights_version`` / ``provenance`` / ``auto_tuning_enabled`` are deliberately
    excluded — they are not tunable score knobs.
    """
    flat: dict[str, int] = {f"c_map.{label}": value for label, value in weights.c_map.items()}
    flat.update({f"b_buckets.{label}": value for label, value in weights.b_buckets.items()})
    flat.update({f"p_levels.{label}": value for label, value in weights.p_levels.items()})
    flat["t1"] = weights.t1
    flat["t2"] = weights.t2
    return flat


def nontarget_knobs_unchanged(live: RiskWeights, candidate: RiskWeights, knob: str) -> bool:
    """``True`` iff ``candidate`` equals ``live`` on every tunable knob EXCEPT ``knob``.

    The lost-update guard for ``apply-parked`` (plan §4 T1 / finding-040 FIX-1). A parked candidate
    is a one-knob delta on the park-time live weights; if an intervening ``AUTO_COMMIT`` moved a
    **different** knob before approval, writing the stale park-time snapshot wholesale would
    silently revert it — and a tier-neutral revert slips both the back-test and direction
    re-checks. This compares the two weights on all tunable knobs other than the parked target
    ``knob`` and returns ``False`` the moment any of them diverged, the fail-closed signal for the
    CLI to refuse the apply (the human must re-run the ratchet against current live).
    """
    live_knobs = _flatten_knobs(live)
    candidate_knobs = _flatten_knobs(candidate)
    live_knobs.pop(knob, None)
    candidate_knobs.pop(knob, None)
    return live_knobs == candidate_knobs


def _disposition(direction: Direction, *, backtest_clean: bool, knob_covered: bool) -> Disposition:
    """The asymmetric reduction: loosen → PARK · dirty → SUPPRESSED · vacuity → PARK · else COMMIT.

    A back-test-clean, unfloored-covered TIGHTEN is the only auto-applicable disposition.
    """
    if direction is Direction.LOOSEN:
        return Disposition.PARK_FOR_APPROVAL
    if not backtest_clean:
        return Disposition.SUPPRESSED
    if not knob_covered:
        return Disposition.PARK_FOR_APPROVAL
    return Disposition.AUTO_COMMIT


def propose_ratchet(
    outcomes: Sequence[OutcomeRecord],
    weights: RiskWeights,
    merges_since_last: int,
) -> RatchetDecision:
    """Propose one ratchet decision over the outcome ledger (plan §4 T3).

    Applies the layered fail-closed gates (kill-switch → thin-data → cadence → hysteresis), then —
    if a knob clears them — builds a ±1-step candidate, classifies its direction over
    :data:`~genome.calibration.model.DIRECTION_WITNESS_LADDER`, runs the back-test
    (:func:`genome.calibration.backtest.run_backtest`), checks unfloored coverage
    (:data:`~genome.calibration.model.KNOB_COVERAGE`), and returns the
    :class:`~genome.calibration.model.RatchetDecision`. Pure: it never writes weights and never
    runs git — applying an ``AUTO_COMMIT`` is the CLI/skill's job, gated on this decision.
    """
    if not weights.auto_tuning_enabled:
        return _no_op("auto-tuning disabled (kill switch off)")
    if len(outcomes) < THIN_DATA_MIN_OUTCOMES:
        return _no_op(f"insufficient data ({len(outcomes)} < {THIN_DATA_MIN_OUTCOMES} outcomes)")
    if merges_since_last < CADENCE_MIN_MERGES:
        return _no_op(f"cadence not met ({merges_since_last} < {CADENCE_MIN_MERGES} merges)")

    tally = per_knob_tally(outcomes, weights)
    if not tally:
        return _no_op("no systematic tier error in the ledger")
    knob, net = max(tally.items(), key=lambda kv: (abs(kv[1]), kv[0]))
    if abs(net) < HYSTERESIS_MIN_RUNS:
        return _no_op(f"hysteresis not met (|{net}| < {HYSTERESIS_MIN_RUNS} on {knob})")

    delta = 1 if net > 0 else -1
    sign = delta
    candidate = _build_candidate(weights, knob, delta)
    direction = classify_direction(weights, candidate)
    result = run_backtest(candidate)
    knob_covered = bool(KNOB_COVERAGE.get(knob, ()))
    disposition = _disposition(direction, backtest_clean=result.clean, knob_covered=knob_covered)

    cited = tuple(
        outcome.merged_sha
        for outcome in outcomes
        if dominant_knob(outcome.predicted.breakdown, weights) == knob
        and (tier_in_hindsight(outcome) - outcome.predicted.tier > 0) == (sign > 0)
        and tier_in_hindsight(outcome) != outcome.predicted.tier
    )
    err_word = "under" if net > 0 else "over"
    step_word = "+1" if delta > 0 else "-1"
    bt_word = "clean" if result.clean else "dirty"
    cov_word = "covered" if knob_covered else "uncovered (clean-by-vacuity)"
    rationale = (
        f"systematic {err_word}-tiering on {knob} across {abs(net)} outcome(s); "
        f"{step_word} {direction.value}, back-test {bt_word}, {cov_word}"
    )
    final_candidate = dataclasses.replace(
        candidate,
        provenance={
            "source": "ratchet",
            "rationale": rationale,
            "cited_outcomes": list(cited),
            "parent_version": weights.weights_version,
        },
    )
    return RatchetDecision(
        disposition=disposition,
        knob=knob,
        direction=direction,
        candidate_weights=final_candidate,
        backtest_clean=result.clean,
        knob_covered=knob_covered,
        cited_merged_shas=cited,
        rationale=rationale,
        auto_applicable=disposition is Disposition.AUTO_COMMIT,
    )

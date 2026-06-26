"""Render the ``/calibrate`` report + the ratchet would-commit/would-draft diff (``finding-040``).

:func:`format_calibration_report` turns the outcome ledger + live weights + the proposed ratchet
decision into the plain-text block the operator reads on demand — per-knob accuracy, the
systematic-error tally, each knob's COVERAGE status, and the proposed disposition with its reason
(PARK-by-vacuity vs PARK-by-loosen made explicit). :func:`format_ratchet_decision` renders the
would-commit (TIGHTEN/AUTO_COMMIT) or would-draft (LOOSEN/PARK) diff a ``--dry-run`` shows.

**No** :mod:`genome.db` and **no** :mod:`genome.config` import. **No anchor magnitudes hard-coded
in this module's source**: every number in the output originates from the ledger / decision at
runtime. Both render functions are implemented; the :data:`INSUFFICIENT_DATA_SENTINEL` literal is
the frozen contract the plan-blind report tests key on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genome.calibration.accuracy import per_knob_tally
from genome.calibration.model import KNOB_COVERAGE
from genome.calibration.ratchet import THIN_DATA_MIN_OUTCOMES

if TYPE_CHECKING:
    from collections.abc import Sequence

    from genome.calibration.model import OutcomeRecord, RatchetDecision, RiskWeights

#: Emitted in place of the proposed-change section when the ledger is below the thin-data
#: threshold (``< THIN_DATA_MIN_OUTCOMES``). A literal sentinel, never a number — the report smoke
#: and doc-consistency tests key on it.
INSUFFICIENT_DATA_SENTINEL: str = (
    "insufficient data — the ratchet is a no-op until more outcomes accumulate"
)


def format_calibration_report(
    outcomes: Sequence[OutcomeRecord],
    weights: RiskWeights,
    decision: RatchetDecision,
) -> str:
    """Render the ``/calibrate report`` block the operator reviews (plan §4 T6).

    Shows per-knob accuracy (predicted vs tier-in-hindsight), the systematic-error tally, each
    tunable knob's COVERAGE status (covered vs PARK-ONLY by vacuity), and the proposed disposition
    with its explicit reason. Below the thin-data threshold it leads with
    :data:`INSUFFICIENT_DATA_SENTINEL`. Contains no hard-coded anchor magnitude — every number
    comes from ``outcomes`` / ``decision`` at call time.
    """
    lines = [
        "# /calibrate report",
        f"outcomes in ledger: {len(outcomes)}",
        f"live weights_version: {weights.weights_version}",
    ]
    if len(outcomes) < THIN_DATA_MIN_OUTCOMES:
        lines.append(INSUFFICIENT_DATA_SENTINEL)
        return "\n".join(lines)

    tally = per_knob_tally(outcomes, weights)
    lines.append("")
    lines.append("per-knob systematic tier error (+ under-tiered · - over-tiered):")
    if tally:
        lines.extend(f"  {knob}: {value:+d}" for knob, value in sorted(tally.items()))
    else:
        lines.append("  (none — every prediction confirmed in hindsight)")

    lines.append("")
    lines.append("per-knob coverage status:")
    for knob, covering in KNOB_COVERAGE.items():
        status = (
            f"covered by {', '.join(covering)}"
            if covering
            else "PARK-ONLY (no unfloored coverage — clean-by-vacuity)"
        )
        lines.append(f"  {knob}: {status}")

    lines.append("")
    lines.append("proposed disposition:")
    lines.append(format_ratchet_decision(decision))
    return "\n".join(lines)


def format_ratchet_decision(decision: RatchetDecision) -> str:
    """Render the would-commit / would-draft diff for one ratchet decision (plan §4 T6).

    A TIGHTEN/AUTO_COMMIT shows the would-commit diff (knob, ±1 step, new ``weights_version``,
    back-test clean, cited SHAs); a LOOSEN or clean-by-vacuity tighten shows the would-draft (PARK)
    block with the human-approval reason. A ``NO_OP`` renders the gate that stopped it.
    """
    lines = [f"disposition: {decision.disposition.value.upper()}"]
    if decision.knob is not None:
        lines.append(f"knob: {decision.knob}")
    if decision.direction is not None:
        lines.append(f"direction: {decision.direction.value}")
    if decision.candidate_weights is not None:
        lines.append(f"would bump weights_version → {decision.candidate_weights.weights_version}")
    lines.append(
        f"back-test clean: {decision.backtest_clean} · knob covered: {decision.knob_covered}"
    )
    if decision.cited_merged_shas:
        lines.append("cited merged SHAs: " + ", ".join(decision.cited_merged_shas))
    lines.append(f"rationale: {decision.rationale}")
    return "\n".join(lines)

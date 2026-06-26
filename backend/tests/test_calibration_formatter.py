"""Operator-facing render — the /calibrate report + the would-commit/would-draft diff.

from: §5 test #18 (test_calibration_formatter.py) + §6:
  * ``format_calibration_report`` includes per-knob accuracy + coverage status + the proposed
    disposition with its PARK-reason;
  * a thin-data report contains ``INSUFFICIENT_DATA_SENTINEL``;
  * ``format_ratchet_decision`` renders the knob delta + the cited SHAs.

The decisions are constructed directly (frozen dataclasses) so the rendered tokens (knob name,
cited SHAs, new weights_version, disposition) are INPUTS that must surface in the output. The
sentinel is a frozen literal. RED until the formatter bodies land. test->spec provenance is
stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import dataclasses

import _calibration_ledger as cl
from genome.calibration.formatter import (
    INSUFFICIENT_DATA_SENTINEL,
    format_calibration_report,
    format_ratchet_decision,
)
from genome.calibration.model import (
    SEED_RISK_WEIGHTS,
    Direction,
    Disposition,
    RatchetDecision,
)


def _park_decision() -> RatchetDecision:
    """A parked clean-by-vacuity tighten — exercises the PARK-reason rendering."""
    return RatchetDecision(
        disposition=Disposition.PARK_FOR_APPROVAL,
        knob="c_map.pipeline",
        direction=Direction.TIGHTEN,
        candidate_weights=dataclasses.replace(SEED_RISK_WEIGHTS, weights_version="rw-2"),
        backtest_clean=True,
        knob_covered=False,
        cited_merged_shas=("deadbeef", "cafef00d"),
        rationale="clean-by-vacuity: c_map.pipeline has no unfloored coverage",
        auto_applicable=False,
    )


def _no_op_decision() -> RatchetDecision:
    """A NO_OP verdict (no knob) — what a thin-data report carries."""
    return RatchetDecision(
        disposition=Disposition.NO_OP,
        knob=None,
        direction=None,
        candidate_weights=None,
        backtest_clean=True,
        knob_covered=False,
        cited_merged_shas=(),
        rationale="thin data",
        auto_applicable=False,
    )


def test_thin_data_report_leads_with_the_insufficient_data_sentinel() -> None:
    """from: §6 (a thin-data report contains INSUFFICIENT_DATA_SENTINEL).

    Below the thin-data threshold the report does not invent a proposed change — it shows the
    literal insufficient-data sentinel so the operator knows the ratchet is a no-op pending data.
    """
    thin = cl.ledger(3, 1, cl.breakdown(c=1), cl.actual_blocked())
    report = format_calibration_report(thin, SEED_RISK_WEIGHTS, _no_op_decision())
    assert INSUFFICIENT_DATA_SENTINEL in report


def test_report_shows_per_knob_coverage_and_the_disposition_reason() -> None:
    """from: §6 (per-knob accuracy + coverage status + the proposed disposition with its reason).

    A non-thin report surfaces the targeted knob, each knob's COVERAGE status, and the proposed
    disposition (here a PARK with its clean-by-vacuity reason made explicit).
    """
    outcomes = cl.ledger(12, 1, cl.breakdown(c=3), cl.actual_blocked())
    report = format_calibration_report(outcomes, SEED_RISK_WEIGHTS, _park_decision())
    assert "c_map.pipeline" in report
    assert "cover" in report.lower()
    assert "park" in report.lower()


def test_format_ratchet_decision_renders_the_knob_delta_and_cited_shas() -> None:
    """from: §6 (format_ratchet_decision renders the knob delta + cited SHAs).

    The would-commit / would-draft diff names the targeted knob, the bumped weights_version (the
    delta), and every cited merged SHA — the audit-grade provenance the operator reviews.
    """
    rendered = format_ratchet_decision(_park_decision())
    assert "c_map.pipeline" in rendered
    assert "rw-2" in rendered
    assert "deadbeef" in rendered
    assert "cafef00d" in rendered

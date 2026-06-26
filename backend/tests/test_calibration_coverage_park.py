"""Clean-by-vacuity is distinguished from clean-with-coverage (FIX-1).

from: §5 test #4 (test_calibration_coverage_park.py) + §6:
  * for EACH of the 9 PARK-ONLY knobs, a +1-step tighten — even when back-test-clean — yields
    ``Disposition.PARK_FOR_APPROVAL`` (NOT AUTO_COMMIT): a back-test that is clean only because
    NO unfloored row constrains the knob cannot vouch for the change, so it is human-gated.

Predicted-surprise guard (COVERAGE-MAP COMPLETENESS): a knob mis-marked *covered* would let a
vacuous tighten AUTO_COMMIT. The robust completeness guard is the structural assertion that all
nine knobs carry an EMPTY ``KNOB_COVERAGE`` tuple (GREEN from freeze) — flip any one to non-empty
and it fails here. The behavioral confirmation drives ``propose_ratchet`` on the knobs whose
dominant sub-score is UNIQUELY identifiable from the breakdown the ledger carries (c_map.pipeline
C=3, b_buckets.moderate B=2, b_buckets.large B=3, p_levels.correction P=2); the C=2 trio
(annotation-loader / analysis / insights) and the floored-only schema/ddl are covered by the
structural guard, since an OutcomeRecord's breakdown can't distinguish a C=2 park-only label from
the covered ``data-backfill``, and a floored scope can never under-tier.

RED until ``propose_ratchet`` lands; the coverage-completeness assertion is GREEN from freeze.
test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import pytest

import _calibration_ledger as cl
from genome.calibration.model import KNOB_COVERAGE, Disposition
from genome.calibration.ratchet import propose_ratchet

#: The nine PARK-ONLY knobs — no unfloored back-test row exercises them, so a tighten of any is
#: clean only by vacuity (transcribed from the FROZEN KNOB_COVERAGE empty-tuple entries).
_PARK_ONLY_KNOBS = (
    "c_map.annotation-loader",
    "c_map.analysis",
    "c_map.insights",
    "c_map.pipeline",
    "c_map.schema",
    "c_map.ddl",
    "b_buckets.moderate",
    "b_buckets.large",
    "p_levels.correction",
)

#: The subset whose dominant sub-score is uniquely identifiable from an OutcomeRecord breakdown,
#: so an under-tiering ledger provably targets THAT knob: (knob, breakdown-kwargs for a clean,
#: unfloored, Tier-1 scope whose only nonzero sub-score is the knob's).
_BEHAVIORALLY_TARGETABLE = (
    ("c_map.pipeline", {"c": 3}),
    ("b_buckets.moderate", {"b": 2}),
    ("b_buckets.large", {"b": 3}),
    ("p_levels.correction", {"p": 2}),
)


def test_all_nine_park_only_knobs_have_empty_coverage() -> None:
    """from: §6 (the 9 PARK-ONLY knobs) — GREEN completeness guard.

    Every park-only knob maps to an EMPTY coverage tuple in KNOB_COVERAGE. This is the structural
    source of "clean-by-vacuity": mis-marking any one covered (a non-empty tuple) would let a
    vacuous tighten reach AUTO_COMMIT, and this assertion catches exactly that.
    """
    for knob in _PARK_ONLY_KNOBS:
        assert knob in KNOB_COVERAGE, f"{knob} missing from KNOB_COVERAGE"
        assert KNOB_COVERAGE[knob] == (), f"{knob} is not park-only (coverage not empty)"


@pytest.mark.parametrize(("knob", "bd_kwargs"), _BEHAVIORALLY_TARGETABLE)
def test_clean_by_vacuity_tighten_parks_never_auto_commits(
    knob: str, bd_kwargs: dict[str, int]
) -> None:
    """from: §6 (a +1 tighten of a PARK-ONLY knob, even back-test-clean, PARKS not AUTO_COMMITs).

    An under-tiering ledger whose only nonzero sub-score is this park-only knob's drives the
    ratchet to a +1 tighten that flips no back-test row (the constraining rows are floored). With
    no unfloored coverage the verdict is PARK_FOR_APPROVAL, never AUTO_COMMIT — clean-by-vacuity
    is human-gated.
    """
    under = cl.ledger(12, 1, cl.breakdown(**bd_kwargs), cl.actual_blocked(), prefix=knob)
    decision = propose_ratchet(under, cl.enabled(), merges_since_last=5)
    assert decision.disposition is not Disposition.AUTO_COMMIT, knob
    assert decision.disposition is Disposition.PARK_FOR_APPROVAL, knob
    assert decision.auto_applicable is False, knob

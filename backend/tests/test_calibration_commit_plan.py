"""Git-write CommitPlan — pathspec-scoped argv, never a broad stage, never a subprocess.

from: §5 test #11 (test_calibration_commit_plan.py) + §6:
  * ``render_commit_plan(decision)`` for an AUTO_COMMIT yields
    ``argv_add == ("git","add","--",WEIGHTS_PATHSPEC)`` and
    ``argv_commit == ("git","commit","-F","-","--",WEIGHTS_PATHSPEC)``;
  * NEVER contains ``-A`` / ``-u`` / ``.`` / a bare ``commit -F -`` without the ``--`` pathspec;
  * it is pure (no subprocess).

Predicted-surprise guard (GIT-WRITE PATHSPEC: never -A/-u/'.'): a regression that broadened the
stage to the working tree would change these argv and fail here. The decision is constructed
directly (a frozen dataclass), so the only thing under test is the rendering. RED until the
``render_commit_plan`` body lands; the WEIGHTS_PATHSPEC value + the no-subprocess-import purity
check are GREEN from freeze.

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import dataclasses

import pytest

import genome.calibration.commit_plan as commit_plan_module
from genome.calibration.commit_plan import WEIGHTS_PATHSPEC, render_commit_plan
from genome.calibration.model import (
    SEED_RISK_WEIGHTS,
    Direction,
    Disposition,
    RatchetDecision,
)


def _auto_commit_decision() -> RatchetDecision:
    """A realistic AUTO_COMMIT verdict (covered, clean, tighten) — the only committable case."""
    candidate = dataclasses.replace(
        SEED_RISK_WEIGHTS, weights_version="rw-2", auto_tuning_enabled=True
    )
    return RatchetDecision(
        disposition=Disposition.AUTO_COMMIT,
        knob="c_map.cli",
        direction=Direction.TIGHTEN,
        candidate_weights=candidate,
        backtest_clean=True,
        knob_covered=True,
        cited_merged_shas=("deadbeef", "cafef00d"),
        rationale="systematic under-tiering on cli; +1 tighten, back-test clean, covered",
        auto_applicable=True,
    )


def _parked_decision() -> RatchetDecision:
    """A PARK verdict — render_commit_plan must refuse it (never commit a parked change)."""
    return RatchetDecision(
        disposition=Disposition.PARK_FOR_APPROVAL,
        knob="c_map.pipeline",
        direction=Direction.LOOSEN,
        candidate_weights=dataclasses.replace(SEED_RISK_WEIGHTS, weights_version="rw-2"),
        backtest_clean=True,
        knob_covered=False,
        cited_merged_shas=("sha-a",),
        rationale="parked loosen",
        auto_applicable=False,
    )


def test_weights_pathspec_is_the_single_git_tracked_file() -> None:
    """from: §6 (WEIGHTS_PATHSPEC) — GREEN from freeze.

    The pathspec is exactly the one git-tracked weights file, repo-relative, so both argv scope
    to it after the ``--`` separator.
    """
    assert WEIGHTS_PATHSPEC == "backend/src/genome/calibration/risk_weights.json"


def test_render_commit_plan_argv_are_pathspec_scoped() -> None:
    """from: §6 (argv_add / argv_commit are exactly the pathspec-scoped forms).

    An AUTO_COMMIT renders ``git add -- <weights>`` and ``git commit -F - -- <weights>`` exactly
    — the commit is provably single-file by construction.
    """
    plan = render_commit_plan(_auto_commit_decision())
    assert plan.argv_add == ("git", "add", "--", WEIGHTS_PATHSPEC)
    assert plan.argv_commit == ("git", "commit", "-F", "-", "--", WEIGHTS_PATHSPEC)


def test_render_commit_plan_never_stages_broadly() -> None:
    """from: §6 (NEVER contains -A / -u / '.' / a bare commit -F - without the -- pathspec).

    The predicted-surprise guard: no broad-stage token may appear in either argv, and the commit
    argv must terminate the option list with ``--`` immediately before the pathspec (so a future
    ``git commit -F -`` without the ``--`` pathspec is caught).
    """
    plan = render_commit_plan(_auto_commit_decision())
    for argv in (plan.argv_add, plan.argv_commit):
        assert "-A" not in argv
        assert "-u" not in argv
        assert "." not in argv
        # the only pathspec is the weights file, and it is fenced by a literal "--"
        assert argv.count(WEIGHTS_PATHSPEC) == 1
        assert "--" in argv
        assert argv[-2:] == ("--", WEIGHTS_PATHSPEC)
    # the body is carried via "-F -" (stdin), and the rationale rides in the message
    assert plan.argv_commit[:4] == ("git", "commit", "-F", "-")
    assert "under-tiering on cli" in plan.message


def test_render_commit_plan_refuses_a_non_auto_commit_decision() -> None:
    """from: §6 (only meaningful for an AUTO_COMMIT) + the commit_plan docstring (others raise).

    The skill must never commit a parked / suppressed / no-op verdict, so rendering a non-
    AUTO_COMMIT decision fails closed (ValueError).
    """
    with pytest.raises(ValueError, match=r"AUTO_COMMIT|auto_commit|disposition"):
        render_commit_plan(_parked_decision())


def test_commit_plan_module_imports_no_subprocess() -> None:
    """from: §6 (it is pure — no subprocess) — GREEN from freeze.

    git-WRITE has zero precedent here: the Python core emits argv data and never runs git, so the
    module must not even import ``subprocess``. (The skill runs git, gated on the CLI exit.)
    """
    assert not hasattr(commit_plan_module, "subprocess")

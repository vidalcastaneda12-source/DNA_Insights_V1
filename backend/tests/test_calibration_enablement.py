"""The enablement flip + its reversibility falsifier (``finding-040``; C1 Phase 2 PR 2, plan §5).

PR 2 flips the **live** ``risk_weights.json`` to ``auto_tuning_enabled=true`` / ``rw-2`` — the only
behavioral change of the PR. These tests pin that flip and prove it is kill-switch-reversible:

* the live config is enabled at ``rw-2`` (the flip happened);
* the ``SEED_RISK_WEIGHTS`` constant stays the immutable ``rw-1`` / dark reconciliation +
  back-test + kill-switch baseline (live-file-only — the ratchet writes the file, never the
  constant);
* the **falsifier**: toggling the (now-enabled) live config back to ``auto_tuning_enabled=false``
  re-freezes the ENTIRE write surface — ``ratchet --apply`` NO_OPs and ``apply-parked --apply``
  refuses, neither writing weights — so the kill switch still works after the flip.

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

import _calibration_ledger as cl
import genome.calibration.cli as cli_mod
from genome.calibration.cli import calibration_app
from genome.calibration.model import (
    SEED_RISK_WEIGHTS,
    AuditRow,
    Direction,
    Disposition,
    RatchetDecision,
    RiskWeights,
)
from genome.calibration.persistence import read_weights

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def _const[T](value: T) -> Callable[[], T]:
    """A zero-arg callable returning ``value`` — a monkeypatch stand-in for the cli's readers."""

    def _return() -> T:
        return value

    return _return


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Reset structlog after each invocation so the per-call stderr routing does not leak."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def _parked_row() -> AuditRow:
    """A pending (applied=False) PARK row — something for ``apply-parked`` to act on."""
    candidate = dataclasses.replace(
        cl.enabled(), c_map={**SEED_RISK_WEIGHTS.c_map, "pipeline": 4}, weights_version="rw-3"
    )
    decision = RatchetDecision(
        disposition=Disposition.PARK_FOR_APPROVAL,
        knob="c_map.pipeline",
        direction=Direction.TIGHTEN,
        candidate_weights=candidate,
        backtest_clean=True,
        knob_covered=False,
        cited_merged_shas=("sha-a",),
        rationale="parked",
        auto_applicable=False,
    )
    return AuditRow(date="2026-06-25", applied=False, decision=decision)


def test_live_config_is_enabled_after_the_flip() -> None:
    """from: plan §5 (the enablement flip — the live config is enabled at rw-2).

    The live ``risk_weights.json`` carries ``auto_tuning_enabled=true`` at ``rw-2`` — the single
    behavioral change of PR 2. This is the only test that asserts the live config is ON.
    """
    weights = read_weights()
    assert weights.auto_tuning_enabled is True
    assert weights.weights_version == "rw-2"


def test_seed_constant_stays_dark_after_the_flip() -> None:
    """from: plan §5 (live-file-only — the SEED_RISK_WEIGHTS constant stays the dark rw-1 baseline).

    The flip touches only the live file; the reconciliation / back-test / kill-switch baseline
    constant is unchanged, so the kill-switch matrix test (which uses SEED) stays meaningful.
    """
    assert SEED_RISK_WEIGHTS.auto_tuning_enabled is False
    assert SEED_RISK_WEIGHTS.weights_version == "rw-1"


def test_kill_switch_toggle_off_refreezes_both_write_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: plan §6 (the reversibility falsifier — toggling false re-freezes both write paths).

    Starting from the now-enabled live config, toggling ``auto_tuning_enabled`` back to ``false``
    re-freezes BOTH write paths: the ledger that would ``AUTO_COMMIT`` under enabled weights makes
    ``ratchet --apply`` a NO_OP, and the pending parked row makes ``apply-parked --apply`` refuse
    (FIX-3 HONOR). Neither writes — the kill switch still works after the flip.
    """
    disabled = dataclasses.replace(read_weights(), auto_tuning_enabled=False)
    monkeypatch.setattr(cli_mod, "read_weights", _const(disabled))
    monkeypatch.setattr(
        cli_mod, "load_outcomes", _const(cl.ledger(12, 1, cl.breakdown(c=1), cl.actual_blocked()))
    )
    monkeypatch.setattr(cli_mod, "load_audit", _const([_parked_row()]))
    writes: list[RiskWeights] = []
    monkeypatch.setattr(cli_mod, "write_weights", writes.append)
    monkeypatch.setattr(cli_mod, "append_audit", lambda _row: None)

    # ratchet --apply: a covered-tighten ledger that AUTO_COMMITs under enabled weights NO_OPs here.
    ratchet_result = CliRunner().invoke(
        calibration_app, ["ratchet", "--apply", "--merges-since-last", "5"]
    )
    assert ratchet_result.exit_code == 0, ratchet_result.output

    # apply-parked --apply: the pending parked row is frozen by the honored kill switch.
    parked_result = CliRunner().invoke(calibration_app, ["apply-parked", "--apply"])
    assert parked_result.exit_code == 0, parked_result.output
    assert "kill switch off" in parked_result.stdout

    assert writes == []  # neither write path mutated the weights

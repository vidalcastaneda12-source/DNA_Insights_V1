"""Write-hook loop closure — write-outcome sources predicted from the persisted manifest (FIX-3).

from: §5 test #13 (test_calibration_writehook_roundtrip.py) + §6:
  * persist a PredictedManifest, then ``write-outcome`` SOURCES the predicted block from that
    manifest, appends an OutcomeRecord, and the ledger is NON-EMPTY and round-trips through
    from_json;
  * a manifest ABSENT for the scope -> a VISIBLE "outcome NOT recorded" warning + exit 0
    (non-blocking) + the ledger stays EMPTY (no corrupt/partial row).

Predicted-surprise guard (WRITE-HOOK AVAILABILITY): an absent manifest must be a visible drop,
never a silent or corrupt append. The manifest is set up via the real ``write_manifest`` seam (so
the persisted shape and the CLI's read stay mutually consistent), and the predicted tier is read
back from the appended record to prove it was SOURCED from the manifest (not re-typed at close).
RED until ``write_manifest`` / ``read_manifest`` / the ``write-outcome`` body land.

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome.calibration.cli import calibration_app
from genome.calibration.model import (
    OutcomeRecord,
    PredictedBlock,
    PredictedManifest,
    TierBreakdown,
)
from genome.calibration.persistence import load_outcomes, write_manifest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Reset structlog after each invocation so the per-call stderr routing does not leak."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def _actual_json() -> str:
    """A realistic close-side ACTUAL gate-facts payload (ActualBlock shape) — a clean pass."""
    return json.dumps(
        {
            "gate_verdict": "pass",
            "review_blockers": [],
            "surprises_materialized": [],
            "surprises_missed": [],
            "anchors_moved_unexpected": [],
            "revise_cycles": 0,
            "fix_first_cycles": 0,
            "needed_deep": False,
        }
    )


def _persist_manifest(scope_id: str, predicted_tier: int) -> None:
    """Persist a dispatch-time predicted manifest under the (cwd-relative) default store."""
    manifest = PredictedManifest(
        scope_id=scope_id,
        risk_weights_version="rw-1",
        predicted=PredictedBlock(
            tier=predicted_tier,
            breakdown=TierBreakdown(c=3, b=1, p=1, a=0, s=5, floor=0, deep_t2=False),
        ),
    )
    write_manifest(manifest)


def test_write_outcome_sources_predicted_from_the_manifest_and_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from: §6 (write-outcome sources predicted from the persisted manifest -> ledger non-empty).

    With a persisted manifest (predicted Tier 2), ``write-outcome`` appends exactly one
    OutcomeRecord whose predicted tier is the manifest's (sourced, not re-typed) and whose actual
    is the supplied close facts; the appended record round-trips through from_json.
    """
    monkeypatch.chdir(tmp_path)
    _persist_manifest("PR-77", predicted_tier=2)

    result = CliRunner().invoke(
        calibration_app,
        ["write-outcome", "--scope-id", "PR-77", "--actual-json", "-"],
        input=_actual_json(),
    )
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception

    records = load_outcomes()
    assert len(records) == 1
    rec = records[0]
    assert rec.scope_id == "PR-77"
    assert rec.predicted.tier == 2  # sourced from the manifest, not the actual JSON
    assert rec.actual.gate_verdict == "pass"
    assert OutcomeRecord.from_json(rec.to_json()) == rec


def test_write_outcome_absent_manifest_is_a_visible_drop_not_a_corrupt_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from: §6 (manifest ABSENT -> visible 'outcome NOT recorded' warning + exit 0 + ledger EMPTY).

    With no persisted manifest for the scope, ``write-outcome`` does NOT block the close: it warns
    visibly ("outcome NOT recorded"), exits 0 (non-blocking), and appends nothing — never a
    corrupt/partial row.
    """
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        calibration_app,
        ["write-outcome", "--scope-id", "PR-404", "--actual-json", "-"],
        input=_actual_json(),
    )
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    assert "outcome NOT recorded" in result.output
    assert not (tmp_path / "data" / "calibration" / "outcomes.jsonl").exists()

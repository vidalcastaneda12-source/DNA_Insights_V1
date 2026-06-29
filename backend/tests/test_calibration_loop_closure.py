"""The deterministic loop-closure test — the hard enablement gate (``finding-040``; plan §5).

Proves the FULL cross-run learning loop closes deterministically, end to end through the real CLI +
the real persistence serialization:

    outcome ledger → ratchet proposes → AUTO_COMMIT → write_weights → read_weights → compute_tier
    emits the NEW (higher) tier

A ledger of 12 systematically under-tiered outcomes (predicted Tier 1, breakdown ``c=1`` →
dominant knob ``c_map.tests``; merge-time ``blocked`` → hindsight Tier 2) drives the ratchet to
auto-commit a ``c_map.tests`` ``1 → 2`` tighten (back-test-clean + unfloored-covered). A manifest
sitting exactly at the ``t2`` boundary (``change_class=["tests"]``, 8 imports → moderate, ``minor``
precedent) scores **Tier 1** under the seed (C1+B2+P1=S4) and **Tier 2** after the auto-commit
(C2+B2+P1=S5≥t2). The tier FLIP across the apply proves the written weights flow back into
``compute_tier``.

This test writes ONLY under ``tmp_path`` — never the git-tracked ``risk_weights.json`` — by
monkeypatching the cli's ``read_weights`` / ``write_weights`` to a temp file (the real persistence
serialization round-trip, relocated) and ``chdir``-ing so the audit ledger lands under ``tmp_path``.

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

import _calibration_ledger as cl
import genome.calibration.cli as cli_mod
from genome.calibration import persistence as persistence_mod
from genome.calibration.cli import calibration_app

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


def _boundary_manifest() -> str:
    """A manifest at the ``t2`` band edge: tests (C from c_map.tests) + 8 imports (moderate B=2) +
    minor precedent (P=1). S = C + 2 + 1, so it flips Tier 1 → Tier 2 exactly when ``c_map.tests``
    goes 1 → 2."""
    return json.dumps(
        {
            "change_class": ["tests"],
            "imports_touched_count": 8,
            "precedent_surprise": "minor",
            "applicable_anchors_count": 0,
        }
    )


def _tier(manifest: str) -> int:
    result = CliRunner().invoke(
        calibration_app, ["compute-tier", "--manifest", "-"], input=manifest
    )
    assert result.exit_code == 0, result.output
    return int(json.loads(result.stdout)["tier"])


def test_outcome_ledger_to_ratchet_apply_to_compute_tier_closes_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from: plan §5 (the deterministic loop-closure test — the hard enablement gate).

    The applied weight change deterministically moves the emitted tier of a boundary manifest from
    1 to 2 — the outcome ledger really does feed the next ``compute_tier`` via the ratchet's write.
    The git-tracked ``risk_weights.json`` is never touched (a temp file stands in as live config).
    """
    monkeypatch.chdir(tmp_path)
    weights_path = tmp_path / "risk_weights.json"
    # Seed the temp live config ENABLED (the dark seed would NO_OP at the kill switch).
    weights_path.write_text(json.dumps(cl.enabled().to_json()), encoding="utf-8")
    monkeypatch.setattr(cli_mod, "read_weights", lambda: persistence_mod.read_weights(weights_path))
    monkeypatch.setattr(
        cli_mod,
        "write_weights",
        lambda weights: persistence_mod.write_weights(weights, weights_path),
    )
    # 12 systematically under-tiered outcomes targeting the covered, clean c_map.tests knob.
    ledger = cl.ledger(12, 1, cl.breakdown(c=1), cl.actual_blocked())
    monkeypatch.setattr(cli_mod, "load_outcomes", lambda: ledger)

    manifest = _boundary_manifest()

    # (A) Before the apply: the boundary manifest is Tier 1 under the seed weights.
    assert _tier(manifest) == 1

    # (B) The ratchet auto-commits the c_map.tests tighten and emits the pathspec-scoped CommitPlan.
    applied = CliRunner().invoke(
        calibration_app, ["ratchet", "--apply", "--merges-since-last", "5"]
    )
    assert applied.exit_code == 0, applied.output
    assert "argv_commit" in applied.stdout

    # (C) The new weights were persisted (version bumped, knob moved) — a real write, not a stub.
    persisted = persistence_mod.read_weights(weights_path)
    assert persisted.c_map["tests"] == 2
    assert persisted.weights_version == "rw-2"

    # (D) The loop closes: compute_tier now reads the NEW weights and emits Tier 2 for the same
    # manifest. The tier moved BECAUSE the ratchet's write flowed back into the source of truth.
    assert _tier(manifest) == 2

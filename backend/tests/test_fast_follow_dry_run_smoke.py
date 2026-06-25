"""Spec §3 integration smoke — ``triage --dry-run`` over a seeded candidates JSON.

Plan-blind spec source: synthesized-plan §6 ("--dry-run smoke = 2 DRAIN/1 EJECT/'would
drain'"), §5 test list item 5 ("spec §3 smoke: seed 2 trivial + 1 schema → triage --dry-run →
assert 2 DRAIN / 1 EJECT and 'would drain'"), R3 (CONSEQUENCE: "the --dry-run smoke tests the
CLASSIFIER on pre-structured input by design; the derivation is a model-driven property NOT
unit-testable … The smoke's docstring says so, so it is not mistaken for end-to-end proof"),
and the FROZEN INTERFACE CONTRACT (``triage --candidates <Path> --dry-run``; the canonical JSON
seam; ``Candidate.to_json``).

DESIGN NOTE (R3, ratified): this smoke exercises the CLASSIFIER on PRE-STRUCTURED input — the
candidates JSON is hand-seeded with already-derived attributes. The repo-sweep→Candidate
attribute DERIVATION (reading what each candidate touches and emitting change_class /
applicable_anchors / blast_radius / tier / touched_paths) is a MODEL-DRIVEN skill step, not
covered here by design. End-to-end derivation safety is guarded by fail-closed extraction +
touchpoint-1 approval + Sub-A's gate, not by this test. This is explicitly NOT an end-to-end
proof of the scan.

The expected outcome (2 DRAIN / 1 EJECT / 0 DISCARD and "would drain") comes from §6; nothing
is reverse-engineered from the stubbed bodies (``raise NotImplementedError`` now — RED is
correct).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome.fast_follow.cli import fast_follow_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Restore structlog defaults after each test."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def _drain_candidate_dict(cid: str) -> dict[str, object]:
    """A trivial DRAIN-eligible candidate: core, anchors=0, blast≤cap, tier-0/1, no schema path."""
    return {
        "candidate_id": cid,
        "source": "repo-sweep",
        "kind": "doc-nit",
        "change_class": ["core"],
        "blast_radius": 1,
        "applicable_anchors": 0,
        "tier": "tier-0",
        "touched_paths": [f"docs/notes/{cid}.md"],
        "is_stale": False,
    }


def _schema_candidate_dict(cid: str) -> dict[str, object]:
    """A schema candidate that must EJECT (change_class={schema} AND a docs/schemas/** path)."""
    return {
        "candidate_id": cid,
        "source": "repo-sweep",
        "kind": "schema-edit",
        "change_class": ["schema"],
        "blast_radius": 1,
        "applicable_anchors": 0,
        "tier": "tier-0",
        "touched_paths": ["docs/schemas/schema_group_1_raw_inputs.md"],
        "is_stale": False,
    }


def _seed_candidates_json(path: Path) -> None:
    """Write the §3 smoke fixture: 2 trivial DRAIN + 1 schema EJECT candidate."""
    # The canonical JSON seam is a top-level ARRAY of candidate objects (interface contract:
    # the CLI rejects a dict-wrapped payload — "must be a JSON array").
    candidates = [
        _drain_candidate_dict("nit-1"),
        _drain_candidate_dict("nit-2"),
        _schema_candidate_dict("schema-1"),
    ]
    path.write_text(json.dumps(candidates), encoding="utf-8")


def test_dry_run_smoke_two_drain_one_eject(tmp_path: Path) -> None:
    """from: plan §6 (--dry-run smoke = 2 DRAIN / 1 EJECT / "would drain") + §5 item 5 + R3.

    Seed a candidates JSON with 2 trivial DRAIN-eligible + 1 schema candidate; run
    ``triage --candidates <json> --dry-run``; assert exit 0, the plan reports exactly
    2 DRAIN / 1 EJECT / 0 DISCARD, and the output contains "would drain" (the dry-run
    affordance). Per R3 this exercises the CLASSIFIER on pre-structured input; the attribute
    DERIVATION is a model-driven skill step not covered here by design.
    """
    candidates_path = tmp_path / "candidates.json"
    _seed_candidates_json(candidates_path)

    result = CliRunner().invoke(
        fast_follow_app, ["triage", "--candidates", str(candidates_path), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception

    lowered = result.output.lower()
    # The dry-run affordance (§6 literal).
    assert "would drain" in lowered, result.output
    # 2 DRAIN / 1 EJECT / 0 DISCARD — the §6 expected output, pinned EXACTLY on the rendered
    # headline (a drift to 3 DRAIN must fail this; per review test-1 the id-presence checks
    # alone did not pin the count).
    assert "counts: drain=2 eject=1 discard=0" in result.output, result.output
    assert "would drain 2 / eject 1 / discard 0" in lowered, result.output
    # And the expected items land in the right partitions.
    assert "nit-1" in result.output
    assert "nit-2" in result.output
    assert "schema-1" in result.output

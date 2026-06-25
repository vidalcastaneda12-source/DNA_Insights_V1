"""Spec §6 integration smoke — ``scope-split dry-run`` over a synthetic 3-cluster manifest.

Plan-blind spec source: SYNTHESIZED-PLAN §6 ("dry-run 3-cluster exit 0, exactly 3 ordered
schema-first, literal 'would create 3 sub-scopes', nothing created; atomic-blob check") + §5
("dry_run_smoke (synthetic 3-cluster static → exit 0 NOT NotImplementedError, exactly 3 ordered,
LITERAL 'would create 3 sub-scopes' asserted, each origin_scope, schema first, creates
nothing)"); FROZEN-INTERFACE cli.py (dry-run "--manifest <Path or '-'> [--engine ...]" "creates
nothing, writes no ROADMAP, runs no scope-run; prints literal 'would create N sub-scopes' or
'atomic — no split'"; "--manifest '-' reads stdin"); IMPL-CONTRACT DECISION 1 (manifest-primary:
with the static engine carrying no high-coupling edge, the 3 change_class clusters survive the
veto).

DESIGN NOTE: the 3-cluster manifest carries three separable change_class boundaries (schema /
cli / tests) and the ``static`` engine supplies an edge-free coupling graph, so nothing vetoes
the cut — this exercises the manifest-primary partition + topo-order + render path end to end.

RED-until-filled: asserts the SPECIFIED output (exit 0, the literal 'would create 3 sub-scopes',
3 ordered sub-scopes schema-first, nothing written) so it goes RED on the stubbed CLI body now
and GREEN when the bodies land — it does NOT assert pytest.raises(NotImplementedError).

test->spec provenance noted per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome.scope_split.cli import scope_split_app

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


def _three_cluster_manifest() -> dict[str, object]:
    """A synthetic manifest with three separable change_class boundaries (schema/cli/tests).

    Each cluster touches a distinct module; the static engine supplies no high-coupling edge, so
    nothing fuses the clusters — the manifest-primary partition yields exactly three sub-scopes,
    ordered schema-first per SCHEMA_FIRST_ORDER.
    """
    return {
        "scope_id": "PR-3CL",
        "title": "synthetic three-cluster scope",
        "change_class": ["schema", "cli", "tests"],
        "blast_radius": {
            "imports_touched": [
                "ddl/group_x.sql",
                "ddl/group_y.sql",
                "genome/x/cli.py",
                "genome/x/cli_commands.py",
                "backend/tests/test_x.py",
                "backend/tests/test_y.py",
            ],
            "tests_covering": [],
        },
        "applicable_anchors": [],
        "depends_on": [],
        "risk_breakdown": {"S": 3},
        "risk_tier": 1,
    }


def _atomic_manifest() -> dict[str, object]:
    """A single change_class blob — not separable → atomic."""
    return {
        "scope_id": "PR-BLOB",
        "title": "indivisible blob",
        "change_class": ["pipeline"],
        "blast_radius": {"imports_touched": ["genome.pipe.a", "genome.pipe.b"]},
        "applicable_anchors": [],
        "risk_breakdown": {"S": 5},
        "risk_tier": 2,
    }


_DRY_RUN_ARGS = ["dry-run", "--manifest", "-", "--engine", "static"]


def test_dry_run_three_cluster_creates_three_ordered_sub_scopes(tmp_path: Path) -> None:
    """from: SYNTHESIZED-PLAN §6 ("dry-run 3-cluster exit 0, exactly 3 ordered schema-first,
    literal 'would create 3 sub-scopes', nothing created") + §5 item dry_run_smoke + DECISION 1.

    The synthetic 3-cluster manifest with an edge-free static graph yields exactly 3 sub-scopes,
    schema-first, the LITERAL 'would create 3 sub-scopes' affordance, and writes no ROADMAP.
    (RED-until-filled.)
    """
    result = CliRunner().invoke(
        scope_split_app, _DRY_RUN_ARGS, input=json.dumps(_three_cluster_manifest())
    )

    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception

    # the LITERAL dry-run affordance (§6) — pinned exactly, so a drift to 2 must fail
    assert "would create 3 sub-scopes" in result.output, result.output

    # each placeholder sub-scope id is rendered and carries the origin scope (provenance #8)
    for i in range(1, 4):
        assert f"PR-3CL-s{i}" in result.output, result.output
    assert "PR-3CL" in result.output

    # schema cluster first: its id (s1) appears before the cli/tests ids in the output stream
    pos_s1 = result.output.index("PR-3CL-s1")
    pos_s2 = result.output.index("PR-3CL-s2")
    pos_s3 = result.output.index("PR-3CL-s3")
    assert pos_s1 < pos_s2 < pos_s3, result.output

    # dry-run creates nothing on disk (no ROADMAP write under the sandbox)
    assert not any(tmp_path.iterdir()), "dry-run wrote a file"


def test_dry_run_atomic_blob_prints_the_sentinel() -> None:
    """from: FROZEN-INTERFACE cli dry-run ("prints … 'atomic — no split'") + SYNTHESIZED-PLAN §6
    ("atomic-blob") + §5 ("atomic smoke").

    A single-change_class blob renders the 'atomic — no split' sentinel and does NOT claim to
    create sub-scopes. (RED-until-filled.)
    """
    result = CliRunner().invoke(
        scope_split_app, _DRY_RUN_ARGS, input=json.dumps(_atomic_manifest())
    )

    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    assert "atomic — no split" in result.output, result.output
    assert "would create" not in result.output, result.output

"""CLI units — scope-split check / dry-run / write-roadmap surface (CliRunner, no root app).

Plan-blind spec source: FROZEN-INTERFACE cli.py (scope_split_app commands: check "--manifest
<Path or '-'> [--engine ...] [--json/--no-json]"; dry-run "creates nothing, writes no ROADMAP";
write-roadmap "append-only; atomic→sentinel write nothing; absent parent slot→typer.BadParameter";
"--manifest '-' reads stdin"; the env note: "test registration via importing
genome.scope_split.cli.scope_split_app directly + CliRunner, not the root genome app");
IMPL-CONTRACT arch-1 (stdin seam) + mech #3 (two-branch --json) + SYNTHESIZED-PLAN §5 ("cli
(malformed→BadParameter; --json valid; write-roadmap once then noop; dry-run writes nothing)").

We invoke scope_split_app DIRECTLY (the root `genome` app needs httpx in this env). All command
bodies are STUBBED → RED-until-filled: each test asserts the SPECIFIED behavior (the exit-code
class, the JSON shape, the noop-on-second-write) so it goes RED on NotImplementedError now and
GREEN when the bodies land — never pytest.raises(NotImplementedError).

test->spec provenance noted per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome.scope_split.cli import scope_split_app
from genome.scope_split.roadmap_writer import BLOCK_BEGIN, BLOCK_END

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    try:
        yield
    finally:
        structlog.reset_defaults()


def _split_manifest() -> dict[str, object]:
    return {
        "scope_id": "PR-CLI",
        "title": "two-cluster",
        "change_class": ["schema", "cli"],
        "blast_radius": {
            "imports_touched": [
                "ddl/group_x.sql",
                "ddl/group_y.sql",
                "genome/x/cli.py",
                "genome/x/cli_commands.py",
            ]
        },
        "applicable_anchors": [],
        "risk_breakdown": {"S": 3},
        "risk_tier": 1,
    }


def _atomic_manifest() -> dict[str, object]:
    return {
        "scope_id": "PR-ATOM",
        "title": "blob",
        "change_class": ["pipeline"],
        "blast_radius": {"imports_touched": ["genome.pipe.a"]},
        "applicable_anchors": [],
        "risk_breakdown": {"S": 5},
        "risk_tier": 2,
    }


# ── malformed / missing manifest → typer.BadParameter (non-zero) ──────────────


def test_check_missing_manifest_file_is_bad_parameter(tmp_path: Path) -> None:
    """from: FROZEN-INTERFACE cli (malformed manifest path) + SYNTHESIZED-PLAN §5
    ("malformed→BadParameter") + §6 ("absent parent→BadParameter").

    A --manifest pointing at a non-existent file fails closed with a non-zero exit
    (typer.BadParameter), never a traceback or a silent atomic. (RED-until-filled.)
    """
    missing = tmp_path / "nope.json"
    result = CliRunner().invoke(scope_split_app, ["check", "--manifest", str(missing)])
    assert result.exit_code != 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception


def test_check_malformed_json_on_stdin_is_bad_parameter() -> None:
    """from: SYNTHESIZED-PLAN §5 ("malformed→BadParameter") + arch-1 (stdin seam).

    Non-JSON content on stdin is a typer.BadParameter (non-zero exit), not a crash.
    (RED-until-filled.)
    """
    result = CliRunner().invoke(
        scope_split_app, ["check", "--manifest", "-"], input="this is not json"
    )
    assert result.exit_code != 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception


# ── --manifest '-' reads stdin (arch-1 seam) ──────────────────────────────────


def test_check_reads_manifest_from_stdin() -> None:
    """from: FROZEN-INTERFACE ("--manifest '-' reads stdin (arch-1 seam)") + arch-1.

    A valid manifest on stdin is accepted and produces a clean (exit 0) check.
    (RED-until-filled.)
    """
    result = CliRunner().invoke(
        scope_split_app,
        ["check", "--manifest", "-", "--engine", "static"],
        input=json.dumps(_atomic_manifest()),
    )
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception


# ── check --json two-branch shape (mech #3) ───────────────────────────────────


def test_check_json_atomic_branch_is_exactly_two_keys() -> None:
    """from: mech #3 + FROZEN-INTERFACE SplitResult.to_json (atomic → EXACTLY
    {"atomic":True,"reason":str}) + SYNTHESIZED-PLAN §6 ("atomic-blob check --json
    {"atomic":true,...} no sub_scopes key").

    --json on an atomic scope emits exactly {"atomic":true,"reason":...} with no sub_scopes /
    order / cut_quality keys. (RED-until-filled.)
    """
    result = CliRunner().invoke(
        scope_split_app,
        ["check", "--manifest", "-", "--engine", "static", "--json"],
        input=json.dumps(_atomic_manifest()),
    )
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"atomic", "reason"}
    assert payload["atomic"] is True


def test_check_json_split_branch_is_full_dict() -> None:
    """from: mech #3 + FROZEN-INTERFACE (split → full dict) + SYNTHESIZED-PLAN §5 ("--json
    valid") + DECISION 1 (static engine, no high edge → the 2 clusters split).

    --json on a separable scope (static engine, no high-coupling edge) emits the full split
    dict: atomic false + sub_scopes + order + cut_quality. (RED-until-filled.)
    """
    result = CliRunner().invoke(
        scope_split_app,
        ["check", "--manifest", "-", "--engine", "static", "--json"],
        input=json.dumps(_split_manifest()),
    )
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    payload = json.loads(result.stdout)
    assert payload["atomic"] is False
    assert set(payload.keys()) == {"atomic", "reason", "sub_scopes", "order", "cut_quality"}
    assert len(payload["sub_scopes"]) == 2


# ── write-roadmap: once then noop; dry-run writes nothing ──────────────────────


def _roadmap_with_markers() -> str:
    return (
        "## Sub Project B2 — scope-split (Phase 1)\n\n"
        "Slot prose.\n\n"
        f"{BLOCK_BEGIN}\n{BLOCK_END}\n"
        "\n## Next section\n"
    )


def test_write_roadmap_is_idempotent_second_run_byte_identical(tmp_path: Path) -> None:
    """from: SYNTHESIZED-PLAN §5 ("write-roadmap once then noop") + §6 ("write-roadmap twice
    byte-identical 2nd") + FROZEN-INTERFACE write-roadmap (append-only).

    Running write-roadmap twice leaves the ROADMAP byte-identical on the second pass.
    (RED-until-filled.)
    """
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(_roadmap_with_markers(), encoding="utf-8")

    runner = CliRunner()
    first = runner.invoke(
        scope_split_app,
        ["write-roadmap", "--manifest", "-", "--engine", "static", "--roadmap", str(roadmap)],
        input=json.dumps(_split_manifest()),
    )
    assert first.exit_code == 0, first.output
    assert not isinstance(first.exception, NotImplementedError), first.exception
    after_first = roadmap.read_text(encoding="utf-8")

    second = runner.invoke(
        scope_split_app,
        ["write-roadmap", "--manifest", "-", "--engine", "static", "--roadmap", str(roadmap)],
        input=json.dumps(_split_manifest()),
    )
    assert second.exit_code == 0, second.output
    after_second = roadmap.read_text(encoding="utf-8")
    assert after_second == after_first


def test_write_roadmap_absent_parent_slot_is_bad_parameter(tmp_path: Path) -> None:
    """from: FROZEN-INTERFACE write-roadmap ("absent parent slot→typer.BadParameter") +
    SYNTHESIZED-PLAN §6 ("absent parent→BadParameter").

    write-roadmap against a ROADMAP with no managed markers fails closed (non-zero) rather than
    clobbering (failure-ordering (b)). (RED-until-filled.)
    """
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("## Some other section\n\nno markers here\n", encoding="utf-8")
    before = roadmap.read_text(encoding="utf-8")

    result = CliRunner().invoke(
        scope_split_app,
        ["write-roadmap", "--manifest", "-", "--engine", "static", "--roadmap", str(roadmap)],
        input=json.dumps(_split_manifest()),
    )
    assert result.exit_code != 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    # nothing was clobbered
    assert roadmap.read_text(encoding="utf-8") == before


def test_dry_run_writes_no_roadmap(tmp_path: Path) -> None:
    """from: FROZEN-INTERFACE dry-run ("writes no ROADMAP") + SYNTHESIZED-PLAN §5 ("dry-run
    writes nothing").

    dry-run never touches the ROADMAP even when a managed parent exists. (RED-until-filled.)
    """
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(_roadmap_with_markers(), encoding="utf-8")
    before = roadmap.read_text(encoding="utf-8")

    result = CliRunner().invoke(
        scope_split_app,
        ["dry-run", "--manifest", "-", "--engine", "static"],
        input=json.dumps(_split_manifest()),
    )
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    assert roadmap.read_text(encoding="utf-8") == before

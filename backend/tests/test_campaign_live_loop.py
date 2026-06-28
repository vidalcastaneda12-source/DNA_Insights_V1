"""Multi-session live-loop test for ``genome campaign`` (PR 2; ``finding-041``).

Spec source: the approved PR-2 plan §5 ("the multi-session live-loop"). PLAN-BLIND / TEST-FIRST:
written from the spec + the FROZEN interface (the four live-launch commands + the append-only
ledger contract), NOT from any implementation — the commands do not exist yet, so this file is
expected RED until the live-launch wiring lands.

The loop proves the PR-2 headline capability: a campaign driven END TO END across a SEQUENCE OF
SEPARATE CliRunner invocations (each one a fresh process-like run that re-reads the ledger from
disk — no in-memory carryover), through both human gates per sub-scope, in dependency order, while
the ledger stays STRICTLY append-only (record count grows every step; locked decision #7) and the
ROADMAP reflects the terminal state. We invoke ``campaign_app`` directly with ``--campaign-dir`` /
``--roadmap`` redirected to ``tmp_path`` (mirroring ``test_campaign_dry_run``).
"""

from __future__ import annotations

import json
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome.campaign.cli import campaign_app
from genome.campaign.model import CampaignStatus
from genome.campaign.persistence import load_campaign, load_history
from genome.scope_split.roadmap_writer import BLOCK_BEGIN, BLOCK_END

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from click.testing import Result

    from genome.campaign.model import CampaignState, SubScopeState


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """The campaign callback reconfigures structlog (stderr); reset it after each test."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def _split_manifest() -> dict[str, object]:
    """A manifest the static engine splits into two ordered sub-scopes (schema before cli)."""
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
            ],
        },
        "applicable_anchors": [],
        "risk_breakdown": {"S": 3},
        "risk_tier": 1,
    }


def _roadmap_with_region() -> str:
    return f"# ROADMAP\n\nintro\n\n{BLOCK_BEGIN}\n{BLOCK_END}\n\noutro\n"


def _paths(home: Path) -> tuple[Path, Path]:
    return home / "campaign", home / "ROADMAP.md"


def _load(home: Path) -> CampaignState:
    return load_campaign("PR-CLI", campaign_dir=home / "campaign")


def _require(home: Path, sub: str) -> SubScopeState:
    record = _load(home).by_id(sub)
    assert record is not None, f"no active record for {sub!r}"
    return record


def _ledger_len(home: Path) -> int:
    """The number of records in the append-only PR-CLI ledger (monotonic by #7)."""
    return len(load_history("PR-CLI", campaign_dir=home / "campaign"))


def _start(home: Path) -> None:
    cdir, roadmap = _paths(home)
    roadmap.write_text(_roadmap_with_region(), encoding="utf-8")
    result = CliRunner().invoke(
        campaign_app,
        [
            "start",
            "--manifest",
            "-",
            "--engine",
            "static",
            "--campaign-dir",
            str(cdir),
            "--roadmap",
            str(roadmap),
        ],
        input=json.dumps(_split_manifest()),
    )
    assert result.exit_code == 0, result.output


def _revalidate(home: Path, sub: str, decision: str) -> Result:
    cdir, roadmap = _paths(home)
    return CliRunner().invoke(
        campaign_app,
        [
            "revalidate",
            "--campaign",
            "PR-CLI",
            "--sub-scope",
            sub,
            "--decision",
            decision,
            "--engine",
            "static",
            "--campaign-dir",
            str(cdir),
            "--roadmap",
            str(roadmap),
        ],
    )


def _approve_plan(home: Path, sub: str) -> Result:
    cdir, roadmap = _paths(home)
    return CliRunner().invoke(
        campaign_app,
        [
            "approve-plan",
            "--campaign",
            "PR-CLI",
            "--sub-scope",
            sub,
            "--approved",
            "--campaign-dir",
            str(cdir),
            "--roadmap",
            str(roadmap),
        ],
    )


def _record_merge(home: Path, sub: str) -> Result:
    cdir, roadmap = _paths(home)
    return CliRunner().invoke(
        campaign_app,
        [
            "record-merge",
            "--campaign",
            "PR-CLI",
            "--sub-scope",
            sub,
            "--merged",
            "--campaign-dir",
            str(cdir),
            "--roadmap",
            str(roadmap),
        ],
    )


def test_multi_session_live_loop_drives_both_sub_scopes_to_merged_append_only(
    tmp_path: Path,
) -> None:
    """from: plan §5 — the multi-session live-loop.

    Each step is a SEPARATE CliRunner invocation that reloads from disk: revalidate still_needed →
    approve-plan --approved → record-merge --merged for each sub-scope in dependency order. At the
    end every sub-scope is MERGED, the campaign is_done(), the ledger record count strictly
    increased at every step (append-only, #7), and the ROADMAP shows both as [x] merged.
    """
    _start(tmp_path)
    counts = [_ledger_len(tmp_path)]

    # Sub-scope 1 — through both human gates to merged.
    assert _revalidate(tmp_path, "PR-CLI-s1", "still_needed").exit_code == 0
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.PLANNING
    counts.append(_ledger_len(tmp_path))

    assert _approve_plan(tmp_path, "PR-CLI-s1").exit_code == 0
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.IMPLEMENTING
    counts.append(_ledger_len(tmp_path))

    assert _record_merge(tmp_path, "PR-CLI-s1").exit_code == 0
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.MERGED
    assert _require(tmp_path, "PR-CLI-s2").status is CampaignStatus.READY  # teed up by the merge
    counts.append(_ledger_len(tmp_path))

    # Sub-scope 2 — now ready, through both gates to merged.
    assert _revalidate(tmp_path, "PR-CLI-s2", "still_needed").exit_code == 0
    assert _require(tmp_path, "PR-CLI-s2").status is CampaignStatus.PLANNING
    counts.append(_ledger_len(tmp_path))

    assert _approve_plan(tmp_path, "PR-CLI-s2").exit_code == 0
    assert _require(tmp_path, "PR-CLI-s2").status is CampaignStatus.IMPLEMENTING
    counts.append(_ledger_len(tmp_path))

    assert _record_merge(tmp_path, "PR-CLI-s2").exit_code == 0
    counts.append(_ledger_len(tmp_path))

    final = _load(tmp_path)
    assert final.is_done()
    assert all(s.status is CampaignStatus.MERGED for s in final.sub_scopes)
    assert all(b > a for a, b in pairwise(counts))  # strictly append-only

    roadmap = (tmp_path / "ROADMAP.md").read_text(encoding="utf-8")
    assert "- [x] **PR-CLI-s1** — merged" in roadmap
    assert "- [x] **PR-CLI-s2** — merged" in roadmap

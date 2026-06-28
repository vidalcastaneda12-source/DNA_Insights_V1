"""CLI tests for ``genome campaign`` — dry-run / start / status / resume / cancel / write-roadmap
surface (CliRunner, no root app).

Spec source: SYNTHESIZED-PLAN §4 step 5 (``cli.py`` commands) + §5 (``test_campaign_dry_run.py`` —
'dry-run proposes the plan and creates NOTHING') + the design §2 surface. Plan-blind. We invoke
``campaign_app`` DIRECTLY (the root ``genome`` app needs httpx in this env), mirroring the
scope_split CLI test, with ``--campaign-dir`` / ``--roadmap`` redirected to tmp paths so no test
touches the real ``data/`` or ``ROADMAP.md``.

PR-1 boundary: the campaign is a planning + tracking scaffold — it seeds, inspects, resumes, and
reflects, but does NOT advance the lifecycle from the CLI (recording the human gate events as a
sub-scope is driven is the PR-2 live-launch wiring; the advancement reducers are PR-1 core, unit
tested in ``test_campaign_state_machine.py``).
"""

from __future__ import annotations

import json
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


def _atomic_manifest() -> dict[str, object]:
    """A single-change-class manifest the splitter reports atomic (one indivisible unit)."""
    return {
        "scope_id": "PR-ATOM",
        "title": "blob",
        "change_class": ["pipeline"],
        "blast_radius": {"imports_touched": ["genome.pipe.a"]},
        "applicable_anchors": [],
        "risk_breakdown": {"S": 5},
        "risk_tier": 2,
    }


def _roadmap_with_region() -> str:
    return (
        f"# ROADMAP\n\nhand-authored intro\n\n{BLOCK_BEGIN}\n{BLOCK_END}\n\nhand-authored outro\n"
    )


def _start(runner: CliRunner, campaign_dir: Path, roadmap: Path) -> None:
    """Seed a PR-CLI campaign into ``campaign_dir`` with the two-cluster manifest."""
    roadmap.write_text(_roadmap_with_region(), encoding="utf-8")
    result = runner.invoke(
        campaign_app,
        [
            "start",
            "--manifest",
            "-",
            "--engine",
            "static",
            "--campaign-dir",
            str(campaign_dir),
            "--roadmap",
            str(roadmap),
        ],
        input=json.dumps(_split_manifest()),
    )
    assert result.exit_code == 0, result.output


def test_dry_run_proposes_the_order_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from: §5 ('dry-run proposes the decomposition + campaign plan, creates nothing')."""
    monkeypatch.chdir(tmp_path)  # any accidental data/ write would land here
    result = CliRunner().invoke(
        campaign_app,
        ["dry-run", "--manifest", "-", "--engine", "static"],
        input=json.dumps(_split_manifest()),
    )
    assert result.exit_code == 0, result.output
    assert "would run 2 sub-scopes in order" in result.output
    assert "PR-CLI-s1" in result.output
    assert "PR-CLI-s2" in result.output
    assert not (tmp_path / "data").exists()  # creates nothing


def test_dry_run_on_an_atomic_manifest_reports_atomic_and_creates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: §5 ('an atomic manifest prints the atomic/no-campaign sentinel')."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        campaign_app,
        ["dry-run", "--manifest", "-", "--engine", "static"],
        input=json.dumps(_atomic_manifest()),
    )
    assert result.exit_code == 0, result.output
    assert "atomic" in result.output.lower()
    assert not (tmp_path / "data").exists()


def test_start_seeds_the_ledger_tees_up_the_head_and_reflects_roadmap(tmp_path: Path) -> None:
    """from: §4 step 5 (start: seed_campaign + append_records + reflect ROADMAP; never launches)."""
    cdir = tmp_path / "campaign"
    roadmap = tmp_path / "ROADMAP.md"
    _start(CliRunner(), cdir, roadmap)

    state = load_campaign("PR-CLI", campaign_dir=cdir)
    statuses = {s.sub_scope_id: s.status for s in state.sub_scopes}
    assert statuses["PR-CLI-s1"] is CampaignStatus.READY  # deps-free head teed up
    assert statuses["PR-CLI-s2"] is CampaignStatus.PENDING  # gated behind s1
    # ROADMAP reflected the live state into the managed region.
    assert "**PR-CLI-s1**" in roadmap.read_text(encoding="utf-8")


def test_start_on_an_already_started_campaign_is_rejected(tmp_path: Path) -> None:
    """from: silent-1 (review) — re-running ``start`` on an existing campaign must FAIL CLOSED.

    A second seed would append a duplicate ``record_seq`` run (0..N-1 again) onto the ledger,
    tearing the append-only monotonic-seq invariant (locked #7). The re-run must be rejected and
    the ledger left byte-untouched, not silently appended to.
    """
    cdir = tmp_path / "campaign"
    runner = CliRunner()
    _start(runner, cdir, tmp_path / "ROADMAP.md")
    before = load_history("PR-CLI", campaign_dir=cdir)

    result = runner.invoke(
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
            str(tmp_path / "ROADMAP.md"),
        ],
        input=json.dumps(_split_manifest()),
    )
    assert result.exit_code != 0  # rejected, not a silent duplicate-seed append
    assert load_history("PR-CLI", campaign_dir=cdir) == before  # ledger untouched


def test_start_on_an_atomic_manifest_creates_no_campaign(tmp_path: Path) -> None:
    """from: §4 step 5 (atomic → echo sentinel + exit, no campaign)."""
    cdir = tmp_path / "campaign"
    roadmap = tmp_path / "ROADMAP.md"
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
        input=json.dumps(_atomic_manifest()),
    )
    assert result.exit_code == 0, result.output
    assert "atomic" in result.output.lower()
    assert not (cdir / "PR-ATOM.jsonl").exists()


def test_status_renders_the_current_view(tmp_path: Path) -> None:
    """from: §4 step 5 (status: load_campaign + format_campaign_status)."""
    cdir = tmp_path / "campaign"
    runner = CliRunner()
    _start(runner, cdir, tmp_path / "ROADMAP.md")
    result = runner.invoke(
        campaign_app, ["status", "--campaign", "PR-CLI", "--campaign-dir", str(cdir)]
    )
    assert result.exit_code == 0, result.output
    assert "PR-CLI-s1" in result.output
    assert "ready" in result.output


def test_resume_points_at_the_next_ready_sub_scope(tmp_path: Path) -> None:
    """from: §4 step 5 (resume: next_ready, advisory — 'run it via /scope-run')."""
    cdir = tmp_path / "campaign"
    runner = CliRunner()
    _start(runner, cdir, tmp_path / "ROADMAP.md")
    result = runner.invoke(
        campaign_app, ["resume", "--campaign", "PR-CLI", "--campaign-dir", str(cdir)]
    )
    assert result.exit_code == 0, result.output
    assert "PR-CLI-s1" in result.output
    assert "scope-run" in result.output  # advisory pointer, never an auto-launch


def test_cancel_ejects_all_active_and_is_append_only(tmp_path: Path) -> None:
    """from: refinement C (cancel: append terminal ejections, never truncate; reloads clean)."""
    cdir = tmp_path / "campaign"
    runner = CliRunner()
    _start(runner, cdir, tmp_path / "ROADMAP.md")
    before = len(load_history("PR-CLI", campaign_dir=cdir))

    result = runner.invoke(
        campaign_app, ["cancel", "--campaign", "PR-CLI", "--campaign-dir", str(cdir)]
    )
    assert result.exit_code == 0, result.output

    state = load_campaign("PR-CLI", campaign_dir=cdir)
    assert state.is_done()
    assert all(s.status is CampaignStatus.EJECTED for s in state.sub_scopes)
    assert len(load_history("PR-CLI", campaign_dir=cdir)) > before  # append-only, grew


def test_write_roadmap_reflects_then_is_a_noop(tmp_path: Path) -> None:
    """from: §4 step 5 (write-roadmap: read/transform/write-if-changed; byte-idempotent).

    Uses a FRESH ROADMAP (one ``start`` did not already reflect into) so the first write is a real
    change and the second is the byte-idempotent no-op.
    """
    cdir = tmp_path / "campaign"
    runner = CliRunner()
    _start(runner, cdir, tmp_path / "start_roadmap.md")
    fresh = tmp_path / "fresh_roadmap.md"
    fresh.write_text(_roadmap_with_region(), encoding="utf-8")

    first = runner.invoke(
        campaign_app,
        [
            "write-roadmap",
            "--campaign",
            "PR-CLI",
            "--campaign-dir",
            str(cdir),
            "--roadmap",
            str(fresh),
        ],
    )
    assert first.exit_code == 0, first.output
    assert "**PR-CLI-s1**" in fresh.read_text(encoding="utf-8")  # first write reflected the state

    again = runner.invoke(
        campaign_app,
        [
            "write-roadmap",
            "--campaign",
            "PR-CLI",
            "--campaign-dir",
            str(cdir),
            "--roadmap",
            str(fresh),
        ],
    )
    assert again.exit_code == 0, again.output
    assert "unchanged" in again.output.lower()  # byte-idempotent second write


def test_status_on_a_missing_campaign_is_clean(tmp_path: Path) -> None:
    """from: §4 step 5 (status of an absent campaign — empty current view, not a crash)."""
    result = CliRunner().invoke(
        campaign_app,
        ["status", "--campaign", "PR-NONE", "--campaign-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "PR-NONE" in result.output


def test_status_on_an_unsafe_campaign_id_is_a_clean_bad_parameter(tmp_path: Path) -> None:
    """from: convention review (nit) — an unsafe ``--campaign`` surfaces a clean BadParameter
    (exit 2, matching the ``--manifest`` precedent), not an unhandled ValueError traceback."""
    result = CliRunner().invoke(
        campaign_app,
        ["status", "--campaign", "../escape", "--campaign-dir", str(tmp_path)],
    )
    assert result.exit_code == 2  # typer BadParameter, not a raw-ValueError exit (1)
    assert not isinstance(result.exception, ValueError)  # the raw ValueError did not leak

"""CLI tests for the ``genome campaign`` LIVE-LAUNCH surface (PR 2; ``finding-041``).

Spec source: the approved PR-2 plan §4.1 (the four new commands ``revalidate`` / ``approve-plan``
/ ``record-merge`` / ``show``), §4.3 (the CLI-boundary re-split-child fold) and §5 (the plan-blind
RED→GREEN test list). PLAN-BLIND / TEST-FIRST: written from the spec + the FROZEN interface (the
command names, flags, decision vocabulary, and the persisted ledger/record shape), NOT from any
implementation — none of these commands exist yet, so this whole file is expected RED until the
live-launch wiring lands. We invoke ``campaign_app`` DIRECTLY (mirroring ``test_campaign_dry_run``;
the root ``genome`` app needs httpx in this env), with ``--campaign-dir`` / ``--roadmap`` redirected
to ``tmp_path`` so no test touches the real ``data/`` or ``ROADMAP.md``.

The two HEADLINE tests are the no-autonomous-gate guards (plan §3 constraint 3): without the
explicit ``--approved`` / ``--merged`` flag the command must reject the human-gate crossing AND
leave the append-only ledger BYTE-IDENTICAL (the BadParameter is raised before any append).
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

    from click.testing import Result

    from genome.campaign.model import CampaignState, SubScopeState


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """The campaign callback reconfigures structlog (stderr); reset it after each test."""
    try:
        yield
    finally:
        structlog.reset_defaults()


# ── Manifests + ROADMAP fixtures (per-file helpers, mirroring test_campaign_dry_run) ──


def _split_manifest(scope_id: str = "PR-CLI", *, title: str = "two-cluster") -> dict[str, object]:
    """A manifest the static engine splits into two ordered sub-scopes (schema before cli)."""
    return {
        "scope_id": scope_id,
        "title": title,
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


def _atomic_manifest(scope_id: str = "PR-ATOM") -> dict[str, object]:
    """A single-change-class manifest the splitter reports atomic (no re-split produced)."""
    return {
        "scope_id": scope_id,
        "title": "blob",
        "change_class": ["pipeline"],
        "blast_radius": {"imports_touched": ["genome.pipe.a"]},
        "applicable_anchors": [],
        "risk_breakdown": {"S": 5},
        "risk_tier": 2,
    }


def _roadmap_with_region() -> str:
    return f"# ROADMAP\n\nintro\n\n{BLOCK_BEGIN}\n{BLOCK_END}\n\noutro\n"


def _paths(home: Path) -> tuple[Path, Path]:
    """The (campaign_dir, roadmap) under a tmp home — mirrors the dry-run test's layout."""
    return home / "campaign", home / "ROADMAP.md"


# ── State + ledger readers (assert against the persisted ledger, not in-memory state) ──


def _load(home: Path) -> CampaignState:
    """Reduce the persisted PR-CLI campaign to its current view (reload-from-disk)."""
    return load_campaign("PR-CLI", campaign_dir=home / "campaign")


def _history(home: Path) -> list[SubScopeState]:
    """The full append-only PR-CLI ledger (oldest-first)."""
    return load_history("PR-CLI", campaign_dir=home / "campaign")


def _require(home: Path, sub: str) -> SubScopeState:
    """The active record for ``sub`` in the persisted PR-CLI campaign (fail if absent)."""
    record = _load(home).by_id(sub)
    assert record is not None, f"no active record for {sub!r}"
    return record


def _ledger_bytes(home: Path) -> bytes:
    """The raw bytes of the PR-CLI ledger file (``b''`` when it does not yet exist)."""
    cdir, _ = _paths(home)
    path = cdir / "PR-CLI.jsonl"
    return path.read_bytes() if path.exists() else b""


def _assert_command_registered(name: str) -> None:
    """The PR-2 live-launch command must be wired into ``campaign_app`` — RED until PR-2 lands.

    A structural probe on the FROZEN interface (the command names are the contract): ``<cmd>
    --help`` exits 0 for a registered command and exits non-zero ("No such command") while the
    command is absent, so this fires the right RED reason for the rejection tests below.
    """
    probe = CliRunner().invoke(campaign_app, [name, "--help"])
    assert probe.exit_code == 0, f"campaign command {name!r} is not registered yet: {probe.output}"


# ── Command invokers (separate CliRunner per call → each reloads the ledger from disk) ──


def _start(home: Path) -> None:
    """Seed the PR-CLI two-cluster campaign (s1 schema, s2 cli; s2 gated on s1)."""
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


def _revalidate(
    home: Path,
    sub: str,
    decision: str,
    *,
    manifest: str | None = None,
    input_text: str | None = None,
) -> Result:
    """Invoke ``campaign revalidate`` (static engine; ``--manifest`` only when supplied)."""
    cdir, roadmap = _paths(home)
    args = [
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
    ]
    if manifest is not None:
        args += ["--manifest", manifest]
    return CliRunner().invoke(campaign_app, args, input=input_text)


def _approve_plan(home: Path, sub: str, *, approved: bool) -> Result:
    """Invoke ``campaign approve-plan`` (Gate 1); ``--approved`` only when the flag is set."""
    cdir, roadmap = _paths(home)
    args = [
        "approve-plan",
        "--campaign",
        "PR-CLI",
        "--sub-scope",
        sub,
        "--campaign-dir",
        str(cdir),
        "--roadmap",
        str(roadmap),
    ]
    if approved:
        args.append("--approved")
    return CliRunner().invoke(campaign_app, args)


def _record_merge(home: Path, sub: str, *, merged: bool) -> Result:
    """Invoke ``campaign record-merge`` (Gate 2); ``--merged`` only when the flag is set."""
    cdir, roadmap = _paths(home)
    args = [
        "record-merge",
        "--campaign",
        "PR-CLI",
        "--sub-scope",
        sub,
        "--campaign-dir",
        str(cdir),
        "--roadmap",
        str(roadmap),
    ]
    if merged:
        args.append("--merged")
    return CliRunner().invoke(campaign_app, args)


def _show(home: Path, sub: str, *, as_json: bool = False) -> Result:
    """Invoke the read-only ``campaign show`` (``--json`` for machine output)."""
    cdir, _ = _paths(home)
    args = ["show", "--campaign", "PR-CLI", "--sub-scope", sub, "--campaign-dir", str(cdir)]
    if as_json:
        args.append("--json")
    return CliRunner().invoke(campaign_app, args)


# ── Gate 1 — approve-plan ─────────────────────────────────────────────────────


def test_approve_plan_with_approved_advances_to_implementing_and_reflects_roadmap(
    tmp_path: Path,
) -> None:
    """from: plan §5 / §4.1(b) — approve-plan --approved: PLANNING→IMPLEMENTING + ROADMAP."""
    _start(tmp_path)
    rev = _revalidate(tmp_path, "PR-CLI-s1", "still_needed")
    assert rev.exit_code == 0, rev.output  # READY → PLANNING precondition

    result = _approve_plan(tmp_path, "PR-CLI-s1", approved=True)
    assert result.exit_code == 0, result.output

    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.IMPLEMENTING
    roadmap = (tmp_path / "ROADMAP.md").read_text(encoding="utf-8")
    assert "**PR-CLI-s1** — implementing" in roadmap  # the live status is reflected


def test_approve_plan_without_approved_is_rejected_and_ledger_is_byte_identical(
    tmp_path: Path,
) -> None:
    """from: plan §5 HEADLINE Gate-1 / §3 constraint 3 / §4.1(b).

    Without --approved the PLANNING→IMPLEMENTING crossing (a GATE_CROSSINGS edge) needs an
    external event; the core refuses, the CLI surfaces a clean BadParameter, and the rejection
    is raised BEFORE any append, so the ledger is byte-untouched — the no-autonomous-gate guard.
    """
    _assert_command_registered("approve-plan")
    _start(tmp_path)
    assert _revalidate(tmp_path, "PR-CLI-s1", "still_needed").exit_code == 0
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.PLANNING  # at the Gate-1 edge

    before = _ledger_bytes(tmp_path)
    result = _approve_plan(tmp_path, "PR-CLI-s1", approved=False)
    assert result.exit_code != 0  # the gate is refused without the explicit flag
    assert _ledger_bytes(tmp_path) == before  # ledger byte-identical — no write on rejection
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.PLANNING  # not advanced


# ── Gate 2 — record-merge ─────────────────────────────────────────────────────


def test_record_merge_with_merged_advances_to_merged_and_tees_up_dependent(tmp_path: Path) -> None:
    """from: plan §5 / §4.1(c) — record-merge --merged → MERGED + dependent READY + ROADMAP."""
    _start(tmp_path)
    assert _revalidate(tmp_path, "PR-CLI-s1", "still_needed").exit_code == 0
    assert _approve_plan(tmp_path, "PR-CLI-s1", approved=True).exit_code == 0
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.IMPLEMENTING

    result = _record_merge(tmp_path, "PR-CLI-s1", merged=True)
    assert result.exit_code == 0, result.output

    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.MERGED
    assert _require(tmp_path, "PR-CLI-s2").status is CampaignStatus.READY  # dependent teed up
    roadmap = (tmp_path / "ROADMAP.md").read_text(encoding="utf-8")
    assert "- [x] **PR-CLI-s1** — merged" in roadmap


def test_record_merge_without_merged_is_rejected_and_ledger_is_byte_identical(
    tmp_path: Path,
) -> None:
    """from: plan §5 HEADLINE Gate-2 (GAP-C) / §4.1(c).

    GAP-C: advance_on_merge hard-codes external_event=True, so the CLI ``if not merged: raise
    BadParameter`` is the SOLE structural enforcer of Gate 2. Without --merged the command must
    reject before any append — ledger byte-identical, sub-scope still IMPLEMENTING.
    """
    _assert_command_registered("record-merge")
    _start(tmp_path)
    assert _revalidate(tmp_path, "PR-CLI-s1", "still_needed").exit_code == 0
    assert _approve_plan(tmp_path, "PR-CLI-s1", approved=True).exit_code == 0
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.IMPLEMENTING  # the Gate-2 edge

    before = _ledger_bytes(tmp_path)
    result = _record_merge(tmp_path, "PR-CLI-s1", merged=False)
    assert result.exit_code != 0
    assert _ledger_bytes(tmp_path) == before
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.IMPLEMENTING  # not advanced


def test_record_merge_on_a_non_implementing_sub_scope_is_rejected(tmp_path: Path) -> None:
    """from: plan §5 / §4.1(c) — record-merge on a non-IMPLEMENTING sub-scope → clean BadParameter.

    A freshly-seeded head is READY, not IMPLEMENTING; advance_on_merge's transition rejects the
    illegal READY→MERGED edge → the CLI surfaces a clean BadParameter (not a raw traceback).
    """
    _assert_command_registered("record-merge")
    _start(tmp_path)
    before = _ledger_bytes(tmp_path)
    result = _record_merge(tmp_path, "PR-CLI-s1", merged=True)  # s1 is READY, not IMPLEMENTING
    assert result.exit_code != 0
    assert not isinstance(result.exception, (KeyError, ValueError))  # clean BadParameter
    assert _ledger_bytes(tmp_path) == before


# ── revalidate — still_needed / moot / changed ────────────────────────────────


def test_revalidate_still_needed_moves_a_ready_sub_scope_to_planning(tmp_path: Path) -> None:
    """from: plan §5 / §4.1(a) — still_needed: READY→PLANNING (the tee-up→planning event)."""
    _start(tmp_path)
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.READY
    result = _revalidate(tmp_path, "PR-CLI-s1", "still_needed")
    assert result.exit_code == 0, result.output
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.PLANNING


def test_revalidate_moot_resolves_and_tees_up_the_gated_dependent(tmp_path: Path) -> None:
    """from: plan §5 / §4.1(a) — moot: READY→MOOT AND the bundled tee_up unblocks the dependent.

    apply_revalidation MOOT does not tee up on its own; the CLI bundles tee_up over the verdict in
    the same write, so s2 (gated solely on s1) flips PENDING→READY in the SAME run.
    """
    _start(tmp_path)
    assert _require(tmp_path, "PR-CLI-s2").status is CampaignStatus.PENDING  # gated behind s1
    result = _revalidate(tmp_path, "PR-CLI-s1", "moot")
    assert result.exit_code == 0, result.output
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.MOOT
    assert _require(tmp_path, "PR-CLI-s2").status is CampaignStatus.READY  # tee_up unblocked it


def test_revalidate_changed_with_a_manifest_keeps_ready_and_swaps_the_snapshot(
    tmp_path: Path,
) -> None:
    """from: plan §5 / §4.1(a) — changed --manifest -: stays READY, new manifest_snapshot."""
    _start(tmp_path)
    before = dict(_require(tmp_path, "PR-CLI-s1").manifest_snapshot)
    fed = json.dumps(_split_manifest("PR-CHANGED", title="re-proposed snapshot"))
    result = _revalidate(tmp_path, "PR-CLI-s1", "changed", manifest="-", input_text=fed)
    assert result.exit_code == 0, result.output

    s1 = _require(tmp_path, "PR-CLI-s1")
    assert s1.status is CampaignStatus.READY  # changed is a content-only supersession
    assert dict(s1.manifest_snapshot) != before  # the snapshot was re-proposed
    assert s1.manifest_snapshot.get("title") == "re-proposed snapshot"  # from the fed manifest


def test_revalidate_changed_without_a_manifest_is_rejected(tmp_path: Path) -> None:
    """from: plan §5 / §4.1(a) — changed with no --manifest is nonsensical → clean BadParameter."""
    _assert_command_registered("revalidate")
    _start(tmp_path)
    before = _ledger_bytes(tmp_path)
    result = _revalidate(tmp_path, "PR-CLI-s1", "changed")  # no --manifest
    assert result.exit_code != 0
    assert _ledger_bytes(tmp_path) == before
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.READY  # unchanged


# ── revalidate — grown (re-split / eject-loud / cap) ───────────────────────────


def test_revalidate_grown_with_a_nonatomic_resplit_ejects_and_seeds_depth_plus_one_children(
    tmp_path: Path,
) -> None:
    """from: plan §5 / §4.1(a) — grown within cap: eject original, seed children at depth+1."""
    _start(tmp_path)
    fed = json.dumps(_split_manifest("PR-GROW"))  # static engine re-splits this into 2 children
    result = _revalidate(tmp_path, "PR-CLI-s1", "grown", manifest="-", input_text=fed)
    assert result.exit_code == 0, result.output

    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.EJECTED  # original carved away
    children = [r for r in _history(tmp_path) if r.sub_scope_id.startswith("PR-GROW")]
    assert children  # new child records were seeded
    assert all(r.resplit_depth == 1 for r in children)  # at resplit_depth + 1


def test_revalidate_grown_with_an_atomic_manifest_ejects_loud_with_a_note(tmp_path: Path) -> None:
    """from: plan §5 / §4.1(a) — grown but the re-split is atomic → eject-loud, no children."""
    _start(tmp_path)
    fed = json.dumps(_atomic_manifest("PR-ATOM"))  # propose_split → atomic → no resplit_children
    result = _revalidate(tmp_path, "PR-CLI-s1", "grown", manifest="-", input_text=fed)
    assert result.exit_code == 0, result.output

    s1 = _require(tmp_path, "PR-CLI-s1")
    assert s1.status is CampaignStatus.EJECTED
    assert s1.note  # loud — a non-empty escalation note, never a silent drop
    assert "escalat" in s1.note.lower()


def test_revalidate_grown_past_the_cap_ejects_loud_with_a_cap_note(tmp_path: Path) -> None:
    """from: plan §5 / §4.1(a) — a second grow on a depth-1 child hits the cap → eject 'cap'."""
    _start(tmp_path)
    # First grow: PR-CLI-s1 (depth 0) → ejected, children seeded at depth 1, the head teed up.
    first = _revalidate(
        tmp_path,
        "PR-CLI-s1",
        "grown",
        manifest="-",
        input_text=json.dumps(_split_manifest("PR-GROW")),
    )
    assert first.exit_code == 0, first.output
    assert _require(tmp_path, "PR-GROW-s1").status is CampaignStatus.READY  # depth-1 head is ready

    # Second grow on the depth-1 child → at the cap → eject-loud, no further carve.
    second = _revalidate(
        tmp_path,
        "PR-GROW-s1",
        "grown",
        manifest="-",
        input_text=json.dumps(_split_manifest("PR-GROW2")),
    )
    assert second.exit_code == 0, second.output
    child = _require(tmp_path, "PR-GROW-s1")
    assert child.status is CampaignStatus.EJECTED
    assert "cap" in child.note.lower()  # the re-split cap escalation


# ── revalidate — precondition + §4.3 fold + unknown sub-scope ──────────────────


def test_revalidate_on_a_non_ready_sub_scope_is_rejected_and_ledger_unchanged(
    tmp_path: Path,
) -> None:
    """from: plan §5 / §4.1(a) — re-validation is a READY-stage gate; non-READY rejects."""
    _assert_command_registered("revalidate")
    _start(tmp_path)
    assert _revalidate(tmp_path, "PR-CLI-s1", "still_needed").exit_code == 0
    assert _approve_plan(tmp_path, "PR-CLI-s1", approved=True).exit_code == 0
    assert _require(tmp_path, "PR-CLI-s1").status is CampaignStatus.IMPLEMENTING  # non-READY now

    before = _ledger_bytes(tmp_path)
    result = _revalidate(tmp_path, "PR-CLI-s1", "still_needed")  # re-validate a non-READY sub-scope
    assert result.exit_code != 0
    assert not isinstance(result.exception, (KeyError, ValueError))  # clean BadParameter
    assert _ledger_bytes(tmp_path) == before


def test_revalidate_grown_with_a_colliding_resplit_child_is_rejected_no_write(
    tmp_path: Path,
) -> None:
    """from: plan §5 / §4.3 fold — a colliding re-split child is rejected, no write.

    Growing with the campaign's OWN scope_id makes propose_split emit children whose ids
    (PR-CLI-s1, PR-CLI-s2) collide with active members → the §4.3 CLI guard rejects before any
    append. (The sibling "dangling depends_on" half of the §4.3 fold is the SAME guard but is not
    constructible through this CLI — propose_split always chains siblings, never emits a dangling
    dep — so collision is the CLI-reachable trigger for the fold. See the report's spec note.)
    """
    _assert_command_registered("revalidate")
    _start(tmp_path)
    before = _ledger_bytes(tmp_path)
    fed = json.dumps(_split_manifest("PR-CLI"))  # children collide with active PR-CLI-s* members
    result = _revalidate(tmp_path, "PR-CLI-s1", "grown", manifest="-", input_text=fed)
    assert result.exit_code != 0  # collision rejected
    assert _ledger_bytes(tmp_path) == before  # no write


def test_unknown_sub_scope_is_a_clean_bad_parameter_on_every_gate_command(tmp_path: Path) -> None:
    """from: plan §5 — an unknown --sub-scope on any gate command → clean BadParameter.

    The core ``_current_record`` raises ValueError for an unknown sub-scope; every command must
    wrap it as a clean BadParameter (non-zero exit, no raw KeyError/ValueError leaking out).
    """
    for name in ("revalidate", "approve-plan", "record-merge"):
        _assert_command_registered(name)
    _start(tmp_path)
    results = [
        _revalidate(tmp_path, "PR-CLI-sX", "still_needed"),
        _approve_plan(tmp_path, "PR-CLI-sX", approved=True),
        _record_merge(tmp_path, "PR-CLI-sX", merged=True),
    ]
    for result in results:
        assert result.exit_code != 0
        assert not isinstance(result.exception, (KeyError, ValueError))  # not a raw traceback


# ── show --json (the GAP-A conductor seam) ────────────────────────────────────


def test_show_json_emits_the_active_records_manifest_snapshot_and_status(tmp_path: Path) -> None:
    """from: plan §5 / §4.1(d) — show --json emits the active record (snapshot + status)."""
    _start(tmp_path)
    result = _show(tmp_path, "PR-CLI-s1", as_json=True)
    assert result.exit_code == 0, result.output

    parsed = json.loads(result.output)
    assert isinstance(parsed, dict)
    assert parsed.get("status") == "ready"  # the active record's live status
    snapshot = parsed.get("manifest_snapshot")
    assert isinstance(snapshot, dict)
    assert snapshot  # the non-empty mini-manifest
    assert snapshot.get("sub_scope_id") == "PR-CLI-s1"  # the right sub-scope's snapshot

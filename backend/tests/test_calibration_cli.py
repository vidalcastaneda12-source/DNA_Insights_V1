"""``genome calibrate`` CLI seam — pure JSON stdout, persist gating, dry-run inertness.

from: §5 test #14 (test_calibration_cli.py) + §6:
  * ``compute-tier --manifest -`` emits ``{"tier":int,"breakdown":{...}}`` JSON on stdout (pure;
    structlog goes to stderr);
  * ``--persist`` writes ``data/calibration/manifests/<scope_id>.json``; ``--no-persist`` does not;
  * ``write-outcome`` with a malformed ``--actual-json`` exits non-zero with NO append;
  * ``ratchet --dry-run`` exits 0, leaves ``git status`` unchanged AND ``risk_weights.json``
    byte-identical (hash before == after);
  * ``show-weights --json`` is valid JSON with no ``floor`` key.

We invoke ``calibration_app`` DIRECTLY (the root ``genome`` app needs httpx in this env) via
CliRunner. Every command body is STUBBED -> each test asserts the SPECIFIED behavior and adds
``assert not isinstance(result.exception, NotImplementedError)`` so it is honestly RED on the stub
now and GREEN when the body lands — never ``pytest.raises(NotImplementedError)``. The dry-run
inertness is asserted by before/after byte-identity (robust whether the weights file is tracked
yet), the faithful expression of "changes nothing".

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

import _calibration_ledger as cl
import genome.calibration.cli as cli_mod
from genome.calibration.cli import calibration_app
from genome.calibration.commit_plan import WEIGHTS_PATHSPEC
from genome.calibration.model import (
    SEED_RISK_WEIGHTS,
    AuditRow,
    Direction,
    Disposition,
    RatchetDecision,
    RiskWeights,
)
from genome.calibration.persistence import DEFAULT_WEIGHTS_PATH
from genome.calibration.ratchet import nontarget_knobs_unchanged

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


def _repo_root() -> Path:
    """The repo root (walk up from this test file to the directory holding CLAUDE.md)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "CLAUDE.md").is_file():
            return parent
    msg = "could not locate repo root"
    raise AssertionError(msg)


def _manifest_json() -> str:
    """A PR-6-shaped TierFields manifest (data-backfill, 3 imports, clean) -> Tier 1 under seed."""
    return json.dumps(
        {
            "change_class": ["data-backfill"],
            "imports_touched_count": 3,
            "precedent_surprise": "clean",
            "applicable_anchors_count": 0,
        }
    )


def test_compute_tier_emits_pure_tier_breakdown_json_on_stdout() -> None:
    """from: §6 (compute-tier emits {"tier":int,"breakdown":{...}} JSON on stdout).

    The dispatcher RUNS this and consumes the returned object as THE tier. stdout is pure JSON
    (logs go to stderr): a PR-6-shaped manifest scores Tier 1 with a breakdown object.
    """
    result = CliRunner().invoke(
        calibration_app, ["compute-tier", "--manifest", "-"], input=_manifest_json()
    )
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    payload = json.loads(result.stdout)
    assert payload["tier"] == 1
    assert isinstance(payload["breakdown"], dict)


def test_compute_tier_persist_writes_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from: §6 (--persist writes data/calibration/manifests/<scope_id>.json).

    With ``--persist`` the dispatch-time predicted manifest is written under the runtime ``data/``
    home (cwd-relative), feeding A's close hook.
    """
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        calibration_app,
        ["compute-tier", "--manifest", "-", "--scope-id", "PR-X", "--persist"],
        input=_manifest_json(),
    )
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    assert (tmp_path / "data" / "calibration" / "manifests" / "PR-X.json").is_file()


def test_compute_tier_no_persist_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from: §6 (--no-persist does not write the manifest).

    Off by default: an ad-hoc compute-tier never clobbers a dispatch store.
    """
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        calibration_app,
        ["compute-tier", "--manifest", "-", "--scope-id", "PR-X", "--no-persist"],
        input=_manifest_json(),
    )
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    assert not (tmp_path / "data" / "calibration" / "manifests" / "PR-X.json").exists()


def test_write_outcome_malformed_actual_json_exits_nonzero_without_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from: §6 (write-outcome with a malformed --actual-json exits non-zero with NO append).

    A non-JSON ACTUAL payload is a clean BadParameter (non-zero), and nothing is appended to the
    ledger — a malformed close never leaves a corrupt/partial row.
    """
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        calibration_app,
        ["write-outcome", "--scope-id", "PR-X", "--actual-json", "-"],
        input="this is not json",
    )
    assert result.exit_code != 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    assert not (tmp_path / "data" / "calibration" / "outcomes.jsonl").exists()


def test_ratchet_dry_run_is_inert() -> None:
    """from: §6 (ratchet --dry-run exits 0, leaves git status unchanged AND weights byte-identical).

    ``--dry-run`` (the default) computes the decision and changes nothing: the git-tracked
    ``risk_weights.json`` is byte-identical (hash before == after) and its git status is unchanged
    — the Python core never runs git, and the seed always NO_OPs (auto-tuning disabled).
    """
    repo_root = _repo_root()

    def weights_hash() -> str:
        return hashlib.sha256(DEFAULT_WEIGHTS_PATH.read_bytes()).hexdigest()

    def scoped_git_status() -> str:
        return subprocess.run(  # noqa: S603 — fixed argv, scoped to the one weights pathspec
            ["git", "status", "--porcelain", "--", WEIGHTS_PATHSPEC],  # noqa: S607
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        ).stdout

    hash_before = weights_hash()
    status_before = scoped_git_status()

    result = CliRunner().invoke(calibration_app, ["ratchet", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception

    assert weights_hash() == hash_before, "dry-run mutated risk_weights.json"
    assert scoped_git_status() == status_before, "dry-run changed git status"


def test_show_weights_json_is_valid_and_has_no_floor_key() -> None:
    """from: §6 (show-weights --json is valid JSON with no floor key).

    The machine-readable weights are pure JSON on stdout and carry no ``floor`` key — the floor is
    immutable and not representable in the tunable config.
    """
    result = CliRunner().invoke(calibration_app, ["show-weights", "--json"])
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert "floor" not in payload
    assert "c_map" in payload


# ── Stage-3 G/F: compute-tier --persist requires --scope-id (no manifest on the error path) ──


def test_compute_tier_persist_without_scope_id_exits_nonzero_and_writes_no_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from: Stage-3 G/F (--persist without --scope-id -> non-zero exit, no manifest written).

    ``--persist`` needs a filename stem; omitting ``--scope-id`` is a clean BadParameter, and the
    manifest store is never created.
    """
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        calibration_app, ["compute-tier", "--manifest", "-", "--persist"], input=_manifest_json()
    )
    assert result.exit_code != 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    assert not (tmp_path / "data" / "calibration" / "manifests").exists()


# ── Stage-3 F/E: ratchet --apply write paths (monkeypatched so no real file is mutated) ──


def _capture_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[AuditRow], list[RiskWeights]]:
    """Patch the cli's write_weights / append_audit to capture order + payloads (no real file)."""
    events: list[str] = []
    audit_rows: list[AuditRow] = []
    written: list[RiskWeights] = []

    def _write(weights: RiskWeights) -> None:
        events.append("write")
        written.append(weights)

    def _audit(row: AuditRow) -> None:
        events.append("audit")
        audit_rows.append(row)

    monkeypatch.setattr(cli_mod, "write_weights", _write)
    monkeypatch.setattr(cli_mod, "append_audit", _audit)
    return events, audit_rows, written


def test_ratchet_apply_auto_commit_audits_before_write_and_emits_commit_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: Stage-3 F (ratchet --apply AUTO_COMMIT) + E silent-3 (audit BEFORE the weights mutate).

    With enabled weights and a covered-tighten ledger the ratchet AUTO_COMMITs: the audit row is
    appended (applied=True) BEFORE write_weights, and the pathspec-scoped CommitPlan JSON is emitted
    for the skill.
    """
    monkeypatch.setattr(cli_mod, "read_weights", _const(cl.enabled()))
    monkeypatch.setattr(
        cli_mod, "load_outcomes", _const(cl.ledger(12, 1, cl.breakdown(c=1), cl.actual_blocked()))
    )
    events, audit_rows, written = _capture_writes(monkeypatch)

    # --merges-since-last clears the cadence gate (default 0 would NO_OP before the apply path).
    result = CliRunner().invoke(calibration_app, ["ratchet", "--apply", "--merges-since-last", "5"])
    assert result.exit_code == 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception
    assert events == ["audit", "write"]  # silent-3: audit precedes the mutation
    assert len(written) == 1
    assert audit_rows[0].applied is True
    assert "argv_commit" in result.stdout
    assert WEIGHTS_PATHSPEC in result.stdout


def test_ratchet_apply_park_appends_unapplied_audit_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: Stage-3 F (the PARK audit-append path — uncovered tighten parks, no write)."""
    monkeypatch.setattr(cli_mod, "read_weights", _const(cl.enabled()))
    monkeypatch.setattr(
        cli_mod, "load_outcomes", _const(cl.ledger(12, 1, cl.breakdown(c=3), cl.actual_blocked()))
    )
    events, audit_rows, written = _capture_writes(monkeypatch)

    result = CliRunner().invoke(calibration_app, ["ratchet", "--apply", "--merges-since-last", "5"])
    assert result.exit_code == 0, result.output
    assert written == []  # no weights mutation on a PARK
    assert events == ["audit"]
    assert audit_rows[0].applied is False


# ── Stage-3 F/E: apply-parked (no-parked, dry-run, clean-apply, dirty reject, direction reject) ──


def _clean_tighten_candidate() -> RiskWeights:
    """A clean tighten candidate (raise c_map.pipeline) — flips no frozen back-test row."""
    return dataclasses.replace(
        cl.enabled(),
        c_map={**SEED_RISK_WEIGHTS.c_map, "pipeline": 4},
        weights_version="rw-2",
    )


def _parked_row(candidate: RiskWeights, direction: Direction) -> AuditRow:
    """A parked (applied=False) audit row carrying ``candidate`` and its recorded ``direction``."""
    decision = RatchetDecision(
        disposition=Disposition.PARK_FOR_APPROVAL,
        knob="c_map.pipeline",
        direction=direction,
        candidate_weights=candidate,
        backtest_clean=True,
        knob_covered=False,
        cited_merged_shas=("sha-a",),
        rationale="parked for human approval",
        auto_applicable=False,
    )
    return AuditRow(date="2026-06-25", applied=False, decision=decision)


def test_apply_parked_with_no_parked_items_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """from: Stage-3 F (apply-parked with nothing parked changes nothing)."""
    monkeypatch.setattr(cli_mod, "load_audit", _const([]))
    monkeypatch.setattr(cli_mod, "read_weights", _const(cl.enabled()))
    events, _audit, written = _capture_writes(monkeypatch)

    result = CliRunner().invoke(calibration_app, ["apply-parked", "--apply"])
    assert result.exit_code == 0, result.output
    assert "no parked decision" in result.stdout
    assert events == []
    assert written == []


def test_apply_parked_dry_run_rechecks_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """from: Stage-3 F (apply-parked --dry-run re-checks the parked candidate, writes nothing)."""
    parked = _parked_row(_clean_tighten_candidate(), Direction.TIGHTEN)
    monkeypatch.setattr(cli_mod, "load_audit", _const([parked]))
    monkeypatch.setattr(cli_mod, "read_weights", _const(cl.enabled()))
    events, _audit, written = _capture_writes(monkeypatch)

    result = CliRunner().invoke(calibration_app, ["apply-parked"])  # --dry-run is the default
    assert result.exit_code == 0, result.output
    assert "re-check" in result.stdout
    assert events == []
    assert written == []


def test_apply_parked_apply_clean_audits_before_write_and_emits_commit_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: Stage-3 F (apply-parked --apply clean) + E (audit before write; direction holds)."""
    parked = _parked_row(_clean_tighten_candidate(), Direction.TIGHTEN)
    monkeypatch.setattr(cli_mod, "load_audit", _const([parked]))
    monkeypatch.setattr(cli_mod, "read_weights", _const(cl.enabled()))
    events, audit_rows, written = _capture_writes(monkeypatch)

    result = CliRunner().invoke(calibration_app, ["apply-parked", "--apply"])
    assert result.exit_code == 0, result.output
    assert events == ["audit", "write"]  # silent-3 on the human-approval path too
    assert audit_rows[0].applied is True
    assert audit_rows[0].decision.disposition is Disposition.AUTO_COMMIT
    assert len(written) == 1
    assert "argv_commit" in result.stdout


def test_apply_parked_apply_rejects_a_now_backtest_dirty_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: Stage-3 F (the TOCTOU back-test re-check) — a now-flipping candidate is refused."""
    dirty = dataclasses.replace(
        cl.enabled(),
        b_buckets={**SEED_RISK_WEIGHTS.b_buckets, "small": 2},  # raising small flips PR-7
        weights_version="rw-2",
    )
    monkeypatch.setattr(cli_mod, "load_audit", _const([_parked_row(dirty, Direction.TIGHTEN)]))
    monkeypatch.setattr(cli_mod, "read_weights", _const(cl.enabled()))
    events, _audit, written = _capture_writes(monkeypatch)

    result = CliRunner().invoke(calibration_app, ["apply-parked", "--apply"])
    assert result.exit_code == 0, result.output
    assert "flips a back-test row" in result.stdout
    assert events == []
    assert written == []


def test_apply_parked_apply_rejects_when_direction_changed_since_park(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: Stage-3 E (the real direction re-check) — a parked TIGHTEN that now LOOSENs is refused.

    The candidate (lower b_buckets.small) is back-test-clean but classifies as a LOOSEN against the
    current live weights, while the row was parked as a TIGHTEN; the direction re-check aborts the
    apply rather than silently applying an un-approved loosen.
    """
    loosen_candidate = dataclasses.replace(
        cl.enabled(),
        b_buckets={**SEED_RISK_WEIGHTS.b_buckets, "small": 0},  # lowering small loosens
        weights_version="rw-2",
    )
    parked = _parked_row(loosen_candidate, Direction.TIGHTEN)
    monkeypatch.setattr(cli_mod, "load_audit", _const([parked]))
    monkeypatch.setattr(cli_mod, "read_weights", _const(cl.enabled()))
    events, _audit, written = _capture_writes(monkeypatch)

    result = CliRunner().invoke(calibration_app, ["apply-parked", "--apply"])
    assert result.exit_code == 0, result.output
    assert "direction changed since park" in result.stdout
    assert events == []
    assert written == []


# ── PR-1 pre-enablement must-fixes: apply-parked kill-switch (FIX-3), consumption (FIX-2),
#    lost-update (FIX-1). All are unreachable while dark (no PARK row exists), so these prove the
#    ACTIVATED write path is correct. ──


def test_apply_parked_honors_the_kill_switch_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """from: finding-040 FIX-3 (apply-parked HONORS auto_tuning_enabled — VSC-User Gate-1 decision).

    With a parked decision but the live config's kill switch OFF (reachable only via
    toggle-off-after-park), the one-click apply REFUSES to write — "no weight write until signoff".
    The kill switch re-freezes the human-approval path, not just the automatic ratchet.
    """
    parked = _parked_row(_clean_tighten_candidate(), Direction.TIGHTEN)
    monkeypatch.setattr(cli_mod, "load_audit", _const([parked]))
    # SEED_RISK_WEIGHTS ships auto_tuning_enabled=False (the dark/disabled live config).
    monkeypatch.setattr(cli_mod, "read_weights", _const(SEED_RISK_WEIGHTS))
    events, _audit, written = _capture_writes(monkeypatch)

    result = CliRunner().invoke(calibration_app, ["apply-parked", "--apply"])
    assert result.exit_code == 0, result.output
    assert "kill switch off" in result.stdout
    assert events == []
    assert written == []


def test_apply_parked_consumes_the_parked_row_so_it_is_not_re_appliable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: finding-040 FIX-2 (an approved parked row is consumed — insert-then-supersede).

    Once an ``applied=True`` row carrying the same candidate exists, the parked row is consumed:
    ``apply-parked`` finds nothing pending (no duplicate write / duplicate CommitPlan / empty
    re-commit), proving the approval retires the PARK row without an in-place edit.
    """
    candidate = _clean_tighten_candidate()
    parked = _parked_row(candidate, Direction.TIGHTEN)
    approved = AuditRow(
        date="2026-06-26",
        applied=True,
        decision=dataclasses.replace(
            parked.decision, disposition=Disposition.AUTO_COMMIT, auto_applicable=True
        ),
    )
    monkeypatch.setattr(cli_mod, "load_audit", _const([parked, approved]))
    monkeypatch.setattr(cli_mod, "read_weights", _const(cl.enabled()))
    events, _audit, written = _capture_writes(monkeypatch)

    result = CliRunner().invoke(calibration_app, ["apply-parked", "--apply"])
    assert result.exit_code == 0, result.output
    assert "no parked decision" in result.stdout
    assert events == []
    assert written == []


def test_apply_parked_rejects_a_stale_snapshot_lost_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: finding-040 FIX-1 (the stale full-snapshot apply / lost-update guard).

    The parked candidate is a clean-by-vacuity ``c_map.pipeline`` tighten built on the seed; an
    intervening auto-commit then moved a DIFFERENT knob (``c_map.cli`` 1→2) in live. The candidate
    is still back-test-clean AND its direction is still TIGHTEN, so it slips both existing TOCTOU
    re-checks — ONLY the non-target-knob guard catches that writing the stale snapshot would revert
    the ``c_map.cli`` move. The apply is refused, nothing written.
    """
    parked = _parked_row(_clean_tighten_candidate(), Direction.TIGHTEN)  # knob = c_map.pipeline
    live = dataclasses.replace(
        cl.enabled(),
        c_map={**SEED_RISK_WEIGHTS.c_map, "cli": 2},  # the intervening different-knob auto-commit
        weights_version="rw-2",
    )
    monkeypatch.setattr(cli_mod, "load_audit", _const([parked]))
    monkeypatch.setattr(cli_mod, "read_weights", _const(live))
    events, _audit, written = _capture_writes(monkeypatch)

    result = CliRunner().invoke(calibration_app, ["apply-parked", "--apply"])
    assert result.exit_code == 0, result.output
    assert "moved on another knob" in result.stdout
    assert events == []
    assert written == []


def test_nontarget_knobs_unchanged_true_on_a_pure_one_knob_delta() -> None:
    """from: finding-040 FIX-1 unit (the lost-update guard passes a faithful one-knob delta).

    A candidate that differs from live on ONLY the target knob is a clean one-step delta — the
    guard returns ``True`` (the apply may proceed).
    """
    candidate = dataclasses.replace(
        SEED_RISK_WEIGHTS, c_map={**SEED_RISK_WEIGHTS.c_map, "tests": 2}
    )
    assert nontarget_knobs_unchanged(SEED_RISK_WEIGHTS, candidate, "c_map.tests") is True


def test_nontarget_knobs_unchanged_false_when_a_second_knob_diverged() -> None:
    """from: finding-040 FIX-1 unit (the guard catches a second, non-target knob divergence).

    A candidate that also differs on a non-target knob is a stale snapshot — the guard returns
    ``False`` (refuse the apply rather than silently revert the other knob).
    """
    candidate = dataclasses.replace(
        SEED_RISK_WEIGHTS, c_map={**SEED_RISK_WEIGHTS.c_map, "tests": 2, "cli": 2}
    )
    assert nontarget_knobs_unchanged(SEED_RISK_WEIGHTS, candidate, "c_map.tests") is False

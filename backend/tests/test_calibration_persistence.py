"""Append-only ledger / audit / manifest I/O + the default-path contract (mirrors fast_follow).

from: §5 test #12 (test_calibration_persistence.py) + §6:
  * outcomes / audit JSONL append-only round-trip;
  * load returns EMPTY on an absent file (first run);
  * a malformed line RAISES (no silent-empty — losing history is the unsafe direction);
  * write_manifest / read_manifest round-trip; read_manifest absent -> None;
  * DEFAULT_LEDGER / AUDIT / MANIFEST paths under ``data/`` (NOT ``archive/``);
  * DEFAULT_WEIGHTS_PATH is the package-relative git-tracked file.

Every write routes through ``tmp_path`` — never the real ``data/``. The default-path assertions
are GREEN from freeze (the constants exist); the I/O round-trips are RED until the bodies land.

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

from genome.calibration.model import (
    SEED_RISK_WEIGHTS,
    ActualBlock,
    AuditRow,
    Direction,
    Disposition,
    OutcomeRecord,
    PredictedBlock,
    PredictedManifest,
    RatchetDecision,
    RiskWeights,
    TierBreakdown,
)
from genome.calibration.persistence import (
    DEFAULT_AUDIT_PATH,
    DEFAULT_LEDGER_PATH,
    DEFAULT_MANIFEST_DIR,
    DEFAULT_WEIGHTS_PATH,
    append_audit,
    append_outcome,
    load_audit,
    load_outcomes,
    pending_parked,
    read_manifest,
    read_weights,
    write_manifest,
    write_weights,
)

if TYPE_CHECKING:
    from pathlib import Path


def _predicted(tier: int = 1) -> PredictedBlock:
    """A realistic dispatch-time prediction block."""
    return PredictedBlock(
        tier=tier,
        breakdown=TierBreakdown(c=2, b=1, p=1, a=0, s=4, floor=0, deep_t2=False),
        premortem_surprises=("anchor drift",),
        anchors_to_watch=("gnomad_matches",),
    )


def _outcome(scope_id: str) -> OutcomeRecord:
    """A realistic outcome-ledger datum for ``scope_id``."""
    return OutcomeRecord(
        scope_id=scope_id,
        merged_sha=f"sha-{scope_id}",
        date="2026-06-25",
        risk_weights_version="rw-1",
        predicted=_predicted(),
        actual=ActualBlock(gate_verdict="pass"),
    )


def _audit_row() -> AuditRow:
    """A realistic parked-loosen audit row (the kind appended on a ratchet pass)."""
    candidate: RiskWeights = dataclasses.replace(SEED_RISK_WEIGHTS, weights_version="rw-2")
    decision = RatchetDecision(
        disposition=Disposition.PARK_FOR_APPROVAL,
        knob="c_map.pipeline",
        direction=Direction.LOOSEN,
        candidate_weights=candidate,
        backtest_clean=True,
        knob_covered=False,
        cited_merged_shas=("sha-a", "sha-b"),
        rationale="parked: clean-by-vacuity tighten needs human approval",
        auto_applicable=False,
    )
    return AuditRow(date="2026-06-25", applied=False, decision=decision)


# ── outcomes JSONL append-only round-trip ─────────────────────────────────────


def test_append_then_load_outcomes_round_trips(tmp_path: Path) -> None:
    """from: §6 (outcomes JSONL append-only round-trip).

    A record appended to a tmp ledger loads back identical — the on-disk seam preserves the
    datum exactly (tuples restored as tuples through the nested blocks).
    """
    ledger = tmp_path / "calibration" / "outcomes.jsonl"
    record = _outcome("PR-7")
    append_outcome(record, ledger)
    assert load_outcomes(ledger) == [record]


def test_append_outcomes_is_append_only_and_ordered(tmp_path: Path) -> None:
    """from: §6 (append-only — never rewrites/truncates).

    Two appends across "runs" leave both records, in insertion order — the ledger grows, never
    clobbers prior history.
    """
    ledger = tmp_path / "calibration" / "outcomes.jsonl"
    first, second = _outcome("PR-6"), _outcome("PR-7")
    append_outcome(first, ledger)
    append_outcome(second, ledger)
    assert load_outcomes(ledger) == [first, second]


def test_load_outcomes_missing_file_is_empty(tmp_path: Path) -> None:
    """from: §6 (load returns EMPTY on an absent file — first run).

    The very first dispatch has no ledger yet; loading an absent path yields an empty list, not
    an error.
    """
    assert load_outcomes(tmp_path / "nope.jsonl") == []


def test_load_outcomes_malformed_line_raises(tmp_path: Path) -> None:
    """from: §6 (a malformed line RAISES — no silent-empty).

    A line that is missing the required ``risk_weights_version`` is a malformed record; the
    loader fails loud (ValueError via OutcomeRecord.from_json) rather than silently dropping
    history (which would let the ratchet reduce a truncated ledger).
    """
    ledger = tmp_path / "outcomes.jsonl"
    ledger.write_text(
        '{"scope_id": "PR-7", "merged_sha": "x", "date": "2026-06-25"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="risk_weights_version"):
        load_outcomes(ledger)


# ── audit JSONL append-only round-trip ────────────────────────────────────────


def test_append_then_load_audit_round_trips_key_fields(tmp_path: Path) -> None:
    """from: §6 (audit JSONL append-only round-trip).

    An audit row appended to a tmp log loads back with its disposition / knob / direction /
    applied flag / cited SHAs intact — the reviewable-and-revertible audit trail survives the
    serialization seam.
    """
    audit = tmp_path / "ratchet_audit.jsonl"
    row = _audit_row()
    append_audit(row, audit)
    loaded = load_audit(audit)
    assert len(loaded) == 1
    got = loaded[0]
    assert got.applied is False
    assert got.date == row.date
    assert got.decision.disposition is Disposition.PARK_FOR_APPROVAL
    assert got.decision.knob == "c_map.pipeline"
    assert got.decision.direction is Direction.LOOSEN
    assert got.decision.cited_merged_shas == ("sha-a", "sha-b")


def test_load_audit_missing_file_is_empty(tmp_path: Path) -> None:
    """from: §6 (load returns EMPTY on an absent file).

    A first-ever ratchet pass has no audit log yet; loading an absent path yields an empty list.
    """
    assert load_audit(tmp_path / "nope.jsonl") == []


# ── manifest write/read round-trip ────────────────────────────────────────────


def test_write_then_read_manifest_round_trips(tmp_path: Path) -> None:
    """from: §6 (write_manifest / read_manifest round-trip).

    The dispatch-time predicted manifest persisted under a tmp dir reads back identical — the
    loop-closure feed ``write-outcome`` sources the predicted block from.
    """
    manifest = PredictedManifest(
        scope_id="PR-99",
        risk_weights_version="rw-1",
        predicted=_predicted(tier=2),
    )
    write_manifest(manifest, tmp_path)
    assert read_manifest("PR-99", tmp_path) == manifest


def test_read_manifest_absent_is_none(tmp_path: Path) -> None:
    """from: §6 (read_manifest absent -> None — the visible-drop signal).

    No persisted manifest for a scope returns ``None`` (not an error) — the signal
    ``write-outcome`` turns into a stderr warning + exit 0 + no ledger append.
    """
    assert read_manifest("PR-DOES-NOT-EXIST", tmp_path) is None


# ── weights write/read round-trip (the only live-config writer) ───────────────


def test_write_then_read_weights_round_trips_to_a_tmp_file(tmp_path: Path) -> None:
    """from: Stage-3 F — write_weights -> read_weights round-trips a candidate to a tmp file.

    A mutated candidate (bumped version + a tuned ``c_map`` entry + a moved threshold) written to a
    tmp path reads back byte-equal through ``to_json``/``from_json`` — the ratchet's apply path
    persists exactly what it computed. Routed through ``tmp_path`` so the real git-tracked
    ``risk_weights.json`` is never mutated.
    """
    weights_file = tmp_path / "risk_weights.json"
    candidate: RiskWeights = dataclasses.replace(
        SEED_RISK_WEIGHTS,
        weights_version="rw-2",
        c_map={**SEED_RISK_WEIGHTS.c_map, "tests": 2},
        t1=2,
    )
    write_weights(candidate, weights_file)
    assert read_weights(weights_file) == candidate


def test_read_weights_rejects_a_forbidden_floor_key(tmp_path: Path) -> None:
    """from: Stage-3 F + plan §3 (the live-config reader fail-closes on a 'floor' key).

    The immutable floor is not representable in the tunable config; a hand-edited
    ``risk_weights.json`` carrying a ``floor`` key is rejected by the reader, so an auto-tune can
    never weaken the trip-wire through the on-disk seam.
    """
    weights_file = tmp_path / "risk_weights.json"
    weights_file.write_text(
        '{"weights_version": "x", "c_map": {}, "b_buckets": {}, "p_levels": {}, '
        '"t1": 1, "t2": 5, "floor": 2}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="floor"):
        read_weights(weights_file)


# ── default-path contract (GREEN from freeze) ─────────────────────────────────


def test_default_runtime_paths_are_under_data_not_archive() -> None:
    """from: §6 (DEFAULT_LEDGER / AUDIT / MANIFEST under data/, NOT archive/).

    The mutable runtime state lives under the gitignored ``data/`` home (CLAUDE.md), never under
    ``archive/`` (snapshot territory).
    """
    for path in (DEFAULT_LEDGER_PATH, DEFAULT_AUDIT_PATH, DEFAULT_MANIFEST_DIR):
        assert "data" in path.parts, f"{path} not under data/"
        assert "archive" not in path.parts, f"{path} must not be under archive/"
    assert DEFAULT_LEDGER_PATH.name == "outcomes.jsonl"
    assert DEFAULT_AUDIT_PATH.name == "ratchet_audit.jsonl"
    assert DEFAULT_MANIFEST_DIR.name == "manifests"
    assert "calibration" in DEFAULT_LEDGER_PATH.parts


def test_default_weights_path_is_the_package_relative_git_tracked_file() -> None:
    """from: §6 (DEFAULT_WEIGHTS_PATH is the package-relative git-tracked file).

    The tunable config ships WITH the source (package-relative, not cwd-relative) and is a real
    on-disk file — the one git-tracked weights the ratchet's CommitPlan stages. It is NOT under
    ``data/`` (it is versioned, not runtime state).
    """
    assert DEFAULT_WEIGHTS_PATH.is_file()
    assert DEFAULT_WEIGHTS_PATH.name == "risk_weights.json"
    tail = DEFAULT_WEIGHTS_PATH.parts[-5:]
    assert tail == ("backend", "src", "genome", "calibration", "risk_weights.json")
    assert "data" not in DEFAULT_WEIGHTS_PATH.parts


# ── pending_parked: consumed-row exclusion (finding-040 FIX-2) ─────────────────


def _parked(candidate: RiskWeights) -> AuditRow:
    """An un-applied PARK_FOR_APPROVAL audit row carrying ``candidate``."""
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


def _approved(candidate: RiskWeights) -> AuditRow:
    """The ``applied=True`` AUTO_COMMIT row an approval appends (the consumption marker)."""
    decision = RatchetDecision(
        disposition=Disposition.AUTO_COMMIT,
        knob="c_map.pipeline",
        direction=Direction.TIGHTEN,
        candidate_weights=candidate,
        backtest_clean=True,
        knob_covered=False,
        cited_merged_shas=("sha-a",),
        rationale="approved",
        auto_applicable=True,
    )
    return AuditRow(date="2026-06-26", applied=True, decision=decision)


def test_pending_parked_excludes_a_consumed_row_and_keeps_an_unapproved_one() -> None:
    """from: finding-040 FIX-2 (an approved parked row is consumed; an un-approved one stays).

    A parked candidate whose value-equal ``applied=True`` row exists is consumed (insert-then-
    supersede, never re-selected); a parked candidate with no matching applied row stays pending —
    so a clean-by-vacuity tighten is approvable exactly once and older parked rows do not strand.
    """
    cand_a = dataclasses.replace(
        SEED_RISK_WEIGHTS, c_map={**SEED_RISK_WEIGHTS.c_map, "pipeline": 4}, weights_version="rw-2"
    )
    cand_b = dataclasses.replace(
        SEED_RISK_WEIGHTS, c_map={**SEED_RISK_WEIGHTS.c_map, "cli": 2}, weights_version="rw-2"
    )
    rows = [_parked(cand_a), _approved(cand_a), _parked(cand_b)]
    pending = pending_parked(rows)
    assert [row.decision.candidate_weights for row in pending] == [cand_b]

"""Serialization seam — round-trips + fail-closed rejection (PHI structurally impossible).

from: §5 test #7 (test_calibration_model.py) + §6:
  * from_json / to_json round-trip incl nested PredictedBlock / ActualBlock / TierBreakdown;
  * ``OutcomeRecord.from_json`` RAISES when ``risk_weights_version`` is missing;
  * ``TierFields.from_json`` and ``OutcomeRecord.from_json`` REJECT an unexpected field
    (PHI-impossible-by-construction);
  * strict narrowing rejects bool-as-int.

The model copies (does not import) the strict ``_as_*`` narrowers from scope_split so a refactor
of the frozen splitter can never silently move the calibrator's JSON contract. Exception types
follow the codebase convention: missing/unexpected -> ValueError, type-narrow -> TypeError.
RED until the from_json / to_json bodies land.

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import pytest

from genome.calibration.model import (
    ActualBlock,
    OutcomeRecord,
    PredictedBlock,
    TierBreakdown,
    TierFields,
)


def _breakdown() -> TierBreakdown:
    """A realistic PR-7-shaped sub-score breakdown (C=2, B=1, P=1, A=0, S=4, unfloored)."""
    return TierBreakdown(c=2, b=1, p=1, a=0, s=4, floor=0, deep_t2=False)


def _predicted() -> PredictedBlock:
    """A realistic dispatch-time prediction with non-empty premortem tuples (round-trip teeth)."""
    return PredictedBlock(
        tier=1,
        breakdown=_breakdown(),
        premortem_surprises=("gnomad_matches drifts on reload",),
        anchors_to_watch=("gnomad_matches", "row_count"),
    )


def _actual() -> ActualBlock:
    """A realistic merge-time ground-truth block (a clean pass with one mild revise cycle)."""
    return ActualBlock(
        gate_verdict="pass",
        review_blockers=(),
        surprises_materialized=(),
        surprises_missed=("an unforeseen palindromic collapse",),
        anchors_moved_unexpected=(),
        revise_cycles=1,
        fix_first_cycles=0,
        needed_deep=False,
    )


def _outcome() -> OutcomeRecord:
    """A realistic one-line outcome-ledger datum naming its weights epoch."""
    return OutcomeRecord(
        scope_id="PR-7",
        merged_sha="0f1e2d3c4b5a",
        date="2026-06-25",
        risk_weights_version="rw-1",
        predicted=_predicted(),
        actual=_actual(),
    )


def _outcome_payload() -> dict[str, object]:
    """A valid OutcomeRecord JSON mapping built by hand (to_json is stubbed at freeze).

    Breakdown keys are UPPERCASE per ``TierBreakdown.to_json`` (the dispatcher ``risk_breakdown``
    shape) so the hand-built payload matches the frozen serialization contract.
    """
    return {
        "scope_id": "PR-7",
        "merged_sha": "0f1e2d3c4b5a",
        "date": "2026-06-25",
        "risk_weights_version": "rw-1",
        "predicted": {
            "tier": 1,
            "breakdown": {"C": 2, "B": 1, "P": 1, "A": 0, "S": 4, "floor": 0, "deep_T2": False},
            "premortem_surprises": ["gnomad_matches drifts on reload"],
            "anchors_to_watch": ["gnomad_matches", "row_count"],
        },
        "actual": {
            "gate_verdict": "pass",
            "review_blockers": [],
            "surprises_materialized": [],
            "surprises_missed": ["an unforeseen palindromic collapse"],
            "anchors_moved_unexpected": [],
            "revise_cycles": 1,
            "fix_first_cycles": 0,
            "needed_deep": False,
        },
    }


# ── round-trips (nested PredictedBlock / ActualBlock / TierBreakdown) ──────────


def test_outcome_record_round_trips_through_json() -> None:
    """from: §6 (from_json/to_json round-trip incl nested PredictedBlock/ActualBlock/TierBreakdown).

    A full record survives ``from_json(to_json(rec)) == rec`` — exercising the nested
    PredictedBlock (and its TierBreakdown) plus the ActualBlock, with tuples restored as tuples.
    """
    record = _outcome()
    assert OutcomeRecord.from_json(record.to_json()) == record


def test_predicted_block_round_trips_with_its_breakdown() -> None:
    """from: §6 (nested TierBreakdown serialization through PredictedBlock).

    TierBreakdown has no standalone from_json — it round-trips inside PredictedBlock. The
    predicted half (tier + breakdown + premortem tuples) restores byte-equal.
    """
    predicted = _predicted()
    assert PredictedBlock.from_json(predicted.to_json()) == predicted


def test_actual_block_round_trips_through_json() -> None:
    """from: §6 (nested ActualBlock round-trip).

    The merge-time ground-truth block (verdict + the surprise/anchor tuples + cycle counts)
    restores byte-equal through its JSON seam.
    """
    actual = _actual()
    assert ActualBlock.from_json(actual.to_json()) == actual


# ── fail-closed rejection ──────────────────────────────────────────────────────


def test_outcome_record_rejects_missing_risk_weights_version() -> None:
    """from: §6 (OutcomeRecord.from_json RAISES when risk_weights_version is missing).

    Every record must name its weights epoch so the ratchet never attributes an outcome to the
    wrong epoch; a payload without ``risk_weights_version`` fails closed (ValueError).
    """
    payload = _outcome_payload()
    del payload["risk_weights_version"]
    with pytest.raises(ValueError, match="risk_weights_version"):
        OutcomeRecord.from_json(payload)


def test_outcome_record_rejects_an_unexpected_field() -> None:
    """from: §6 (OutcomeRecord.from_json REJECTS an unexpected field — PHI impossible).

    A profile-bearing key cannot ride into the ledger: an unexpected top-level field is a
    malformed record and fails closed, so no PHI is structurally representable.
    """
    payload = _outcome_payload()
    payload["patient_name"] = "Jane Doe"
    with pytest.raises(ValueError, match=r"patient_name|unexpected|unknown"):
        OutcomeRecord.from_json(payload)


def test_tier_fields_rejects_an_unexpected_field() -> None:
    """from: §6 (TierFields.from_json REJECTS an unexpected field — PHI impossible).

    The compute-tier seam is equally strict: an unexpected manifest key fails closed rather than
    silently riding a profile field into the calibrator.
    """
    payload: dict[str, object] = {
        "change_class": ["data-backfill"],
        "imports_touched_count": 3,
        "precedent_surprise": "clean",
        "applicable_anchors_count": 0,
        "diagnosis": "BRCA2 c.1234A>T",
    }
    with pytest.raises(ValueError, match=r"diagnosis|unexpected|unknown"):
        TierFields.from_json(payload)


def test_strict_narrowing_rejects_bool_as_int() -> None:
    """from: §6 (strict narrowing rejects bool-as-int).

    The copied ``_as_int`` narrower rejects a ``bool`` masquerading as an int, so a JSON
    ``true`` in an integer slot (``imports_touched_count``) raises TypeError, never coerces to 1.
    """
    payload: dict[str, object] = {
        "change_class": ["data-backfill"],
        "imports_touched_count": True,
        "precedent_surprise": "clean",
        "applicable_anchors_count": 0,
    }
    with pytest.raises(TypeError, match="bool"):
        TierFields.from_json(payload)


# ── change_class vocab guard (BLOCKER A: a structural typo must not silently under-tier) ──


@pytest.mark.parametrize("bad_label", ["DDL", "schema_change", "analysiss", "Schema", "SCHEMA"])
def test_tier_fields_from_json_rejects_out_of_vocab_change_class(bad_label: str) -> None:
    """from: Stage-3 BLOCKER A (out-of-vocab / mis-cased / typo'd change_class -> from_json raises).

    The sibling ``scope_split.model.ScopeManifestInput.from_json`` rejects an unknown change_class
    label; the calibrator's seam must too, or a structural ``DDL`` / ``Schema`` typo scores C=0 and
    silently under-tiers (the irreversible direction). The guard mirrors the sibling's message.
    """
    payload: dict[str, object] = {
        "change_class": [bad_label],
        "imports_touched_count": 1,
        "precedent_surprise": "clean",
        "applicable_anchors_count": 0,
    }
    with pytest.raises(ValueError, match=rf"{bad_label}|unknown label"):
        TierFields.from_json(payload)


# ── gate_verdict vocab guard (WARN B: a mis-cased verdict must not read as a pass) ──


@pytest.mark.parametrize("bad_verdict", ["BLOCKED", "Pass", "green", "ok", "fail"])
def test_actual_block_from_json_rejects_out_of_vocab_gate_verdict(bad_verdict: str) -> None:
    """from: Stage-3 WARN B (out-of-vocab gate_verdict -> from_json raises).

    A typo'd ``BLOCKED`` is otherwise silently treated as a non-blocking verdict by the hindsight
    ladder, corrupting the learning signal; the value guard fails it closed instead.
    """
    with pytest.raises(ValueError, match=rf"{bad_verdict}|gate_verdict"):
        ActualBlock.from_json({"gate_verdict": bad_verdict})


def test_actual_block_from_json_accepts_every_vocab_verdict() -> None:
    """from: Stage-3 WARN B (the in-vocab verdicts still parse) — the guard is not over-tight.

    ``pass`` / ``blocked`` / ``escalate`` all round-trip; only out-of-vocab values are rejected.
    """
    for verdict in ("pass", "blocked", "escalate"):
        assert ActualBlock.from_json({"gate_verdict": verdict}).gate_verdict == verdict

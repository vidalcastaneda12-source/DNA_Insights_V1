"""Model-level units — ScopeManifestInput / SplitResult round-trip + fail-closed narrowing.

Plan-blind spec source: FROZEN-INTERFACE model.py section (constructors + from_json/to_json,
"IMPLEMENTED — tests GREEN from freeze"); IMPL-CONTRACT arch-2 ("commit a golden dispatcher-
manifest fixture … the model round-trip test reads THAT file. from_json is FAIL-CLOSED on a
missing required nested field"); SYNTHESIZED-PLAN §5 ("model (round-trip from live dispatcher
JSON, reads nested fields, strict narrowing TypeError, FrozenInstanceError, unknown
change_class rejected)"); IMPL-CONTRACT mech #3 (SplitResult.to_json TWO-BRANCH body); and the
local S-formula back-test (FROZEN-INTERFACE lines 48-52: "PR3 S=8, schema floor→2").

All assertions are against the SPECIFIED shape/behavior — the golden fixture is the live
Stage-0 dispatcher manifest, not shaped to the implementation. GREEN from freeze.

test->spec provenance: each test docstring names the frozen-interface / contract clause it
enforces, for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from genome.scope_split.model import (
    CutQuality,
    ScopeManifestInput,
    SplitResult,
    SubScope,
    est_risk_tier,
    scope_S,
    tier_from_S,
)

# ── golden fixture (arch-2: the committed live dispatcher manifest) ────────────


def _golden_manifest_path() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "scope_split" / "golden_manifest.json"


def _golden_manifest_data() -> dict[str, object]:
    raw = _golden_manifest_path().read_text(encoding="utf-8")
    data = json.loads(raw)
    assert isinstance(data, dict)
    return data


# ── round-trip from the golden dispatcher JSON ────────────────────────────────


def test_from_json_reads_nested_blast_radius_and_risk_breakdown() -> None:
    """from: FROZEN-INTERFACE model.from_json (nested blast_radius.imports_touched /
    risk_breakdown.S) + arch-2 (the golden fixture is the live dispatcher shape).

    from_json flattens the dispatcher-nested fields: blast_radius.imports_touched →
    imports_touched, blast_radius.tests_covering → tests_covering, risk_breakdown.S →
    risk_score_S. Asserted against the golden fixture's own declared values.
    """
    data = _golden_manifest_data()
    m = ScopeManifestInput.from_json(data)

    assert m.scope_id == "B2-Phase1"
    assert m.change_class == ("cli", "tests")
    # nested blast_radius.imports_touched is read (arch-2: not silently defaulted)
    assert len(m.imports_touched) == 6
    assert "backend/src/genome/scope_split/model.py" in m.imports_touched
    # nested blast_radius.tests_covering is read
    assert len(m.tests_covering) == 2
    # nested risk_breakdown.S → risk_score_S
    assert m.risk_score_S == 3
    assert m.risk_tier == 2
    # applicable_anchors empty in the golden manifest (anchors_to_recheck: NONE)
    assert m.applicable_anchors == ()
    # out_of_scope_candidates carried through (manifest-primary partition refinement signal)
    assert len(m.out_of_scope_candidates) == 2


def test_from_json_accepts_nested_anchor_and_precedent_objects() -> None:
    """from: FROZEN-INTERFACE model.from_json ("Accepts dispatcher-nested anchor objects
    {"name":...} or bare strings; precedent objects {"finding":...} or bare strings").

    The golden fixture's precedent is a list of {"finding":...} objects; from_json narrows
    each to its finding id. Asserted against the fixture's declared findings.
    """
    m = ScopeManifestInput.from_json(_golden_manifest_data())
    assert m.precedent == ("finding-038", "finding-037")


def test_manifest_round_trips_through_to_json_from_json() -> None:
    """from: FROZEN-INTERFACE model.to_json ("FLATTENED shape, re-accepted by from_json
    round-trip").

    A manifest survives to_json() -> from_json() field-for-field.
    """
    m = ScopeManifestInput.from_json(_golden_manifest_data())
    # to_json must produce a JSON-serializable mapping that from_json re-accepts.
    reloaded = ScopeManifestInput.from_json(json.loads(json.dumps(m.to_json())))
    assert reloaded == m


# ── FAIL-CLOSED from_json (arch-2: missing required nested field → ValueError) ─


@pytest.mark.parametrize(
    ("payload", "missing"),
    [
        ({"change_class": ["cli"], "blast_radius": {"imports_touched": ["a"]}}, "scope_id"),
        ({"scope_id": "x", "blast_radius": {"imports_touched": ["a"]}}, "change_class"),
        ({"scope_id": "x", "change_class": ["cli"], "blast_radius": {}}, "imports_touched"),
        ({"scope_id": "x", "change_class": ["cli"]}, "blast_radius"),
    ],
)
def test_from_json_fail_closed_on_missing_required_field(
    payload: dict[str, object], missing: str
) -> None:
    """from: arch-2 ("from_json is FAIL-CLOSED on a missing required nested field …
    blast_radius.imports_touched missing → raise ValueError, do not silently default to ()").

    Required: scope_id, change_class, blast_radius.imports_touched. Each absence raises
    ValueError rather than defaulting.
    """
    with pytest.raises(ValueError):  # noqa: PT011 — fail-closed contract is "a ValueError"
        ScopeManifestInput.from_json(payload)
    assert missing  # the parametrize label documents which field is withheld


def test_from_json_allows_risk_breakdown_s_none() -> None:
    """from: FROZEN-INTERFACE model.from_json ("risk_breakdown.S → None allowed") + arch-2
    ("risk_breakdown.S → None allowed").

    risk_breakdown.S is OPTIONAL/undecidable: a null S survives as risk_score_S=None (not a
    fail-closed error — only the three required fields fail closed).
    """
    m = ScopeManifestInput.from_json(
        {
            "scope_id": "x",
            "change_class": ["cli"],
            "blast_radius": {"imports_touched": ["a"]},
            "risk_breakdown": {"S": None},
        }
    )
    assert m.risk_score_S is None


def test_from_json_strict_narrowing_rejects_wrong_type() -> None:
    """from: SYNTHESIZED-PLAN §5 ("strict narrowing TypeError") + mech #3 (verify_gate
    narrowing helpers copied — strict ``_as_*`` narrowers).

    A change_class supplied as a bare string (not a list) is a type violation: the strict
    narrower raises rather than silently iterating the string's characters.
    """
    with pytest.raises((TypeError, ValueError)):
        ScopeManifestInput.from_json(
            {
                "scope_id": "x",
                "change_class": "cli",  # wrong type — should be a list
                "blast_radius": {"imports_touched": ["a"]},
            }
        )


def test_manifest_is_frozen() -> None:
    """from: FROZEN-INTERFACE ("@dataclass(frozen=True, slots=True) ScopeManifestInput").

    Attribute assignment on a constructed manifest raises FrozenInstanceError (immutability is
    part of the contract — the records are value objects).
    """
    m = ScopeManifestInput(scope_id="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.scope_id = "y"  # type: ignore[misc]


# ── SplitResult TWO-BRANCH to_json (mech #3) ──────────────────────────────────


def test_split_result_to_json_atomic_branch_is_exactly_two_keys() -> None:
    """from: mech #3 + FROZEN-INTERFACE SplitResult.to_json ("atomic → EXACTLY
    {"atomic":True,"reason":str} (no other keys)").

    An atomic result serializes to EXACTLY two keys — no sub_scopes/order/cut_quality leak
    through (the dry-run / check --json contract depends on this exact shape).
    """
    result = SplitResult(atomic=True, reason="not separable by manifest")
    payload = result.to_json()
    assert set(payload.keys()) == {"atomic", "reason"}
    assert payload["atomic"] is True
    assert payload["reason"] == "not separable by manifest"


def test_split_result_to_json_split_branch_is_full_dict() -> None:
    """from: mech #3 + FROZEN-INTERFACE SplitResult.to_json ("split → {"atomic":False,
    "reason":..,"sub_scopes":[...],"order":[...],"cut_quality":{...}}").

    A non-atomic result serializes to the full dict: atomic, reason, sub_scopes, order,
    cut_quality.
    """
    sub = SubScope(
        sub_scope_id="x-s1",
        origin_scope="x",
        change_class=("schema",),
        est_imports_touched=2,
        applicable_anchors=(),
        est_risk_tier=2,
        depends_on=(),
        rationale="schema slice",
    )
    cq = CutQuality(
        cut_cost=0.1,
        max_tier_before=2,
        max_tier_after=2,
        min_subscope_shrink=0.5,
        clean=True,
    )
    result = SplitResult(
        atomic=False,
        reason="2 separable clusters",
        sub_scopes=(sub,),
        order=("x-s1",),
        cut_quality=cq,
    )
    payload = result.to_json()
    assert set(payload.keys()) == {"atomic", "reason", "sub_scopes", "order", "cut_quality"}
    assert payload["atomic"] is False
    assert isinstance(payload["sub_scopes"], list)
    assert payload["order"] == ["x-s1"]
    assert isinstance(payload["cut_quality"], dict)


# ── local S-formula reproduces the dispatcher back-test ───────────────────────


def test_tier_from_s_matches_dispatcher_thresholds() -> None:
    """from: FROZEN-INTERFACE ("reproduces dispatcher back-test exactly") +
    .claude/agents/scope-dispatcher.md ("tier_from_S = 0 if S==0 · 1 if 1<=S<=4 · 2 if S>=5").

    tier_from_S replicates the dispatcher's banded thresholds exactly.
    """
    assert tier_from_S(0) == 0
    assert tier_from_S(1) == 1
    assert tier_from_S(4) == 1
    assert tier_from_S(5) == 2
    assert tier_from_S(8) == 2


def test_scope_s_back_test_pr3_is_eight() -> None:
    """from: FROZEN-INTERFACE ("PR3 S=8") + dispatcher back-test row PR 3 (C=3,B=3,P=2 → S=8).

    The local S-formula reproduces PR-3 (canonicalize-variants): change_class touches a
    pipeline-class concern (C=3), large blast radius >15 imports (B=3), correction-class
    precedent (P=2) → S = 3 + 3 + 2 = 8. We supply the back-test's own C/B/P inputs and assert
    the published S, then assert it maps to deep-Tier-2.
    """
    # scope_S(change_class, imports_touched:int, precedent_surprise:int) → S = C + B + P.
    # PR-3 published inputs: pipeline change_class (C=3), >15 imports (B=3), correction (P=2).
    s = scope_S(("pipeline",), 30, 2)
    assert s == 8
    assert tier_from_S(s) == 2


def test_scope_s_schema_floor_lands_at_tier_two() -> None:
    """from: FROZEN-INTERFACE ("schema floor→2") + dispatcher floor rule ("floor = 2 if
    (schema|ddl touched)") + FROZEN-INTERFACE est_risk_tier signature.

    A schema/ddl change with an otherwise tiny footprint still floors to Tier 2 via
    est_risk_tier (the irreversible-structural-change trip-wire).
    """
    # Signature per FROZEN-INTERFACE: change_class, applicable_anchors, imports_touched count,
    # then precedent_surprise (default 0).
    tier = est_risk_tier(("schema",), (), 0, 0)
    assert tier == 2

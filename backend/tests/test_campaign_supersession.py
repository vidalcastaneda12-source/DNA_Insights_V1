"""Supersession (locked decision #7) tests for ``genome.campaign`` — the insert-then-flip core.

Spec source: SYNTHESIZED-PLAN §5 (``test_campaign_supersession.py``), §3 constraint (locked #7
append-only insert-then-flip; #8 provenance fail-closed), Gate-1 refinement A (gate-crossing
symmetry). Plan-blind: written from the §5/§6 contract, not the implementation bodies.

Asserts the append-only ledger semantics that make the campaign auditable + resumable:

* a ``transition`` APPENDS exactly one record and supersedes the prior active record;
* every prior record's content is byte-immutable across subsequent transitions (never-in-place);
* the current view is exactly the latest-active record per ``sub_scope_id``;
* the full append-only history is recoverable (``reduce_current`` is derived, not destructive);
* fail-closed #8 — a record built without ``origin_scope`` or ``manifest_snapshot`` raises;
* a hand-built >1-active log is rejected by ``CampaignState.__post_init__``.
"""

from __future__ import annotations

import pytest

from genome.campaign.model import CampaignState, CampaignStatus, SubScopeState
from genome.campaign.state_machine import reduce_current, seed_campaign, transition
from genome.scope_split.model import SplitResult, SubScope


def _linear_split(origin: str, n: int) -> SplitResult:
    """A non-atomic SplitResult of ``n`` sub-scopes in a linear dependency chain (s1←s2←…←sN)."""
    subs = tuple(
        SubScope(
            sub_scope_id=f"{origin}-s{i}",
            origin_scope=origin,
            change_class=("cli",),
            est_imports_touched=2,
            applicable_anchors=(),
            est_risk_tier=1,
            depends_on=() if i == 1 else (f"{origin}-s{i - 1}",),
            rationale=f"cluster {i}",
        )
        for i in range(1, n + 1)
    )
    return SplitResult(
        atomic=False,
        reason="clean cut",
        sub_scopes=subs,
        order=tuple(s.sub_scope_id for s in subs),
        cut_quality=None,
    )


def _state(**overrides: object) -> SubScopeState:
    """Build a SubScopeState with valid #8-provenance defaults, overriding any field by kwarg."""
    fields: dict[str, object] = {
        "record_seq": 0,
        "sub_scope_id": "PR-X-s1",
        "status": CampaignStatus.PENDING,
        "origin_scope": "PR-X",
        "manifest_snapshot": {"sub_scope_id": "PR-X-s1"},
        "depends_on": (),
        "supersedes": None,
        "resplit_depth": 0,
        "note": "",
    }
    fields.update(overrides)
    return SubScopeState(**fields)  # type: ignore[arg-type]


def test_transition_appends_one_record_and_supersedes_the_prior() -> None:
    """from: §5 supersession ('a transition APPENDS exactly one record AND supersedes the prior').

    A status transition is an INSERT of a new record (``record_seq`` = max+1, ``supersedes`` =
    the prior active record's seq), not an edit of the prior — the locked-#7 insert-then-flip.
    """
    history = list(seed_campaign(_linear_split("PR-X", 3), "PR-X"))
    prior = next(r for r in history if r.sub_scope_id == "PR-X-s1")

    new_record = transition(history, "PR-X-s1", CampaignStatus.READY)

    assert new_record.record_seq == max(r.record_seq for r in history) + 1
    assert new_record.supersedes == prior.record_seq
    assert new_record.status is CampaignStatus.READY
    assert new_record.sub_scope_id == "PR-X-s1"
    # The append is the caller's (persistence's) job — one record produced, history not mutated.
    assert len(history) == 3


def test_prior_record_content_is_byte_immutable_across_transitions() -> None:
    """from: §5 supersession ('every PRIOR record CONTENT byte-identical, exhaustively').

    Walk a full lifecycle and assert EVERY record, once created, never changes its serialized
    content — snapshot each record's bytes the moment it is created, then re-compare all of them
    after the whole walk (no in-place mutation anywhere in the transition path).
    """
    history = list(seed_campaign(_linear_split("PR-X", 1), "PR-X"))
    snapshots = [history[0].to_json()]  # the seed record's bytes at creation

    # pending → ready → planning → implementing → merged (the two gate crossings are external).
    for new_status, external in (
        (CampaignStatus.READY, False),
        (CampaignStatus.PLANNING, False),
        (CampaignStatus.IMPLEMENTING, True),
        (CampaignStatus.MERGED, True),
    ):
        record = transition(history, "PR-X-s1", new_status, external_event=external)
        history.append(record)
        snapshots.append(record.to_json())  # capture each record's bytes at its creation

    # After the full walk, EVERY record still serializes byte-identically to its creation snapshot.
    assert [r.to_json() for r in history] == snapshots
    assert len(history) == 5  # seed + 4 transitions, all retained (append-only)
    assert history[0].status is CampaignStatus.PENDING  # the seed is unchanged, not mutated forward


def test_reduce_current_yields_exactly_one_active_record_per_sub_scope() -> None:
    """from: §5 supersession ('reduce_current yields exactly one active record per sub_scope_id').

    The current view is the latest ``record_seq`` per ``sub_scope_id``; superseded records drop
    out of the view but stay in the history.
    """
    history = list(seed_campaign(_linear_split("PR-X", 2), "PR-X"))
    history.append(transition(history, "PR-X-s1", CampaignStatus.READY))
    history.append(transition(history, "PR-X-s1", CampaignStatus.PLANNING))

    state = reduce_current(history, campaign_id="PR-X")

    by_id = {s.sub_scope_id: s for s in state.sub_scopes}
    assert len(state.sub_scopes) == 2
    assert by_id["PR-X-s1"].status is CampaignStatus.PLANNING  # latest wins
    assert by_id["PR-X-s2"].status is CampaignStatus.PENDING


def test_full_append_only_history_is_recoverable() -> None:
    """from: §5 supersession ('full append-only history stays recoverable').

    ``reduce_current`` is a derived projection — it never drops records from the underlying log,
    so the complete transition history of every sub-scope remains reconstructable.
    """
    history = list(seed_campaign(_linear_split("PR-X", 1), "PR-X"))
    history.append(transition(history, "PR-X-s1", CampaignStatus.READY))
    history.append(transition(history, "PR-X-s1", CampaignStatus.PLANNING))

    seqs = [r.record_seq for r in history if r.sub_scope_id == "PR-X-s1"]
    statuses = [r.status for r in history if r.sub_scope_id == "PR-X-s1"]
    assert seqs == [0, 1, 2]  # monotonic, contiguous, never overwritten
    assert statuses == [CampaignStatus.PENDING, CampaignStatus.READY, CampaignStatus.PLANNING]


@pytest.mark.parametrize("missing", ["origin_scope", "manifest_snapshot"])
def test_record_without_provenance_is_rejected_fail_closed(missing: str) -> None:
    """from: §3 (#8) + §5 ('a record built without origin_scope/manifest_snapshot RAISES').

    Fail-closed #8: a record missing its attributability (empty ``origin_scope`` or empty
    ``manifest_snapshot``) is rejected at construction, so every persisted transition is
    attributable.
    """
    empty: object = "" if missing == "origin_scope" else {}
    with pytest.raises(ValueError, match=missing):
        _state(**{missing: empty})


@pytest.mark.parametrize("missing", ["origin_scope", "manifest_snapshot"])
def test_from_json_rejects_missing_provenance(missing: str) -> None:
    """from: §3 (#8 provenance) + §4 step 1 (from_json fail-closed narrowing).

    The JSON ingress seam is fail-closed too: a serialized record missing ``origin_scope`` or
    ``manifest_snapshot`` raises rather than reconstructing an unattributable record.
    """
    payload: dict[str, object] = {
        "record_seq": 0,
        "sub_scope_id": "PR-X-s1",
        "status": "pending",
        "origin_scope": "PR-X",
        "manifest_snapshot": {"sub_scope_id": "PR-X-s1"},
        "depends_on": [],
        "supersedes": None,
        "resplit_depth": 0,
        "note": "",
    }
    del payload[missing]
    with pytest.raises((ValueError, TypeError)):
        SubScopeState.from_json(payload)


def test_campaign_state_rejects_more_than_one_active_record_per_sub_scope() -> None:
    """from: §5 ('a hand-built >1-active log is rejected by CampaignState.__post_init__').

    The current-view invariant is structural: a ``CampaignState`` carrying two records for the
    same ``sub_scope_id`` is an illegal torn state and is rejected at construction.
    """
    r1 = _state(record_seq=0, sub_scope_id="PR-X-s1", status=CampaignStatus.PENDING)
    r2 = _state(record_seq=1, sub_scope_id="PR-X-s1", status=CampaignStatus.READY)
    with pytest.raises(ValueError, match="PR-X-s1"):
        CampaignState(campaign_id="PR-X", sub_scopes=(r1, r2))

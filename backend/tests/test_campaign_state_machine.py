"""State-machine tests for ``genome.campaign`` — the pure reducers (no I/O, no genome.db).

Spec source: SYNTHESIZED-PLAN §4 step 2 (``state_machine.py`` reducers), §5
(``test_campaign_state_machine.py``), and the Gate-1 refinements: A (gate-crossing symmetry —
BOTH ``planning→implementing`` (Gate 1) and ``implementing→merged`` (Gate 2) require an external
event), B (eject fails loud — a carried, human-readable note). Plan-blind: written from the
contract, not the implementation.

Covers: ``next_ready`` deps-gating; ``advance_on_merge`` teeing up the next dependent; the three
``apply_revalidation`` transitions (moot / changed / grown); the re-split cap; the gate-crossing
guard; and a reachability property (every status reachable, every legal transition fires — no
structurally-dead branch, the finding-039 / PR-5a dead-branch lesson).
"""

from __future__ import annotations

import pytest

from genome.campaign.model import (
    GATE_CROSSINGS,
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    CampaignStatus,
    RevalidationDecision,
    SubScopeState,
)
from genome.campaign.state_machine import (
    advance_on_merge,
    apply_revalidation,
    cancel_campaign,
    next_ready,
    reduce_current,
    seed_campaign,
    tee_up,
    transition,
)
from genome.scope_split.model import MAX_RESPLIT_DEPTH, SplitResult, SubScope


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


def _child(origin: str, sub_id: str) -> SubScope:
    """A re-split child mini-manifest (the shell-produced GROWN carve input)."""
    return SubScope(
        sub_scope_id=sub_id,
        origin_scope=origin,
        change_class=("cli",),
        est_imports_touched=1,
        applicable_anchors=(),
        est_risk_tier=1,
        depends_on=(),
        rationale="re-split child",
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


def _drive_to_implementing(history: list[SubScopeState], sub_id: str) -> None:
    """Append legal records to bring ``sub_id`` from ready to implementing (Gate 1 passed)."""
    history.append(transition(history, sub_id, CampaignStatus.READY))
    history.append(transition(history, sub_id, CampaignStatus.PLANNING))
    history.append(transition(history, sub_id, CampaignStatus.IMPLEMENTING, external_event=True))


# ── seed_campaign ────────────────────────────────────────────────────────────


def test_seed_campaign_yields_pending_records_in_topo_order() -> None:
    """from: §4 step 2 (seed_campaign) + integration-smoke ('seed yields N pending records')."""
    records = seed_campaign(_linear_split("PR-X", 3), "PR-X")
    assert [r.sub_scope_id for r in records] == ["PR-X-s1", "PR-X-s2", "PR-X-s3"]
    assert all(r.status is CampaignStatus.PENDING for r in records)
    assert all(r.supersedes is None for r in records)
    assert [r.record_seq for r in records] == [0, 1, 2]
    # provenance #8 — each seed record carries origin + a non-empty manifest snapshot.
    assert all(r.origin_scope == "PR-X" and r.manifest_snapshot for r in records)


def test_seed_campaign_rejects_an_atomic_split() -> None:
    """from: §4 step 2 ('raises on an atomic SplitResult') — there is nothing to sequence."""
    atomic = SplitResult(atomic=True, reason="one indivisible unit")
    with pytest.raises(ValueError, match="atomic"):
        seed_campaign(atomic, "PR-X")


# ── deps-gating: tee_up + next_ready ──────────────────────────────────────────


def test_tee_up_only_promotes_deps_satisfied_pending_to_ready() -> None:
    """from: §4 step 2 (next_ready deps-gating) — only the deps-free sub-scope becomes ready."""
    history = list(seed_campaign(_linear_split("PR-X", 3), "PR-X"))
    promoted = tee_up(history)
    assert {r.sub_scope_id for r in promoted} == {"PR-X-s1"}
    assert all(r.status is CampaignStatus.READY for r in promoted)


def test_next_ready_returns_first_ready_and_none_when_blocked() -> None:
    """from: §4 step 2 (next_ready) — first ready in order; None when nothing is teed up."""
    history = list(seed_campaign(_linear_split("PR-X", 3), "PR-X"))
    blocked = reduce_current(history, campaign_id="PR-X")
    assert next_ready(blocked) is None  # all pending, none teed up

    history += tee_up(history)
    ready_state = reduce_current(history, campaign_id="PR-X")
    nxt = next_ready(ready_state)
    assert nxt is not None
    assert nxt.sub_scope_id == "PR-X-s1"


def test_advance_on_merge_merges_and_tees_up_the_next_dependent() -> None:
    """from: §5 ('advance_on_merge tees up the next ready dependent')."""
    history = list(seed_campaign(_linear_split("PR-X", 2), "PR-X"))
    _drive_to_implementing(history, "PR-X-s1")

    produced = advance_on_merge(history, "PR-X-s1")

    pairs = {(r.sub_scope_id, r.status) for r in produced}
    assert ("PR-X-s1", CampaignStatus.MERGED) in pairs  # Gate 2 crossing (external)
    assert ("PR-X-s2", CampaignStatus.READY) in pairs  # dependent now unblocked


def test_a_mooted_dependency_unblocks_its_dependent() -> None:
    """from: §4 step 2 (deps satisfied = merged OR moot) — a skipped dep still unblocks."""
    history = list(seed_campaign(_linear_split("PR-X", 2), "PR-X"))
    history += tee_up(history)  # s1 ready
    history.append(transition(history, "PR-X-s1", CampaignStatus.MOOT))
    promoted = tee_up(history)
    assert {r.sub_scope_id for r in promoted} == {"PR-X-s2"}


def test_an_ejected_dependency_does_not_unblock_its_dependent() -> None:
    """from: ptest-2 (review) — only merged/moot RESOLVE a dependency; an EJECTED dep is escalated
    and leaves its dependents blocked (the negative of the moot/merged cases, by design)."""
    history = list(seed_campaign(_linear_split("PR-X", 2), "PR-X"))
    history += tee_up(history)  # s1 ready
    history.append(transition(history, "PR-X-s1", CampaignStatus.EJECTED))
    promoted = tee_up(history)
    assert promoted == []  # s2 stays PENDING — an ejected dep does not satisfy the dependency


# ── apply_revalidation: moot / changed / grown ────────────────────────────────


def test_apply_revalidation_still_needed_advances_to_planning() -> None:
    """from: §4 step 2 (still_needed → ready→planning)."""
    history = list(seed_campaign(_linear_split("PR-X", 1), "PR-X"))
    history += tee_up(history)
    produced = apply_revalidation(history, "PR-X-s1", RevalidationDecision.STILL_NEEDED)
    assert [r.status for r in produced] == [CampaignStatus.PLANNING]


def test_apply_revalidation_moot_skips_the_sub_scope() -> None:
    """from: §5 (the three re-validation transitions — moot)."""
    history = list(seed_campaign(_linear_split("PR-X", 1), "PR-X"))
    history += tee_up(history)
    produced = apply_revalidation(history, "PR-X-s1", RevalidationDecision.MOOT)
    assert [r.status for r in produced] == [CampaignStatus.MOOT]


def test_apply_revalidation_changed_updates_snapshot_and_stays_ready() -> None:
    """from: §5 (re-validation — changed: stays ready with an updated manifest_snapshot)."""
    history = list(seed_campaign(_linear_split("PR-X", 1), "PR-X"))
    history += tee_up(history)
    new_snapshot = {"sub_scope_id": "PR-X-s1", "change_class": ["cli", "tests"]}
    produced = apply_revalidation(
        history,
        "PR-X-s1",
        RevalidationDecision.CHANGED,
        updated_manifest_snapshot=new_snapshot,
    )
    assert len(produced) == 1
    assert produced[0].status is CampaignStatus.READY
    assert produced[0].manifest_snapshot == new_snapshot


def test_apply_revalidation_grown_within_cap_carves_children() -> None:
    """from: §5 (grown → carve children at depth+1) + §4 step 2 (re-split mechanics)."""
    history = list(seed_campaign(_linear_split("PR-X", 1), "PR-X"))
    history += tee_up(history)  # s1 ready at resplit_depth 0
    children = (_child("PR-X", "PR-X-s1-a"), _child("PR-X", "PR-X-s1-b"))

    produced = apply_revalidation(
        history,
        "PR-X-s1",
        RevalidationDecision.GROWN,
        resplit_children=children,
    )

    by_id = {r.sub_scope_id: r for r in produced}
    assert by_id["PR-X-s1"].status is CampaignStatus.EJECTED  # superseded by its children
    assert by_id["PR-X-s1-a"].status is CampaignStatus.PENDING
    assert by_id["PR-X-s1-a"].resplit_depth == 1
    assert by_id["PR-X-s1-b"].resplit_depth == 1


def test_apply_revalidation_grown_past_cap_ejects_loud() -> None:
    """from: refinement B (eject fails loud) + §5 (re-split cap → ejected, not a 2nd-level split).

    A sub-scope already at ``MAX_RESPLIT_DEPTH`` that grows again is EJECTED (escalate), never
    carved a second time — and the ejection carries a non-empty, human-readable note.
    """
    history = [
        _state(status=CampaignStatus.READY, resplit_depth=MAX_RESPLIT_DEPTH),
    ]
    produced = apply_revalidation(
        history,
        "PR-X-s1",
        RevalidationDecision.GROWN,
        resplit_children=(_child("PR-X", "PR-X-s1-a"),),
    )
    assert len(produced) == 1  # no children carved — the cap was hit
    assert produced[0].status is CampaignStatus.EJECTED
    assert produced[0].note  # loud: a non-empty escalation reason
    assert "cap" in produced[0].note.lower()  # the cap note, distinct from the no-children note


def test_apply_revalidation_grown_with_no_children_ejects_with_a_distinct_note() -> None:
    """from: ptest-1 (review) — the GROWN no-children-produced eject is a SEPARATE branch from the
    cap-exceeded eject and carries its own distinguishable note (not just a shared 'escalate')."""
    history = list(seed_campaign(_linear_split("PR-X", 1), "PR-X"))
    history += tee_up(history)  # s1 ready at resplit_depth 0 (cap NOT hit)
    produced = apply_revalidation(history, "PR-X-s1", RevalidationDecision.GROWN)
    assert len(produced) == 1
    assert produced[0].status is CampaignStatus.EJECTED
    assert "no re-split" in produced[0].note.lower()  # the no-children reason, not the cap reason
    assert "cap" not in produced[0].note.lower()


def test_apply_revalidation_refuses_a_non_ready_sub_scope() -> None:
    """from: silent-2 (review) — re-validation applies ONLY to a READY sub-scope ('before it runs').

    The CHANGED branch builds via ``_supersede``, bypassing ``transition``'s legality guard, so
    without a precondition a CHANGED verdict could resurrect a terminal (e.g. MERGED) sub-scope
    back to READY. It must fail closed instead.
    """
    history = list(seed_campaign(_linear_split("PR-X", 1), "PR-X"))
    _drive_to_implementing(history, "PR-X-s1")
    history += advance_on_merge(history, "PR-X-s1")  # s1 is now MERGED (terminal)
    with pytest.raises(ValueError, match=r"READY|ready"):
        apply_revalidation(history, "PR-X-s1", RevalidationDecision.CHANGED)
    # the terminal record is untouched — no resurrection record was appended.
    final_s1 = reduce_current(history, campaign_id="PR-X").by_id("PR-X-s1")
    assert final_s1 is not None
    assert final_s1.status is CampaignStatus.MERGED


# ── gate-crossing guard (refinement A — symmetric) ────────────────────────────


def test_gate_1_planning_to_implementing_requires_external_event() -> None:
    """from: refinement A — Gate 1 (plan approval) is external-driven, never autonomous."""
    history = [_state(status=CampaignStatus.PLANNING)]
    with pytest.raises(ValueError, match="external"):
        transition(history, "PR-X-s1", CampaignStatus.IMPLEMENTING)
    crossed = transition(history, "PR-X-s1", CampaignStatus.IMPLEMENTING, external_event=True)
    assert crossed.status is CampaignStatus.IMPLEMENTING


def test_gate_2_implementing_to_merged_requires_external_event() -> None:
    """from: refinement A — Gate 2 (verify-and-merge) is external-driven, never autonomous."""
    history = [_state(status=CampaignStatus.IMPLEMENTING)]
    with pytest.raises(ValueError, match="external"):
        transition(history, "PR-X-s1", CampaignStatus.MERGED)
    crossed = transition(history, "PR-X-s1", CampaignStatus.MERGED, external_event=True)
    assert crossed.status is CampaignStatus.MERGED


def test_autonomous_tee_up_never_crosses_a_gate() -> None:
    """from: refinement A — the campaign tees up (ready→planning) but cannot cross Gate 1 itself."""
    history = [_state(status=CampaignStatus.READY)]
    teed = transition(history, "PR-X-s1", CampaignStatus.PLANNING)  # autonomous — no external
    assert teed.status is CampaignStatus.PLANNING
    history.append(teed)
    with pytest.raises(ValueError, match="external"):
        transition(history, "PR-X-s1", CampaignStatus.IMPLEMENTING)


def test_illegal_status_transition_is_rejected() -> None:
    """from: §4 step 2 (transition validates new_status via LEGAL_TRANSITIONS)."""
    history = [_state(status=CampaignStatus.PENDING)]
    with pytest.raises(ValueError, match=r"illegal|legal|transition"):
        transition(history, "PR-X-s1", CampaignStatus.MERGED)  # pending→merged is not legal


# ── reachability / no-dead-branch property ────────────────────────────────────


def test_every_status_is_reachable() -> None:
    """from: §5 (reachability property — no structurally-dead status; finding-039 lesson)."""
    reachable = {CampaignStatus.PENDING}  # the seed status
    for allowed in LEGAL_TRANSITIONS.values():
        reachable |= allowed
    assert reachable == set(CampaignStatus)


def test_every_legal_transition_fires() -> None:
    """from: §5 (no structurally-dead branch — every declared legal transition is exercisable)."""
    for from_status, allowed in LEGAL_TRANSITIONS.items():
        for to_status in allowed:
            history = [_state(status=from_status)]
            external = (from_status, to_status) in GATE_CROSSINGS
            record = transition(history, "PR-X-s1", to_status, external_event=external)
            assert record.status is to_status


def test_terminal_statuses_have_no_outgoing_transitions() -> None:
    """from: §4 step 1 (merged/moot/ejected are terminal — a campaign here is done for scope)."""
    for terminal in (CampaignStatus.MERGED, CampaignStatus.MOOT, CampaignStatus.EJECTED):
        assert LEGAL_TRANSITIONS[terminal] == frozenset()


def test_transition_maps_are_mutually_consistent() -> None:
    """from: type-2 (review) — the three constants can't silently drift apart as the enum grows.

    LEGAL_TRANSITIONS is exhaustive over CampaignStatus (no status without a key → no KeyError in
    ``transition``), TERMINAL_STATUSES is exactly the empty-outgoing set, and every GATE_CROSSINGS
    edge is itself a declared legal transition.
    """
    assert set(LEGAL_TRANSITIONS) == set(CampaignStatus)  # exhaustive — every status has a key
    assert frozenset(s for s in CampaignStatus if not LEGAL_TRANSITIONS[s]) == TERMINAL_STATUSES
    for src, dst in GATE_CROSSINGS:
        assert dst in LEGAL_TRANSITIONS[src]  # a gate crossing is a legal edge, just external-gated


# ── cancel ────────────────────────────────────────────────────────────────────


def test_cancel_skips_already_terminal_sub_scopes() -> None:
    """from: ptest-3 (review) — cancel ejects only the still-active sub-scopes; an already-MERGED
    (terminal) one is left untouched (cancel_campaign guards on TERMINAL_STATUSES)."""
    history = list(seed_campaign(_linear_split("PR-X", 2), "PR-X"))
    _drive_to_implementing(history, "PR-X-s1")
    history += advance_on_merge(history, "PR-X-s1")  # s1 MERGED, s2 READY

    ejections = cancel_campaign(history)

    assert {r.sub_scope_id for r in ejections} == {"PR-X-s2"}  # only the active sub-scope ejected
    assert all(r.status is CampaignStatus.EJECTED for r in ejections)
    final = reduce_current([*history, *ejections], campaign_id="PR-X")
    s1 = final.by_id("PR-X-s1")
    s2 = final.by_id("PR-X-s2")
    assert s1 is not None
    assert s1.status is CampaignStatus.MERGED  # untouched by cancel
    assert s2 is not None
    assert s2.status is CampaignStatus.EJECTED

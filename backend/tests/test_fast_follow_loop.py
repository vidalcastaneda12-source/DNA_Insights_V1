"""Loop / batcher + termination — ``plan_next_batch`` / ``group_drains`` / ``loop_done``.

Plan-blind spec source: synthesized-plan §4 ("Also loop/batcher: plan_next_batch with
seen_set dedup, MAX_ITEMS=10, MAX_BATCHES=3, explicit overflow + discards (no silent
truncation), loop_done predicate (dry or cap). Self-spawning nit can't loop (seen-set)."), §5
test list item 2 (batcher grouping + loop_done (dry/cap) + seen-set dedup / self-spawning-nit
termination), R4 (a handled key is EXCLUDED on the next scan — cross-invocation dedup), and
the FROZEN INTERFACE CONTRACT (``plan_next_batch(candidates, seen, batches_done) -> TriagePlan``;
``group_drains(triaged) -> tuple[tuple[Triage, ...], ...]``; ``loop_done(remaining) -> str |
None``; ``TriagePlan.counts()`` keys; ``TriagePlan.overflow`` / ``.discards`` / ``.termination``;
``Candidate.seen_key``; ``MAX_ITEMS`` / ``MAX_BATCHES``).

Every expected outcome comes from the §4 contract / frozen interface; nothing is
reverse-engineered from the stubbed bodies (all ``raise NotImplementedError`` now — RED is
correct).

Pre-mortem coverage (RANKED riskiest #1 / the §2 self-spawning-nit surprise): the seen-set
dedup test is the guard test proving a candidate already handled in a prior run cannot
re-surface into a batch — the loop cannot run forever.
"""

from __future__ import annotations

from genome.fast_follow.classifier import classify
from genome.fast_follow.loop import group_drains, loop_done, plan_next_batch
from genome.fast_follow.model import (
    MAX_BATCHES,
    MAX_ITEMS,
    Candidate,
    Classification,
    TriagePlan,
)


def _drain_candidate(
    cid: str,
    *,
    change_class: frozenset[str] = frozenset({"core"}),
    is_stale: bool = False,
) -> Candidate:
    """A guard-clearing DRAIN candidate with a distinct id (so seen_key differs per id)."""
    return Candidate(
        candidate_id=cid,
        source="repo-sweep",
        kind="doc-nit",
        change_class=change_class,
        blast_radius=1,
        applicable_anchors=0,
        tier="tier-0",
        touched_paths=(f"docs/notes/{cid}.md",),
        is_stale=is_stale,
    )


def _eject_candidate(cid: str) -> Candidate:
    """A candidate that classifies EJECT (guarded class)."""
    return _drain_candidate(cid, change_class=frozenset({"schema"}))


def _discard_candidate(cid: str) -> Candidate:
    """A candidate that classifies DISCARD (stale)."""
    return _drain_candidate(cid, is_stale=True)


# ── plan_next_batch: returns a TriagePlan; counts are correct ─────────────────


def test_plan_next_batch_returns_triage_plan_with_correct_counts() -> None:
    """from: plan §4 (plan_next_batch returns a TriagePlan) + frozen ``TriagePlan.counts()``.

    A mixed input of 2 drainable + 1 eject + 1 discard yields a TriagePlan whose counts()
    reports drain=2, eject=1, discard=1 (the keys the frozen contract pins).
    """
    candidates = [
        _drain_candidate("d1"),
        _drain_candidate("d2"),
        _eject_candidate("e1"),
        _discard_candidate("s1"),
    ]
    plan = plan_next_batch(candidates, seen=set(), batches_done=0)
    assert isinstance(plan, TriagePlan)
    counts = plan.counts()
    assert counts["drain"] == 2
    assert counts["eject"] == 1
    assert counts["discard"] == 1


def test_plan_next_batch_groups_drain_items() -> None:
    """from: plan §4 (DRAIN items grouped) + §5 batcher grouping.

    Every DRAIN classification in the plan's triaged set carries Classification.DRAIN; the
    plan separates the drainable work from the rest.
    """
    candidates = [_drain_candidate("d1"), _drain_candidate("d2"), _eject_candidate("e1")]
    plan = plan_next_batch(candidates, seen=set(), batches_done=0)
    drains = [t for t in plan.triaged if t.classification is Classification.DRAIN]
    assert len(drains) == 2


# ── Termination via loop_done(remaining) ──────────────────────────────────────


def test_loop_done_is_dry_when_only_eject_discard_remain() -> None:
    """from: plan §4 (loop_done returns "dry" when only eject/discard remain) + frozen contract.

    With no remaining DRAINable candidate (all eject/discard), loop_done reports the "dry"
    termination — the drain lane is empty.
    """
    remaining = [_eject_candidate("e1"), _discard_candidate("s1")]
    assert loop_done(remaining) == "dry"


def test_loop_done_is_none_when_drainable_remains() -> None:
    """from: plan §4 (loop_done returns None when work remains) + frozen contract."""
    remaining = [_drain_candidate("d1"), _eject_candidate("e1")]
    assert loop_done(remaining) is None


def test_loop_done_is_cap_at_max_items() -> None:
    """from: plan §4 (loop_done returns "cap" at MAX_ITEMS) + frozen ``MAX_ITEMS``.

    More than MAX_ITEMS drainable candidates remaining trips the "cap" termination — the loop
    stops at the bound rather than draining unbounded work.
    """
    remaining = [_drain_candidate(f"d{i}") for i in range(MAX_ITEMS + 1)]
    assert loop_done(remaining) == "cap"


# ── Overflow + discards are surfaced, never silently truncated ────────────────


def test_overflow_beyond_cap_is_surfaced_not_truncated() -> None:
    """from: plan §4 ("explicit overflow … no silent truncation") + frozen ``TriagePlan.overflow``.

    When more drainable candidates are present than the per-batch cap admits, the excess
    appears in ``TriagePlan.overflow`` — they are carried, never dropped. (Total accounted =
    triaged + overflow + discards; nothing vanishes.)
    """
    candidates = [_drain_candidate(f"d{i}") for i in range(MAX_ITEMS + 5)]
    plan = plan_next_batch(candidates, seen=set(), batches_done=0)
    triaged_drains = [t for t in plan.triaged if t.classification is Classification.DRAIN]
    # The cap admits at most MAX_ITEMS drain items this batch.
    assert len(triaged_drains) <= MAX_ITEMS
    # The excess is surfaced in overflow (non-empty), never silently dropped, and every input
    # candidate is accounted for across triaged + overflow + discards.
    assert len(plan.overflow) > 0
    accounted = len(plan.triaged) + len(plan.overflow) + len(plan.discards)
    assert accounted == len(candidates), "candidates were silently truncated"


def test_discards_are_surfaced_in_triage_plan() -> None:
    """from: plan §4 ("explicit … discards") + frozen ``TriagePlan.discards``.

    Stale candidates land in the plan's ``discards`` channel (visible, logged), not silently
    dropped.
    """
    candidates = [_drain_candidate("d1"), _discard_candidate("s1"), _discard_candidate("s2")]
    plan = plan_next_batch(candidates, seen=set(), batches_done=0)
    assert len(plan.discards) == 2
    for t in plan.discards:
        assert t.classification is Classification.DISCARD


def test_cap_termination_recorded_on_plan() -> None:
    """from: plan §4 (loop_done dry/cap) + frozen ``TriagePlan.termination`` ("cap" at MAX_BATCHES).

    When the batch index has reached MAX_BATCHES, the plan records the "cap" termination — the
    batch budget is exhausted.
    """
    candidates = [_drain_candidate(f"d{i}") for i in range(3)]
    plan = plan_next_batch(candidates, seen=set(), batches_done=MAX_BATCHES)
    assert plan.termination == "cap"


# ── Seen-set dedup / self-spawning-nit termination ────────────────────────────


def test_seen_key_excludes_already_handled_candidate() -> None:
    """from: plan §4 ("Self-spawning nit can't loop (seen-set)") + §5 seen-set dedup + R4.

    A candidate whose ``seen_key()`` is already in the ``seen`` set is EXCLUDED from the batch
    — it cannot re-surface, so the loop cannot run forever. This is the cross-invocation dedup
    R4 requires.
    """
    already = _drain_candidate("d1")
    fresh = _drain_candidate("d2")
    seen = {already.seen_key()}
    plan = plan_next_batch([already, fresh], seen=seen, batches_done=0)
    drained_ids = {t.candidate_id for t in plan.triaged if t.classification is Classification.DRAIN}
    assert "d1" not in drained_ids, "an already-seen candidate re-surfaced into the batch"
    assert "d2" in drained_ids


def test_all_candidates_seen_yields_empty_batch() -> None:
    """from: plan §4 (self-spawning-nit termination) + R4.

    If every candidate's seen_key is already in the seen set, the batch admits nothing — the
    loop terminates rather than re-processing handled items.
    """
    candidates = [_drain_candidate("d1"), _drain_candidate("d2")]
    seen = {c.seen_key() for c in candidates}
    plan = plan_next_batch(candidates, seen=seen, batches_done=0)
    drains = [t for t in plan.triaged if t.classification is Classification.DRAIN]
    assert drains == []


# ── group_drains: independent items grouped, deterministic ────────────────────


def test_group_drains_is_deterministic_for_independent_items() -> None:
    """from: plan §4 (group_drains independent items grouped; deterministic) + frozen signature.

    ``group_drains`` over the same triaged input twice yields identical grouping (a stable,
    order-deterministic partition) — the drain batches are reproducible.
    """
    candidates = [_drain_candidate(f"d{i}") for i in range(4)]
    plan = plan_next_batch(candidates, seen=set(), batches_done=0)
    drains = [t for t in plan.triaged if t.classification is Classification.DRAIN]
    g1 = group_drains(drains)
    g2 = group_drains(drains)
    assert g1 == g2
    # Every drained item appears exactly once across the groups (no loss, no duplication).
    flattened = [t for group in g1 for t in group]
    assert sorted(t.candidate_id for t in flattened) == sorted(t.candidate_id for t in drains)


# ── Ejects must not starve the drain lane (review: correctness-sweep #3) ───────


def test_ejects_do_not_consume_the_drain_budget() -> None:
    """from: review sweep-3 — the per-batch MAX_ITEMS cap bounds DRAINs only.

    MAX_ITEMS ejects ahead of 3 drains must NOT push those drains into overflow: ejects are
    cheap ROADMAP drafts, not drain work, so they don't consume the drain budget.
    """
    ejects = [_eject_candidate(f"e{i}") for i in range(MAX_ITEMS)]
    drains = [_drain_candidate(f"d{i}") for i in range(3)]
    plan = plan_next_batch(ejects + drains, seen=set(), batches_done=0)
    planned_drains = [t for t in plan.triaged if t.classification is Classification.DRAIN]
    assert len(planned_drains) == 3
    assert len(plan.overflow) == 0


def test_only_drains_overflow_not_ejects() -> None:
    """from: review sweep-3 — overflow is DRAIN-only; ejects are always planned, not overflowed."""
    candidates = [_drain_candidate(f"d{i}") for i in range(MAX_ITEMS + 2)]
    candidates += [_eject_candidate(f"e{i}") for i in range(5)]
    plan = plan_next_batch(candidates, seen=set(), batches_done=0)
    assert all(t.classification is Classification.DRAIN for t in plan.overflow)
    planned_ejects = [t for t in plan.triaged if t.classification is Classification.EJECT]
    assert len(planned_ejects) == 5


# ── loop_done / group_drains edge cases (review: ptest-4, ptest-5) ────────────


def test_loop_done_empty_remaining_is_dry() -> None:
    """from: review ptest-4 — loop_done([]) (drain lane empty) → 'dry'."""
    assert loop_done([]) == "dry"


def test_group_drains_no_drain_verdicts_is_empty_tuple() -> None:
    """from: review ptest-5 — group_drains over an all-EJECT triaged sequence → ()."""
    eject_triages = tuple(classify(_eject_candidate(f"e{i}")) for i in range(3))
    assert all(t.classification is Classification.EJECT for t in eject_triages)
    assert group_drains(eject_triages) == ()

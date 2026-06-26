"""The batcher / dedup / termination layer for the fast-follow drain loop (``finding-038``).

Deliberately split from :mod:`genome.fast_follow.classifier` (plan A4): the classifier is
the pure ``Candidate → Classification`` reducer; this module owns the *loop* — the
seen-set dedup, the per-batch :data:`~genome.fast_follow.model.MAX_ITEMS` /
:data:`~genome.fast_follow.model.MAX_BATCHES` bounds, the explicit overflow / discard
partitions (no silent truncation), and the ``dry`` / ``cap`` termination predicate. The
self-spawning nit cannot loop because a handled candidate's ``seen_key`` is excluded on the
next scan. **No** :mod:`genome.db` import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genome.fast_follow.classifier import classify
from genome.fast_follow.model import (
    MAX_BATCHES,
    MAX_ITEMS,
    Classification,
    TriagePlan,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from genome.fast_follow.model import Candidate, Triage


def plan_next_batch(
    candidates: Sequence[Candidate],
    seen: set[str],
    batches_done: int,
) -> TriagePlan:
    """Triage one batch of candidates into a :class:`~genome.fast_follow.model.TriagePlan`.

    Drops any candidate whose ``seen_key`` is already in ``seen`` (cross-invocation dedup,
    plan R4), classifies the rest via :func:`genome.fast_follow.classifier.classify`, caps
    the planned set at :data:`~genome.fast_follow.model.MAX_ITEMS` with the remainder routed
    to explicit overflow (never truncated silently), and stamps the termination summary
    from :func:`loop_done` given ``batches_done``. Pure over its inputs — the seen-set
    *persistence* is :mod:`genome.fast_follow.persistence`'s job, not this function's.
    """
    # Cross-invocation dedup: a handled candidate's seen_key is excluded this scan.
    fresh = [c for c in candidates if c.seen_key() not in seen]

    triaged: list[Triage] = []
    overflow: list[Triage] = []
    discards: list[Triage] = []
    # The DRAIN candidates that still want draining after this batch — the loop_done input.
    overflow_candidates: list[Candidate] = []

    # The per-batch cap (MAX_ITEMS) bounds the DRAIN items only — the work that goes through
    # A's gate. EJECTs are cheap ROADMAP drafts and DISCARDs are terminal, so neither consumes
    # the drain budget (else a batch full of ejects would starve the drain lane). Only DRAINs
    # beyond the cap overflow to a later batch.
    drain_count = 0
    for candidate in fresh:
        triage = classify(candidate)
        classification = triage.classification
        if classification is Classification.DISCARD:
            discards.append(triage)
            continue
        if classification is Classification.EJECT:
            triaged.append(triage)
            continue
        # DRAIN — subject to the per-batch item cap; the excess overflows (never dropped).
        if drain_count < MAX_ITEMS:
            triaged.append(triage)
            drain_count += 1
        else:
            overflow.append(triage)
            overflow_candidates.append(candidate)

    # Termination summary: once the batch budget (MAX_BATCHES) is spent the cap is the binding
    # stop regardless of what remains; otherwise defer to loop_done over the actionable overflow
    # (the drainable work that did not fit this batch).
    if batches_done >= MAX_BATCHES:
        termination: str | None = "cap"
    else:
        termination = loop_done(overflow_candidates)

    return TriagePlan(
        triaged=tuple(triaged),
        overflow=tuple(overflow),
        discards=tuple(discards),
        dry=False,
        termination=termination,
    )


def group_drains(triaged: Sequence[Triage]) -> tuple[tuple[Triage, ...], ...]:
    """Group the DRAIN verdicts into per-batch drain groups (plan §4 loop).

    Partitions the :attr:`~genome.fast_follow.model.Classification.DRAIN` verdicts into the
    grouped batches Sub-A's verify-and-merge gate consumes one group at a time. Pure over
    ``triaged``; ignores EJECT / DISCARD verdicts. The DRAINs are chunked at
    :data:`~genome.fast_follow.model.MAX_ITEMS` per group.
    """
    drains = [t for t in triaged if t.classification is Classification.DRAIN]
    if not drains:
        return ()
    return tuple(
        tuple(drains[start : start + MAX_ITEMS]) for start in range(0, len(drains), MAX_ITEMS)
    )


def loop_done(remaining: Sequence[Candidate]) -> str | None:
    """The loop-termination predicate (plan §4 loop).

    Keys on the *drainable* candidates still remaining (a candidate is drainable iff it
    classifies :attr:`~genome.fast_follow.model.Classification.DRAIN`): returns ``"dry"`` when
    nothing drainable remains (only eject / discard left — the drain lane is empty), ``"cap"``
    when more than :data:`~genome.fast_follow.model.MAX_ITEMS` drainable candidates remain (a
    single batch's item budget cannot clear them), and ``None`` when the loop should continue.
    The two-condition stop (dry or cap) bounds the loop alongside the seen-set dedup.
    """
    drainable = [c for c in remaining if classify(c).classification is Classification.DRAIN]
    if not drainable:
        return "dry"
    if len(drainable) > MAX_ITEMS:
        return "cap"
    return None

"""The fail-closed Candidate → Classification reducer for the fast-follow loop (``finding-038``).

This is the heart of the loop — the pure single-concern analogue of
:mod:`genome.verify_gate.verdict`. It reduces one :class:`~genome.fast_follow.model.Candidate`
to one :class:`~genome.fast_follow.model.Triage`, and nothing else: the batcher / seen-set /
caps live in :mod:`genome.fast_follow.loop` (plan A4), so the exhaustive property test can
target this one pure function. **No** :mod:`genome.db` import — pure data → verdict.

Fail-closed reduction order (plan §4 classifier), DRAIN is reachable only past every guard:

1. **Extraction fail-closed** — any of ``change_class`` (empty) / ``applicable_anchors`` /
   ``blast_radius`` / ``tier`` undecidable (``None``) → :attr:`Classification.EJECT`.
2. **touched_paths independent guard** — any path under ``docs/schemas/**`` or ``ddl/**``
   → :attr:`Classification.EJECT` (catches a schema item mislabeled ``core``, plan A2).
3. **stale / already-handled** (``is_stale``) → :attr:`Classification.DISCARD` (logged).
4. **guarded class** (intersects :data:`~genome.fast_follow.model.GUARDED_CLASSES`) **OR**
   ``applicable_anchors != 0`` **OR** ``blast_radius >
   ``:data:`~genome.fast_follow.model.MAX_DRAIN_FILES` → :attr:`Classification.EJECT`.
5. else Tier-0 / bounded-Tier-1 → :attr:`Classification.DRAIN` (with drain provenance).

The SAFETY INVARIANT (plan §2): no candidate carrying a guarded class, a non-empty anchor
set, an over-cap blast_radius, or a ``docs/schemas/**`` / ``ddl/**`` touched path is EVER
classified DRAIN. The exhaustive property test enumerates (not samples) this invariant.

This file is a **stub** for the interface-freeze step: :func:`classify` raises
:class:`NotImplementedError` so plan-blind tests are honestly RED.
"""

from __future__ import annotations

from genome.fast_follow.model import (
    GUARDED_CLASSES,
    MAX_DRAIN_FILES,
    Candidate,
    Classification,
    Triage,
)

#: The literal path prefixes the independent guard (step 2) EJECTs on (plan A2). Keyed on the
#: candidate's literal ``touched_paths``, never on its derived ``change_class`` label — a
#: schema item the skill mislabels ``core`` still EJECTs on its ``docs/schemas/**`` / ``ddl/**``
#: path. These mirror the immutable schema/DDL roots in CLAUDE.md "Things never to do".
_GUARDED_PATH_PREFIXES: tuple[str, ...] = ("docs/schemas/", "ddl/")


def _touches_guarded_path(touched_paths: tuple[str, ...]) -> bool:
    """``True`` when any literal touched path falls under a guarded schema/DDL root (plan A2)."""
    return any(
        path.startswith(prefix) for path in touched_paths for prefix in _GUARDED_PATH_PREFIXES
    )


def classify(candidate: Candidate) -> Triage:  # noqa: PLR0911 — one return per fail-closed guard
    """Reduce one candidate to its fail-closed :class:`~genome.fast_follow.model.Triage`.

    See the module docstring for the full reduction order. The single pure concern: a
    Candidate in, a Triage out, no I/O, no batching. DRAIN is returned only when every
    guard (extraction, path, stale, guarded-class/anchor/blast) has been cleared. Each
    :class:`~genome.fast_follow.model.Triage` records a ``reason`` and — for a DRAIN — the
    backlog item it ``drains`` (provenance, decision #8).
    """
    cid = candidate.candidate_id

    # 1. Extraction fail-closed: any decision-bearing field undecidable → EJECT. An empty
    #    change_class is the "no class" / unclassified case and is treated as undecidable.
    if (
        not candidate.change_class
        or candidate.blast_radius is None
        or candidate.applicable_anchors is None
        or candidate.tier is None
    ):
        return Triage(
            candidate_id=cid,
            classification=Classification.EJECT,
            retier=None,
            reason=(
                "extraction fail-closed: a decision-bearing field "
                "(change_class / blast_radius / applicable_anchors / tier) is undecidable"
            ),
            drains=None,
        )

    # 2. touched_paths INDEPENDENT guard: a literal path under docs/schemas/** or ddl/**
    #    EJECTs regardless of the derived change_class label (plan A2).
    if _touches_guarded_path(candidate.touched_paths):
        return Triage(
            candidate_id=cid,
            classification=Classification.EJECT,
            retier=None,
            reason=(
                "touched_paths guard: a literal path under docs/schemas/** or ddl/** is immutable"
            ),
            drains=None,
        )

    # 3. stale / already-handled → DISCARD (logged by the loop / formatter).
    if candidate.is_stale:
        return Triage(
            candidate_id=cid,
            classification=Classification.DISCARD,
            retier=None,
            reason="stale: candidate already handled or no longer applicable",
            drains=None,
        )

    # 4. guarded class OR anchor-exposed OR over-cap blast_radius → EJECT.
    guarded = sorted(candidate.change_class & GUARDED_CLASSES)
    if guarded:
        return Triage(
            candidate_id=cid,
            classification=Classification.EJECT,
            retier=None,
            reason=f"guarded change class {guarded}: carries anchors / rebuild obligations",
            drains=None,
        )
    if candidate.applicable_anchors != 0:
        return Triage(
            candidate_id=cid,
            classification=Classification.EJECT,
            retier=None,
            reason=(
                f"anchor-exposed: applicable_anchors={candidate.applicable_anchors} "
                "(a real-data anchor would move)"
            ),
            drains=None,
        )
    if candidate.blast_radius > MAX_DRAIN_FILES:
        return Triage(
            candidate_id=cid,
            classification=Classification.EJECT,
            retier=None,
            reason=(
                f"over-cap blast_radius={candidate.blast_radius} "
                f"> MAX_DRAIN_FILES={MAX_DRAIN_FILES}"
            ),
            drains=None,
        )

    # 5. Every guard cleared → DRAIN, recording which backlog item it drains (provenance).
    return Triage(
        candidate_id=cid,
        classification=Classification.DRAIN,
        retier=candidate.tier,
        reason=(
            f"drainable {candidate.tier}: change_class={sorted(candidate.change_class)}, "
            f"blast_radius={candidate.blast_radius}, no anchors, no guarded path"
        ),
        drains=cid,
    )

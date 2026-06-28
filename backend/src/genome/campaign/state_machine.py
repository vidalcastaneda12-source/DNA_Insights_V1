"""Pure state-machine reducers for the campaign orchestrator (``finding-041``; B2 Phase 2).

The reducers over the append-only ledger — **no I/O, no** :mod:`genome.db`, **no**
:mod:`genome.config`. Every reducer is a pure function of the history (a sequence of
:class:`~genome.campaign.model.SubScopeState` records) and produces NEW records; appending them to
the ledger is :mod:`genome.campaign.persistence`'s job (the I/O-out-of-the-core split, mirroring
:mod:`genome.fast_follow.persistence` / :mod:`genome.calibration.persistence`).

The locked-#7 contract realized here: a status change is :func:`transition` — an INSERT of a record
that supersedes the prior (``record_seq`` = the ledger max + 1, ``supersedes`` = the prior seq),
never an in-place edit. The two human gates are **symmetrically** external-event-gated (Gate-1
refinement A): :func:`transition` raises on ``PLANNING → IMPLEMENTING`` (Gate 1, plan approval) or
``IMPLEMENTING → MERGED`` (Gate 2, verify-and-merge) unless ``external_event=True`` — so the
campaign sequences and tees up, but crosses **neither** gate autonomously.

The GROWN re-validation's *decision* (is this sub-scope now moot / changed / grown, and into what
children) is the model-driven shell's job — a re-dispatch + ``propose_split``
scan (I/O). This module only consumes the already-chosen verdict (and, for a carve, the
shell-produced child mini-manifests) and applies the pure record mechanics, so the reducers stay
I/O-free on every path (finding-038 ESC-2). The re-split cap reuses
:data:`~genome.scope_split.model.MAX_RESPLIT_DEPTH` (no redefinition).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genome.campaign.model import (
    GATE_CROSSINGS,
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    CampaignState,
    CampaignStatus,
    RevalidationDecision,
    SubScopeState,
)
from genome.scope_split.model import MAX_RESPLIT_DEPTH

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from genome.scope_split.model import SplitResult, SubScope


# ── Seed ─────────────────────────────────────────────────────────────────────


def seed_campaign(split: SplitResult, origin_scope: str) -> tuple[SubScopeState, ...]:
    """Seed a campaign's initial ledger from a non-atomic split (design §2 Part 2).

    Produces one ``PENDING`` record per proposed sub-scope, in the split's topological order
    (``record_seq`` 0..N-1, ``supersedes`` ``None``), keyed on the stable placeholder
    ``sub_scope_id`` (``<origin>-sN``; minting a real PR-N id stays the human's call at the
    micro-gate, finding-039). Each record carries the sub-scope's mini-manifest as its
    ``manifest_snapshot`` (provenance #8). Raises :class:`ValueError` on an atomic split — there is
    nothing to sequence.
    """
    if split.atomic:
        msg = "cannot seed a campaign from an atomic SplitResult (there is nothing to sequence)"
        raise ValueError(msg)
    return tuple(
        _seed_record(seq, sub, origin_scope, resplit_depth=0, note="seeded")
        for seq, sub in enumerate(split.sub_scopes)
    )


# ── Transition (the insert-then-flip primitive) ──────────────────────────────


def transition(
    history: Sequence[SubScopeState],
    sub_scope_id: str,
    new_status: CampaignStatus,
    *,
    external_event: bool = False,
    note: str = "",
) -> SubScopeState:
    """Build the record that transitions ``sub_scope_id`` to ``new_status`` (locked #7 — pure).

    Validates ``new_status`` against :data:`~genome.campaign.model.LEGAL_TRANSITIONS` for the
    sub-scope's current status, then enforces the symmetric human-gate guard (Gate-1 refinement
    A): a transition in :data:`~genome.campaign.model.GATE_CROSSINGS` (Gate 1 ``PLANNING →
    IMPLEMENTING`` / Gate 2 ``IMPLEMENTING → MERGED``) requires ``external_event=True`` — else it
    raises, so the campaign can never cross a human gate on its own. Returns the new (superseding)
    record; appending it is persistence's job.
    """
    current = _current_record(history, sub_scope_id)
    allowed = LEGAL_TRANSITIONS[current.status]
    if new_status not in allowed:
        msg = (
            f"illegal transition {current.status.value} → {new_status.value} for "
            f"sub_scope {sub_scope_id!r}; allowed next: {sorted(s.value for s in allowed)!r}"
        )
        raise ValueError(msg)
    if (current.status, new_status) in GATE_CROSSINGS and not external_event:
        msg = (
            f"transition {current.status.value} → {new_status.value} crosses a human gate; "
            "external_event=True is required (the campaign never crosses a gate autonomously)"
        )
        raise ValueError(msg)
    return _supersede(history, sub_scope_id, new_status, note=note)


# ── Sequencing: tee-up + advance-on-merge ────────────────────────────────────


def tee_up(history: Sequence[SubScopeState]) -> list[SubScopeState]:
    """Promote every deps-satisfied ``PENDING`` sub-scope to ``READY`` (design §2 Part 2 — pure).

    A sub-scope is ready when **every** dependency has reached a *resolved* status (``MERGED`` or
    ``MOOT``); an ``EJECTED`` dependency does NOT satisfy a dependency (it was escalated, not
    completed), so its dependents stay blocked. Returns the new ``READY`` records (one per newly
    unblocked sub-scope, with distinct ``record_seq``s); appending them is persistence's job.
    """
    campaign_id = _campaign_id_of(history)
    state = reduce_current(history, campaign_id=campaign_id)
    working: list[SubScopeState] = list(history)
    promoted: list[SubScopeState] = []
    for sub in state.sub_scopes:
        if sub.status is CampaignStatus.PENDING and _deps_satisfied(state, sub):
            record = transition(
                working, sub.sub_scope_id, CampaignStatus.READY, note="deps satisfied"
            )
            promoted.append(record)
            working.append(record)
    return promoted


def advance_on_merge(history: Sequence[SubScopeState], sub_scope_id: str) -> list[SubScopeState]:
    """Merge ``sub_scope_id`` (Gate 2) and tee up any newly unblocked dependents (design §2 Part 2).

    Produces the ``IMPLEMENTING → MERGED`` record (an external Gate-2 event) followed by a
    ``READY`` record for each dependent whose dependencies are now all resolved. The merged record
    and the readied dependents are written together by persistence in one atomic append, so a torn
    write can never leave the merge without its readied dependents.
    """
    merged = transition(
        history,
        sub_scope_id,
        CampaignStatus.MERGED,
        external_event=True,
        note="merged at Gate 2 (verify-and-merge)",
    )
    readied = tee_up([*history, merged])
    return [merged, *readied]


def cancel_campaign(history: Sequence[SubScopeState]) -> list[SubScopeState]:
    """Cancel a campaign by ejecting every active non-terminal sub-scope (refinement C — pure).

    Cancellation is append-only, like every other transition: each active sub-scope still in a
    non-terminal status gets an ``EJECTED`` record with a distinguishing operator note, so a
    cancelled campaign reloads cleanly to an all-terminal state while its full history (the
    original ``PENDING`` seed and every intermediate record) stays intact. A sub-scope already
    terminal (merged / moot / ejected) is left untouched. Appending the records is persistence's
    job; this returns them in a single batch for one atomic write.
    """
    state = reduce_current(history, campaign_id=_campaign_id_of(history))
    working: list[SubScopeState] = list(history)
    ejections: list[SubScopeState] = []
    for sub in state.sub_scopes:
        if sub.status not in TERMINAL_STATUSES:
            record = transition(
                working,
                sub.sub_scope_id,
                CampaignStatus.EJECTED,
                note="campaign cancelled by operator",
            )
            ejections.append(record)
            working.append(record)
    return ejections


# ── Adaptive re-validation (moot / changed / grown) ──────────────────────────


def apply_revalidation(
    history: Sequence[SubScopeState],
    sub_scope_id: str,
    decision: RevalidationDecision,
    *,
    updated_manifest_snapshot: Mapping[str, object] | None = None,
    resplit_children: Sequence[SubScope] | None = None,
) -> list[SubScopeState]:
    """Apply a re-dispatch verdict to a ``READY`` sub-scope before it runs (design §2; pure).

    * ``STILL_NEEDED`` → run it: ``READY → PLANNING``.
    * ``MOOT`` → skip it: ``READY → MOOT`` (resolves the dependency for its dependents).
    * ``CHANGED`` → re-propose: stays ``READY`` with new ``updated_manifest_snapshot``
      (a content-only supersession — a new record, prior bytes untouched).
    * ``GROWN`` → re-split into the shell-supplied ``resplit_children`` (each a new ``PENDING``
      record at ``resplit_depth + 1``) and eject the original. At
      :data:`~genome.scope_split.model.MAX_RESPLIT_DEPTH` (or with no children supplied) the
      original is ejected with a loud escalation note instead of carved a second time (the cap,
      refinement B — eject fails loud).
    """
    current = _current_record(history, sub_scope_id)
    if current.status is not CampaignStatus.READY:
        # Re-validation is a READY-stage gate. The CHANGED branch builds via _supersede (no
        # transition-legality check), so without this precondition a stale CHANGED verdict could
        # resurrect a terminal sub-scope. Fail closed — every verdict requires a READY current.
        msg = (
            "re-validation applies only to a READY sub-scope (design §2 Part 2 — 'before it "
            f"runs'); {sub_scope_id!r} is {current.status.value}"
        )
        raise ValueError(msg)
    if decision is RevalidationDecision.STILL_NEEDED:
        return [
            transition(
                history, sub_scope_id, CampaignStatus.PLANNING, note="re-validated: still needed"
            )
        ]
    if decision is RevalidationDecision.MOOT:
        return [
            transition(
                history, sub_scope_id, CampaignStatus.MOOT, note="re-validated: moot — skipped"
            )
        ]
    if decision is RevalidationDecision.CHANGED:
        snapshot = (
            current.manifest_snapshot
            if updated_manifest_snapshot is None
            else updated_manifest_snapshot
        )
        changed = _supersede(
            history,
            sub_scope_id,
            CampaignStatus.READY,
            note="re-validated: changed — re-proposed",
            manifest_snapshot=snapshot,
        )
        return [changed]
    return _apply_grown(history, current, resplit_children)


def _apply_grown(
    history: Sequence[SubScopeState],
    current: SubScopeState,
    resplit_children: Sequence[SubScope] | None,
) -> list[SubScopeState]:
    """The GROWN branch: carve children within the cap, else eject loud (refinement B + the cap)."""
    if current.resplit_depth >= MAX_RESPLIT_DEPTH:
        note = (
            f"re-validated: grown past the re-split cap (resplit_depth={current.resplit_depth} "
            f">= MAX_RESPLIT_DEPTH={MAX_RESPLIT_DEPTH}); ejected — escalate to a human"
        )
        return [transition(history, current.sub_scope_id, CampaignStatus.EJECTED, note=note)]
    if not resplit_children:
        note = "re-validated: grown but no re-split was produced; ejected — escalate to a human"
        return [transition(history, current.sub_scope_id, CampaignStatus.EJECTED, note=note)]

    child_ids = ", ".join(child.sub_scope_id for child in resplit_children)
    eject = transition(
        history,
        current.sub_scope_id,
        CampaignStatus.EJECTED,
        note=f"re-validated: grown — re-split into {child_ids}",
    )
    working: list[SubScopeState] = [*history, eject]
    records: list[SubScopeState] = [eject]
    child_depth = current.resplit_depth + 1
    for child in resplit_children:
        record = _seed_record(
            _next_seq(working),
            child,
            current.origin_scope,
            resplit_depth=child_depth,
            note="seeded (re-split child)",
        )
        records.append(record)
        working.append(record)
    return records


# ── Reduction: the derived current view ──────────────────────────────────────


def reduce_current(history: Sequence[SubScopeState], *, campaign_id: str) -> CampaignState:
    """Reduce the append-only ledger to its current view — latest-active per sub-scope (locked #7).

    The active record for a ``sub_scope_id`` is the one with the highest ``record_seq``; superseded
    records drop out of the view but stay in ``history``. Sub-scopes are ordered by first
    appearance (the seed / topological order). The full history is never mutated — this is a pure
    projection.
    """
    latest: dict[str, SubScopeState] = {}
    first_seen: dict[str, int] = {}
    for record in history:
        first_seen.setdefault(record.sub_scope_id, record.record_seq)
        existing = latest.get(record.sub_scope_id)
        if existing is None or record.record_seq > existing.record_seq:
            latest[record.sub_scope_id] = record
    ordered = sorted(latest.values(), key=lambda record: first_seen[record.sub_scope_id])
    return CampaignState(campaign_id=campaign_id, sub_scopes=tuple(ordered))


def next_ready(state: CampaignState) -> SubScopeState | None:
    """Return the next sub-scope to run — the first ``READY`` in order, or ``None`` (§2)."""
    for sub in state.sub_scopes:
        if sub.status is CampaignStatus.READY:
            return sub
    return None


# ── Private helpers ──────────────────────────────────────────────────────────


def _seed_record(
    seq: int,
    sub: SubScope,
    origin_scope: str,
    *,
    resplit_depth: int,
    note: str,
) -> SubScopeState:
    """Build an initial ``PENDING`` record from a proposed sub-scope manifest (seed/carve)."""
    return SubScopeState(
        record_seq=seq,
        sub_scope_id=sub.sub_scope_id,
        status=CampaignStatus.PENDING,
        origin_scope=origin_scope,
        manifest_snapshot=dict(sub.to_json()),
        depends_on=sub.depends_on,
        supersedes=None,
        resplit_depth=resplit_depth,
        note=note,
    )


def _supersede(  # noqa: PLR0913 — record-construction primitive; each arg is a distinct ledger input
    history: Sequence[SubScopeState],
    sub_scope_id: str,
    new_status: CampaignStatus,
    *,
    note: str = "",
    manifest_snapshot: Mapping[str, object] | None = None,
    resplit_depth: int | None = None,
) -> SubScopeState:
    """Build the record that supersedes the current active record for ``sub_scope_id`` (locked #7).

    Carries forward ``origin_scope`` / ``depends_on`` / ``manifest_snapshot`` / ``resplit_depth``
    from the current record unless explicitly overridden, sets ``supersedes`` to the current
    record's seq, and assigns the next monotonic ``record_seq``. No legality / gate check here —
    that is :func:`transition`'s job; this is the shared record-construction primitive.
    """
    current = _current_record(history, sub_scope_id)
    return SubScopeState(
        record_seq=_next_seq(history),
        sub_scope_id=sub_scope_id,
        status=new_status,
        origin_scope=current.origin_scope,
        manifest_snapshot=current.manifest_snapshot
        if manifest_snapshot is None
        else manifest_snapshot,
        depends_on=current.depends_on,
        supersedes=current.record_seq,
        resplit_depth=current.resplit_depth if resplit_depth is None else resplit_depth,
        note=note,
    )


def _current_record(history: Sequence[SubScopeState], sub_scope_id: str) -> SubScopeState:
    """Return the latest (highest ``record_seq``) record for ``sub_scope_id`` or raise if absent."""
    matching = [record for record in history if record.sub_scope_id == sub_scope_id]
    if not matching:
        msg = f"no record for sub_scope {sub_scope_id!r} in the campaign history"
        raise ValueError(msg)
    return max(matching, key=lambda record: record.record_seq)


def _deps_satisfied(state: CampaignState, sub: SubScopeState) -> bool:
    """``True`` when every dependency of ``sub`` has reached a resolved (merged / moot) status."""
    resolved = {CampaignStatus.MERGED, CampaignStatus.MOOT}
    for dep_id in sub.depends_on:
        dep = state.by_id(dep_id)
        if dep is None or dep.status not in resolved:
            return False
    return True


def _campaign_id_of(history: Sequence[SubScopeState]) -> str:
    """Derive the campaign id (the shared ``origin_scope``) from a non-empty history."""
    if not history:
        msg = "cannot derive a campaign id from an empty history"
        raise ValueError(msg)
    return history[0].origin_scope


def _next_seq(history: Sequence[SubScopeState]) -> int:
    """The next monotonic ``record_seq`` — ledger max + 1 (deterministic from loaded history)."""
    return max((record.record_seq for record in history), default=-1) + 1

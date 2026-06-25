"""Render a fast-follow triage plan as a human-readable block (``finding-038``).

``format_triage_plan`` turns a :class:`~genome.fast_follow.model.TriagePlan` into the
plain-text block the operator reads at touchpoint-1 (triage approval) — the per-item
verdicts with their reasons, the drain / eject / discard counts, and the overflow /
termination summary. ``format_eject_draft`` renders the EJECT verdicts as a draft for the
human to paste into ``/scope-run`` (drafts-to-stdout, never an autonomous ROADMAP write).

**No** :mod:`genome.db` import. **No anchor magnitudes hard-coded in this module's source**
(plan §4 / §6): every number in the output comes from the plan at runtime, so the real-data
anchors are never transcribed into code.

This file is a **stub** for the interface-freeze step: every body raises
:class:`NotImplementedError` so plan-blind tests are honestly RED.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genome.fast_follow.model import Classification

if TYPE_CHECKING:
    from collections.abc import Sequence

    from genome.fast_follow.model import Triage, TriagePlan

#: Emitted in place of the drain section when no candidate is drainable this batch (every
#: item EJECTed / DISCARDed). A literal sentinel, never a number — the ``--dry-run`` smoke
#: and the doc-consistency test key on it.
NOTHING_DRAINABLE_SENTINEL: str = (
    "Nothing drainable this batch — every candidate ejected or discarded"
)


def _render_reasons(triaged: Sequence[Triage]) -> list[str]:
    """Render a ``candidate_id: reason`` line per verdict, or a single ``(none)`` placeholder."""
    if not triaged:
        return ["  (none)"]
    return [f"  - {triage.candidate_id}: {triage.reason}" for triage in triaged]


def format_triage_plan(plan: TriagePlan) -> str:
    """Render the triage plan as the raw text block the operator reviews (plan §4).

    Includes each item's verdict + reason, the per-disposition counts (drain / eject /
    discard), the overflow partition, and the termination summary — or
    :data:`NOTHING_DRAINABLE_SENTINEL` when no item is drainable. Contains no hard-coded
    anchor magnitude — all numbers originate from ``plan`` at call time.
    """
    counts = plan.counts()
    lines: list[str] = []
    lines.append("FAST-FOLLOW TRIAGE PLAN")
    lines.append(
        f"counts: drain={counts['drain']} eject={counts['eject']} discard={counts['discard']}"
    )
    lines.append("")

    drains = [t for t in plan.triaged if t.classification is Classification.DRAIN]
    lines.append("Drains:")
    if drains:
        for triage in drains:
            tier = triage.retier or "-"
            lines.append(
                f"  - {triage.candidate_id} [{tier}] drains={triage.drains}: {triage.reason}"
            )
    else:
        lines.append(f"  {NOTHING_DRAINABLE_SENTINEL}")
    lines.append("")

    ejects = [t for t in plan.triaged if t.classification is Classification.EJECT]
    lines.append("Ejects:")
    lines.extend(_render_reasons(ejects))
    lines.append("")

    lines.append("Discards:")
    lines.extend(_render_reasons(plan.discards))
    lines.append("")

    lines.append("Overflow (deferred to a later batch, never dropped):")
    lines.extend(_render_reasons(plan.overflow))
    lines.append("")

    termination = plan.termination or "continue"
    lines.append(f"termination: {termination}")
    return "\n".join(lines)


def format_eject_draft(triaged: Sequence[Triage]) -> str:
    """Render the EJECT verdicts as a paste-ready ``/scope-run`` draft (plan OQ-3).

    Drafts-to-stdout only: the caller prints the returned string for a human to paste; this
    never writes ROADMAP.md or any file. Renders one ROADMAP-style block per EJECTed
    candidate, recording the source candidate; ignores DRAIN / DISCARD verdicts.
    """
    ejects = [t for t in triaged if t.classification is Classification.EJECT]
    lines: list[str] = []
    lines.append("# EJECT draft — paste into /scope-run (this is a draft, not an autonomous write)")
    lines.append("")
    if not ejects:
        lines.append("(no ejected candidates — nothing to draft)")
        return "\n".join(lines)
    for triage in ejects:
        lines.append(f"- [ ] {triage.candidate_id}")
        lines.append(f"      source-candidate: {triage.candidate_id}")
        lines.append(f"      eject-reason: {triage.reason}")
        lines.append("")
    return "\n".join(lines).rstrip()

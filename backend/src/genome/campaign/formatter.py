"""Render campaign state as human-readable text (``finding-041``; B2 Phase 2).

``format_campaign_status`` renders the current view as the block an operator reads at
``genome campaign status`` (one line per sub-scope: status, id, dependencies, origin, and — for a
terminal off-ramp — its escalation note, so an ejected sub-scope is **never a silent drop**,
Gate-1 refinement B). ``format_campaign_roadmap_block`` renders the same current view as the
B2-SUBSCOPES managed-block BODY that :func:`genome.scope_split.roadmap_writer.append_roadmap_block`
splices between its sentinels — the campaign owns only this block renderer, never a second writer
or a second managed region.

**No** :mod:`genome.db` import. **No magnitudes hard-coded** — every value in the output
originates from the :class:`~genome.campaign.model.CampaignState` at call time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genome.campaign.model import CampaignStatus

if TYPE_CHECKING:
    from genome.campaign.model import CampaignState, SubScopeState

#: The header line for the ``genome campaign status`` human-readable block.
STATUS_HEADER: str = "CAMPAIGN STATUS —"

#: Statuses rendered as a checked ROADMAP box — terminal-resolved (merged / moot won't be worked
#: further). ``EJECTED`` stays unchecked: it is terminal but needs human escalation, not a tick.
_DONE_STATUSES: frozenset[CampaignStatus] = frozenset(
    {CampaignStatus.MERGED, CampaignStatus.MOOT},
)

#: Statuses whose ``note`` carries a decision-bearing reason worth surfacing (the off-ramps — the
#: moot/eject escalations). Refinement B: an ejected sub-scope shows its note, never drops silently.
_NOTE_STATUSES: frozenset[CampaignStatus] = frozenset(
    {CampaignStatus.MOOT, CampaignStatus.EJECTED},
)


def _note_suffix(sub: SubScopeState, *, joiner: str) -> str:
    """The ``joiner``-prefixed escalation note for an off-ramp sub-scope, or empty otherwise."""
    if sub.note and sub.status in _NOTE_STATUSES:
        return f"{joiner}{sub.note}"
    return ""


def format_campaign_status(state: CampaignState) -> str:
    """Render the current view as the ``genome campaign status`` block (design §2 Part 2).

    One line per sub-scope — its live status, id, dependencies, and originating scope — led by the
    campaign id. A terminal off-ramp (moot / ejected) also shows its escalation note, so a re-split
    eject or a cancellation is human-visible, not a silent drop (refinement B). An empty / atomic
    campaign renders the header plus a literal no-sub-scopes line.
    """
    header = f"{STATUS_HEADER} {state.campaign_id}"
    if not state.sub_scopes:
        return f"{header}\n  (no sub-scopes — empty or atomic campaign)"
    lines = [header]
    for sub in state.sub_scopes:
        deps = f"depends_on: {', '.join(sub.depends_on)}; " if sub.depends_on else ""
        lines.append(
            f"  [{sub.status.value}] {sub.sub_scope_id}  "
            f"({deps}origin_scope: {sub.origin_scope}){_note_suffix(sub, joiner=' — ')}",
        )
    return "\n".join(lines)


def format_campaign_roadmap_block(state: CampaignState) -> str:
    """Render the current view as the B2-SUBSCOPES managed-block body (design §2 Part 2).

    One ``- [x]/[ ] **<id>** — <status> (origin_scope: …)`` slot per sub-scope in topo order,
    carrying live status (not a bare proposed slot) and per-slot provenance (locked #8). ``[x]``
    marks a terminal-resolved sub-scope (merged / moot); an ejected one stays ``[ ]`` and shows its
    escalation note. An empty / atomic campaign renders the empty string (nothing to splice).
    """
    if not state.sub_scopes:
        return ""
    return "\n".join(
        f"- [{'x' if sub.status in _DONE_STATUSES else ' '}] **{sub.sub_scope_id}** — "
        f"{sub.status.value}{_note_suffix(sub, joiner=': ')} (origin_scope: {sub.origin_scope})"
        for sub in state.sub_scopes
    )

"""Campaign orchestrator — the ``genome campaign`` surface (``finding-041``; B2 Phase 2).

A persistent, ordered, dependency-aware set of sub-scopes (from a non-atomic
:class:`~genome.scope_split.model.SplitResult`) driven through ``/scope-run``. The campaign
SEQUENCES, TRACKS, and TEES-UP — it **cannot** cross either human gate (Gate 1 plan approval,
Gate 2 ``/verify-and-merge``); it is advisory at the human boundary always.

State is an **append-only ledger** under locked decision #7: every status transition is an
insert-then-flip supersession (a new :class:`~genome.campaign.model.SubScopeState`, never an
in-place edit), and the current view is the derived latest-active record per sub-scope — which is
what makes the campaign auditable and multi-session resumable.

This package imports **no** :mod:`genome.db` and **no** :mod:`genome.config`;
``python -c "import genome.campaign"`` must run on a fresh checkout with no DuckDB / SQLCipher
built. The guarantee is carried by the package-local ``test_campaign_no_db_import.py``
clean-subprocess test — do not add a database or settings import here or in any module it pulls in.
"""

from __future__ import annotations

from genome.campaign.cli import campaign_app
from genome.campaign.formatter import (
    STATUS_HEADER,
    format_campaign_roadmap_block,
    format_campaign_status,
)
from genome.campaign.model import (
    GATE_CROSSINGS,
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    CampaignState,
    CampaignStatus,
    RevalidationDecision,
    SubScopeState,
)
from genome.campaign.persistence import (
    DEFAULT_CAMPAIGN_DIR,
    append_records,
    load_campaign,
    load_history,
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

__all__ = [
    "DEFAULT_CAMPAIGN_DIR",
    "GATE_CROSSINGS",
    "LEGAL_TRANSITIONS",
    "STATUS_HEADER",
    "TERMINAL_STATUSES",
    "CampaignState",
    "CampaignStatus",
    "RevalidationDecision",
    "SubScopeState",
    "advance_on_merge",
    "append_records",
    "apply_revalidation",
    "campaign_app",
    "cancel_campaign",
    "format_campaign_roadmap_block",
    "format_campaign_status",
    "load_campaign",
    "load_history",
    "next_ready",
    "reduce_current",
    "seed_campaign",
    "tee_up",
    "transition",
]

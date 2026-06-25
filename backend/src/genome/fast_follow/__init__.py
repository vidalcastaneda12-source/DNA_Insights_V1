"""Fast-follow drain loop — the ``genome fast-follow`` surface (``finding-038``).

A bounded, fail-closed triage loop that gives repo-sweep's backlog a consumer: it DRAINs
Tier-0 / bounded-Tier-1 candidates through Sub-A's ``/verify-and-merge`` gate and EJECTs
schema / pipeline / annotation / anchor-exposed candidates back to ``/scope-run``. The core
is a pure ``Candidate → Classification`` reducer (:func:`classify`) plus a batcher
(:func:`plan_next_batch`) with cross-invocation seen-set dedup; the merge / ``gh`` / ``rm``
live in the skill, never here.

SAFETY INVARIANT (plan §2): no candidate carrying a guarded class, a non-empty anchor set,
an over-cap blast_radius, or a touched path under ``docs/schemas/**`` / ``ddl/**`` is EVER
classified DRAIN. The classifier fails closed — anything undecidable EJECTs.

**This package imports no** :mod:`genome.db`. ``python -c "import genome.fast_follow"`` must
run on a fresh checkout with no DuckDB / SQLCipher built (plan §3 / A4). Do not add a
database import here or in any module it pulls in — the DB-free guarantee is carried by the
package-local ``test_fast_follow_no_db_import.py`` clean-subprocess test.
"""

from __future__ import annotations

from genome.fast_follow.classifier import classify
from genome.fast_follow.cli import fast_follow_app
from genome.fast_follow.formatter import (
    NOTHING_DRAINABLE_SENTINEL,
    format_eject_draft,
    format_triage_plan,
)
from genome.fast_follow.loop import group_drains, loop_done, plan_next_batch
from genome.fast_follow.model import (
    GUARD_CLASS_VOCAB,
    GUARDED_CLASSES,
    MAX_BATCHES,
    MAX_DRAIN_FILES,
    MAX_ITEMS,
    TIER_VOCAB,
    Candidate,
    Classification,
    Triage,
    TriagePlan,
)
from genome.fast_follow.persistence import load_seen, save_seen

__all__ = [
    "GUARDED_CLASSES",
    "GUARD_CLASS_VOCAB",
    "MAX_BATCHES",
    "MAX_DRAIN_FILES",
    "MAX_ITEMS",
    "NOTHING_DRAINABLE_SENTINEL",
    "TIER_VOCAB",
    "Candidate",
    "Classification",
    "Triage",
    "TriagePlan",
    "classify",
    "fast_follow_app",
    "format_eject_draft",
    "format_triage_plan",
    "group_drains",
    "load_seen",
    "loop_done",
    "plan_next_batch",
    "save_seen",
]

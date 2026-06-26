"""Smart-cut scope-split detector — the ``genome scope-split`` surface (``finding-039``).

A fail-closed detector that reads a Stage-0 dispatcher scope manifest and proposes whether the
scope is **separable** into independently-shippable sub-scopes, or is one indivisible unit
(atomic). The cut policy is **manifest-primary** (group the footprint by ``change_class``
boundary refined by ``out_of_scope_candidates``) with the git-grep import graph as a **veto**
signal only (DECISION 1). The core is a pure reducer
(:func:`~genome.scope_split.splitter.propose_split`) over frozen records; the ``git grep`` /
ROADMAP writes live behind a Protocol seam / in the CLI, never in the reducer.

SAFETY INVARIANT (plan §2 / failure-ordering (a)): a non-atomic proposal is returned **only**
when a candidate cut survives every gate — the primary partition yields ≥2 clusters, the coupling
veto does not fuse them below the minimum, the topo order is acyclic, the quality gate passes, and
the re-split cap is not hit. Any degenerate / undecidable input fails closed to atomic. A false
split is the costliest mode, so the splitter under-proposes by construction.

**This package imports no** :mod:`genome.db`. ``python -c "import genome.scope_split"`` must run
on a fresh checkout with no DuckDB / SQLCipher built (plan §3). Do not add a database import here
or in any module it pulls in — the DB-free guarantee is carried by the package-local
``test_scope_split_no_db_import.py`` clean-subprocess test.
"""

from __future__ import annotations

from genome.scope_split.cli import scope_split_app
from genome.scope_split.formatter import (
    ATOMIC_SENTINEL,
    MICRO_GATE_HEADER,
    format_roadmap_block,
    format_split_proposal,
)
from genome.scope_split.graph import (
    CouplingEdge,
    CouplingGraph,
    CouplingGraphBuilder,
    GitGrepCouplingBuilder,
    StaticCouplingBuilder,
    make_coupling_builder,
)
from genome.scope_split.model import (
    CHANGE_CLASS_VOCAB,
    MAX_CUT_COST,
    MAX_RESPLIT_DEPTH,
    MIN_CLUSTERS,
    MIN_SUBSCOPE_SHRINK,
    SCHEMA_FIRST_ORDER,
    SHARED_HELPER_FANIN,
    CutQuality,
    RiskTier,
    ScopeManifestInput,
    SplitResult,
    SubScope,
    est_risk_tier,
    scope_S,
    tier_from_S,
)
from genome.scope_split.roadmap_writer import (
    BLOCK_BEGIN,
    BLOCK_END,
    DEFAULT_ROADMAP_PATH,
    append_roadmap_block,
)
from genome.scope_split.splitter import propose_split

__all__ = [
    "ATOMIC_SENTINEL",
    "BLOCK_BEGIN",
    "BLOCK_END",
    "CHANGE_CLASS_VOCAB",
    "DEFAULT_ROADMAP_PATH",
    "MAX_CUT_COST",
    "MAX_RESPLIT_DEPTH",
    "MICRO_GATE_HEADER",
    "MIN_CLUSTERS",
    "MIN_SUBSCOPE_SHRINK",
    "SCHEMA_FIRST_ORDER",
    "SHARED_HELPER_FANIN",
    "CouplingEdge",
    "CouplingGraph",
    "CouplingGraphBuilder",
    "CutQuality",
    "GitGrepCouplingBuilder",
    "RiskTier",
    "ScopeManifestInput",
    "SplitResult",
    "StaticCouplingBuilder",
    "SubScope",
    "append_roadmap_block",
    "est_risk_tier",
    "format_roadmap_block",
    "format_split_proposal",
    "make_coupling_builder",
    "propose_split",
    "scope_S",
    "scope_split_app",
    "tier_from_S",
]

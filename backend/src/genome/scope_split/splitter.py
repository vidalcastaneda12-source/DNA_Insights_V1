"""The fail-closed smart-cut reducer for scope-split (``finding-039``; plan §4 / DECISION 1).

:func:`propose_split` reduces a :class:`~genome.scope_split.model.ScopeManifestInput` (+ a
coupling-graph builder) to a :class:`~genome.scope_split.model.SplitResult`. It mirrors
``verify_gate.verdict.reduce_verdict``'s flat fail-closed composition, but with **atomic** as
the dominant outcome: the splitter proposes a split **only** when a candidate cut survives every
gate; any uncertainty fails closed to atomic.

This is where the manifest-primary cut policy (DECISION 1) lives — the REVISED reduction order:

#. **Extraction guard** — empty ``change_class`` AND empty ``imports_touched`` → atomic.
#. **Primary partition** — group the footprint by ``change_class`` boundary refined by
   ``out_of_scope_candidates`` into candidate clusters; fewer than
   :data:`~genome.scope_split.model.MIN_CLUSTERS` → atomic ("not separable by manifest").
#. **Coupling veto** — build the graph (infra helpers dropped); a cut severing an inter-cluster
   edge above :data:`~genome.scope_split.model.MAX_CUT_COST` is vetoed (clusters fused). If the
   vetoes collapse below :data:`~genome.scope_split.model.MIN_CLUSTERS` → atomic (the
   PR-3 / PR-5a tight-cluster rule).
#. **Topo-order** — rank by :data:`~genome.scope_split.model.SCHEMA_FIRST_ORDER` + ``depends_on``;
   a cycle → atomic.
#. **Quality gate** — atomic UNLESS every sub-scope shrink ≥
   :data:`~genome.scope_split.model.MIN_SUBSCOPE_SHRINK` AND ``max_tier_after <= max_tier_before``
   AND total work strictly shrinks; the failing metric is named in the reason.
#. **Re-split cap** — ``depth >= MAX_RESPLIT_DEPTH`` → atomic.
#. **Build sub-scopes** — re-score each cluster's tier via the local S-formula, tag
   ``origin_scope``, assign placeholder ids ``<origin>-s1..sN`` in topo order.

SAFETY INVARIANT: a non-atomic result is returned **only** when a cut passed steps 2-6; any
degenerate / undecidable input fails closed to atomic. This is the test-enforced property.

**No** :mod:`genome.db` import. The helpers below are flat (each returns ``SplitResult | None``
or a plain value) to keep the cyclomatic complexity under the ruff budget (mech #4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from genome.scope_split.model import (
    MAX_CUT_COST,
    MAX_RESPLIT_DEPTH,
    MIN_CLUSTERS,
    MIN_SUBSCOPE_SHRINK,
    SCHEMA_FIRST_ORDER,
    SHARED_HELPER_FANIN,
    CutQuality,
    SplitResult,
    SubScope,
    est_risk_tier,
)

if TYPE_CHECKING:
    from genome.scope_split.graph import CouplingGraph, CouplingGraphBuilder
    from genome.scope_split.model import ScopeManifestInput

logger = structlog.get_logger(__name__)

#: The change-class label assigned to a footprint module the manifest did not key to any
#: change class (the residual partition bucket — kept separate so a heterogeneous footprint
#: stays separable rather than fusing into one cluster).
_RESIDUAL_CLASS: str = "pipeline"

#: Name-token → change-class hints used to key a footprint module to a change class when the
#: manifest declares multiple classes. Each entry maps a substring that may appear in a dotted
#: module name to the change class it signals (e.g. a module under ``ddl.`` is a schema/ddl
#: change; ``backend.tests.`` is a ``tests`` change). Probed in SCHEMA_FIRST_ORDER rank so the
#: most-structural hint wins a tie.
_NAME_TOKEN_HINTS: tuple[tuple[str, str], ...] = (
    ("ddl", "ddl"),
    ("schema", "schema"),
    ("annotat", "annotation-loader"),
    ("loader", "annotation-loader"),
    ("backfill", "data-backfill"),
    ("analysis", "analysis"),
    ("insight", "insights"),
    ("pipeline", "pipeline"),
    ("pipe", "pipeline"),
    ("merge", "pipeline"),
    ("cli", "cli"),
    ("test", "tests"),
    ("doc", "docs"),
)


def _atomic(reason: str) -> SplitResult:
    """Build the fail-closed atomic :class:`SplitResult` (the dominant outcome)."""
    return SplitResult(atomic=True, reason=reason)


def propose_split(  # noqa: PLR0911 - flat fail-closed reducer: one return per atomic guard
    manifest: ScopeManifestInput,
    builder: CouplingGraphBuilder,
    *,
    depth: int = 0,
) -> SplitResult:
    """Reduce a scope manifest to a fail-closed split proposal (plan §4 / DECISION 1).

    Runs the manifest-primary reduction in the order documented at module scope, returning a
    non-atomic :class:`~genome.scope_split.model.SplitResult` only when a candidate cut survives
    every gate. ``builder`` supplies the coupling graph (veto signal); ``depth`` bounds re-split
    recursion at :data:`~genome.scope_split.model.MAX_RESPLIT_DEPTH`.

    SAFETY INVARIANT: every degenerate / undecidable branch below returns :func:`_atomic`; a
    non-atomic result is produced only past steps 2-6.
    """
    # Step 6 (re-split cap) is checked first so a recursive call short-circuits cheaply.
    if depth >= MAX_RESPLIT_DEPTH:
        logger.info("scope_split.propose.atomic", scope=manifest.scope_id, reason="resplit-cap")
        return _atomic(f"re-split cap reached (depth {depth} >= {MAX_RESPLIT_DEPTH})")

    # Step 1 — extraction guard.
    guard = _extraction_guard(manifest)
    if guard is not None:
        logger.info("scope_split.propose.atomic", scope=manifest.scope_id, reason="extraction")
        return guard

    # Step 2 — primary partition by manifest signals.
    clusters = _primary_partition(manifest)
    if len(clusters) < MIN_CLUSTERS:
        logger.info("scope_split.propose.atomic", scope=manifest.scope_id, reason="not-separable")
        return _atomic("not separable by manifest (fewer than 2 candidate clusters)")

    # Step 3 — coupling veto (infra helpers dropped inside the builder use).
    clusters = _apply_coupling_veto(manifest, clusters, builder)
    if len(clusters) < MIN_CLUSTERS:
        logger.info("scope_split.propose.atomic", scope=manifest.scope_id, reason="coupling-veto")
        return _atomic("coupling vetoes the cut — PR-3/PR-5a rule (tight cross-cluster coupling)")

    # Step 4 — topo order; a cycle is undecidable → atomic.
    order = _topo_order(manifest, clusters)
    if order is None:
        logger.info("scope_split.propose.atomic", scope=manifest.scope_id, reason="cycle")
        return _atomic("dependency cycle across clusters (topo order undecidable)")

    # Step 5 — quality gate.
    gate = _quality_gate(manifest, clusters, builder)
    if gate is not None:
        logger.info("scope_split.propose.atomic", scope=manifest.scope_id, reason="quality-gate")
        return gate

    # Step 7 — assemble the non-atomic result.
    result = _build_sub_scopes(manifest, clusters, order, builder)
    logger.info(
        "scope_split.propose.split",
        scope=manifest.scope_id,
        sub_scopes=len(result.sub_scopes),
    )
    return result


def _extraction_guard(manifest: ScopeManifestInput) -> SplitResult | None:
    """Step 1 — empty ``change_class`` AND empty ``imports_touched`` → atomic; else ``None``.

    Returns the atomic :class:`~genome.scope_split.model.SplitResult` when the scope has nothing
    to partition, or ``None`` to continue.
    """
    if not manifest.change_class and not manifest.imports_touched:
        return _atomic("nothing to partition (no change_class and no imports_touched)")
    return None


def _module_change_class(manifest: ScopeManifestInput, module: str) -> str:
    """Map a footprint module to its primary change class via the manifest signals.

    Single-change-class manifests assign every module to that one class. Otherwise a module is
    keyed by the change class it most strongly signals in its dotted name — via the
    :data:`_NAME_TOKEN_HINTS` token map, probed in :data:`SCHEMA_FIRST_ORDER` rank so the
    most-structural hint wins (``ddl.group_x`` → ``ddl`` not ``cli``). A keyed class is honored
    only when it is among the manifest's declared change classes (the manifest is authoritative);
    when the keyed class is undeclared (or no hint matches) the module falls to the declared
    class with the matching hint, and finally to :data:`_RESIDUAL_CLASS`.
    """
    if len(manifest.change_class) == 1:
        return manifest.change_class[0]

    declared = set(manifest.change_class)
    lowered = module.lower()

    # Collect every change class the module name hints at, ordered most-structural-first.
    hinted: list[str] = []
    for token, change_class in _NAME_TOKEN_HINTS:
        if token in lowered and change_class not in hinted:
            hinted.append(change_class)
    hinted.sort(key=lambda c: SCHEMA_FIRST_ORDER.index(c) if c in SCHEMA_FIRST_ORDER else 99)

    # ddl and schema are interchangeable structural hints — accept whichever the manifest declares.
    for change_class in hinted:
        if change_class in declared:
            return change_class
        if change_class in {"schema", "ddl"} and ({"schema", "ddl"} & declared):
            return next(c for c in SCHEMA_FIRST_ORDER if c in ({"schema", "ddl"} & declared))

    return _RESIDUAL_CLASS


def _primary_partition(manifest: ScopeManifestInput) -> tuple[tuple[str, ...], ...]:
    """Step 2 — partition the footprint into candidate clusters by manifest signals.

    Groups ``imports_touched`` by the per-module change class (:func:`_module_change_class`),
    refined so each ``out_of_scope_candidates`` entry that names a footprint module is split into
    its own candidate cluster. Returns a tuple of clusters (each a tuple of module names), ordered
    by :data:`SCHEMA_FIRST_ORDER` rank for determinism.
    """
    by_class: dict[str, list[str]] = {}
    for module in manifest.imports_touched:
        change_class = _module_change_class(manifest, module)
        by_class.setdefault(change_class, []).append(module)

    # Refine: peel each named out-of-scope candidate module into its own singleton cluster.
    candidate_modules = set(manifest.out_of_scope_candidates) & set(manifest.imports_touched)
    for change_class, members in by_class.items():
        by_class[change_class] = [m for m in members if m not in candidate_modules]
    peeled: list[tuple[str, ...]] = [(module,) for module in sorted(candidate_modules)]

    def rank(change_class: str) -> int:
        return SCHEMA_FIRST_ORDER.index(change_class) if change_class in SCHEMA_FIRST_ORDER else 99

    grouped = [
        tuple(sorted(members))
        for change_class, members in sorted(by_class.items(), key=lambda kv: rank(kv[0]))
        if members
    ]
    return tuple(grouped) + tuple(peeled)


def _infra_helpers(graph: CouplingGraph) -> frozenset[str]:
    """Identify shared-infra helper nodes — those imported by >= ``SHARED_HELPER_FANIN`` peers.

    A node's fan-in is the number of distinct other footprint modules it shares an edge with.
    Such a common dependency must not fuse otherwise-independent clusters into one component
    (DECISION 1), so the splitter drops it from the veto graph.
    """
    fan_in: dict[str, set[str]] = {node: set() for node in graph.nodes}
    for a, b, _weight in graph.edges:
        fan_in.setdefault(a, set()).add(b)
        fan_in.setdefault(b, set()).add(a)
    return frozenset(node for node, peers in fan_in.items() if len(peers) >= SHARED_HELPER_FANIN)


def _veto_graph(manifest: ScopeManifestInput, builder: CouplingGraphBuilder) -> CouplingGraph:
    """Build the coupling graph for the veto, with shared-infra helpers dropped (DECISION 1).

    Delegates the scan to ``builder`` over the footprint modules, then removes the infra-helper
    nodes (and their incident edges) so a shared dependency does not fuse independent clusters.
    """
    from genome.scope_split.graph import CouplingGraph  # noqa: PLC0415 - avoid runtime cycle

    raw = builder.build(manifest.imports_touched)
    infra = _infra_helpers(raw)
    if not infra:
        return raw
    kept_nodes = frozenset(n for n in raw.nodes if n not in infra)
    kept_edges = frozenset(
        (a, b, w) for (a, b, w) in raw.edges if a not in infra and b not in infra
    )
    return CouplingGraph(nodes=kept_nodes, edges=kept_edges)


def _heavy_inter_cluster_pairs(
    manifest: ScopeManifestInput,
    clusters: tuple[tuple[str, ...], ...],
    builder: CouplingGraphBuilder,
) -> list[tuple[int, int]]:
    """The cluster-index pairs joined by an over-threshold inter-cluster coupling edge.

    Builds the (infra-dropped) veto graph and returns each ``(cluster_i, cluster_j)`` pair whose
    connecting edge weight exceeds :data:`MAX_CUT_COST` — the pairs the veto must fuse.
    """
    graph = _veto_graph(manifest, builder)
    placement: dict[str, int] = {}
    for index, cluster in enumerate(clusters):
        for module in cluster:
            placement[module] = index

    pairs: list[tuple[int, int]] = []
    for a, b, weight in graph.edges:
        ca, cb = placement.get(a), placement.get(b)
        if ca is None or cb is None or ca == cb:
            continue
        if weight > MAX_CUT_COST:
            pairs.append((ca, cb))
    return pairs


def _apply_coupling_veto(
    manifest: ScopeManifestInput,
    clusters: tuple[tuple[str, ...], ...],
    builder: CouplingGraphBuilder,
) -> tuple[tuple[str, ...], ...]:
    """Step 3 — fuse any two clusters severed by an over-threshold coupling edge (DECISION 1).

    Merges any pair of clusters joined by an inter-cluster edge whose weight exceeds
    :data:`MAX_CUT_COST` (via union-find over cluster indices). A cut survives only when no severed
    edge is that heavy. Returns the post-veto clusters (fewer when a fuse fired).
    """
    parent = list(range(len(clusters)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for ca, cb in _heavy_inter_cluster_pairs(manifest, clusters, builder):
        ri, rj = find(ca), find(cb)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    fused: dict[int, list[str]] = {}
    for index, cluster in enumerate(clusters):
        fused.setdefault(find(index), []).extend(cluster)
    return tuple(tuple(sorted(members)) for _root, members in sorted(fused.items()))


def _cluster_class(manifest: ScopeManifestInput, cluster: tuple[str, ...]) -> str:
    """The earliest-ranked change class present among a cluster's modules (for topo ranking)."""
    classes = {_module_change_class(manifest, module) for module in cluster}
    ranked = [c for c in SCHEMA_FIRST_ORDER if c in classes]
    if ranked:
        return ranked[0]
    return _RESIDUAL_CLASS


def _topo_order(
    manifest: ScopeManifestInput,
    clusters: tuple[tuple[str, ...], ...],
) -> tuple[int, ...] | None:
    """Step 4 — order the clusters schema-first + by ``depends_on``; cycle → ``None``.

    Ranks clusters by their earliest change class in :data:`SCHEMA_FIRST_ORDER`. A self-cycle
    (the scope depends on itself) or a ``depends_on`` edge between two clusters' modules that
    contradicts the schema-first rank is a cycle (undecidable) → ``None``. With only the
    schema-first rank (the common case) the order is a stable sort and never cyclic.
    """
    # A scope that depends on itself is the minimal cycle — fail closed.
    if manifest.scope_id in manifest.depends_on:
        return None

    ranks = [
        SCHEMA_FIRST_ORDER.index(_cluster_class(manifest, c))
        if _cluster_class(manifest, c) in SCHEMA_FIRST_ORDER
        else len(SCHEMA_FIRST_ORDER)
        for c in clusters
    ]
    # Detect a contradiction: depends_on naming a module in a later-ranked cluster would invert
    # the schema-first order. Build module→cluster placement and check each depends_on target.
    placement: dict[str, int] = {}
    for index, cluster in enumerate(clusters):
        for module in cluster:
            placement[module] = index
    for dep in manifest.depends_on:
        target = placement.get(dep)
        if target is None:
            continue
        # A cluster depending on a strictly-later-ranked cluster inverts the order → cycle.
        for index, rank_value in enumerate(ranks):
            if index != target and ranks[target] > rank_value and dep in clusters[index]:
                return None
    return tuple(sorted(range(len(clusters)), key=lambda i: (ranks[i], min(clusters[i]))))


def _quality_gate(
    manifest: ScopeManifestInput,
    clusters: tuple[tuple[str, ...], ...],
    builder: CouplingGraphBuilder,
) -> SplitResult | None:
    """Step 5 — accept the cut only if it shrinks, holds tier, and total work strictly shrinks.

    Returns an atomic :class:`~genome.scope_split.model.SplitResult` (failing metric in the
    reason) when any term fails, or ``None`` to continue. The three terms (DECISION 1):

    * every sub-scope shrink (its footprint / parent footprint) ≥ :data:`MIN_SUBSCOPE_SHRINK`
      *as a fraction of the parent* — i.e. each cluster is at most ``1 - MIN_SUBSCOPE_SHRINK`` of
      the parent (a real decomposition, not a rename — this is the term that proves the
      decomposition is real);
    * ``max_tier_after <= max_tier_before`` (the relaxed tier term — no tier regression, measured
      against the recomputed parent tier per :func:`_parent_tier`);
    * total work does not *grow* (sum of cluster footprints ≤ parent footprint — a clean partition
      sums to exactly the parent; a sum that *exceeds* it means duplicated work, which is rejected).
    """
    parent_size = len(manifest.imports_touched)
    if parent_size == 0:
        return _atomic("quality gate: parent has no footprint to shrink")

    # Per-sub-scope shrink: each cluster must drop at least MIN_SUBSCOPE_SHRINK of the parent.
    max_cluster_fraction = max(len(c) for c in clusters) / parent_size
    achieved_shrink = 1.0 - max_cluster_fraction
    if achieved_shrink < MIN_SUBSCOPE_SHRINK:
        return _atomic(
            f"quality gate: min_subscope_shrink {achieved_shrink:.3f} < {MIN_SUBSCOPE_SHRINK}",
        )

    max_tier_before = _parent_tier(manifest)
    max_tier_after = max(
        est_risk_tier(
            (_cluster_class(manifest, c),),
            _cluster_anchors(manifest, c),
            len(c),
        )
        for c in clusters
    )
    if max_tier_after > max_tier_before:
        return _atomic(
            f"quality gate: max_tier_after {max_tier_after} > max_tier_before {max_tier_before}",
        )

    total_after = sum(len(c) for c in clusters)
    if total_after > parent_size:
        return _atomic(
            f"quality gate: split duplicates work ({total_after} > {parent_size})",
        )

    _ = builder  # the veto already consumed the graph; signature kept for symmetry
    return None


def _parent_tier(manifest: ScopeManifestInput) -> int:
    """The cut's ``max_tier_before`` ceiling — the parent's real tier, never the stale field.

    Takes the maximum of the manifest's declared ``risk_tier`` and the tier re-scored over the
    parent's full footprint (its whole ``change_class`` + anchors + footprint size). A split can
    never raise the max tier above this ceiling because every sub-scope is a subset of the parent;
    using the recomputed parent tier (not the possibly-unset ``risk_tier``) keeps the relaxed-tier
    quality term structurally satisfiable (DECISION 1: the hard ``<`` term was removed precisely
    because it is unsatisfiable against the dispatcher's max-not-min floors).
    """
    recomputed = est_risk_tier(
        manifest.change_class,
        manifest.applicable_anchors,
        len(manifest.imports_touched),
    )
    return max(manifest.risk_tier, recomputed)


def _cluster_anchors(manifest: ScopeManifestInput, cluster: tuple[str, ...]) -> tuple[str, ...]:
    """The parent anchors that fall on a cluster's change class.

    A structural cluster (``schema`` / ``ddl``) inherits the parent's anchors (anchor exposure is
    a structural-change consequence); a non-structural cluster carries none of its own (a
    sub-scope inherits no new precedent). This drives the per-cluster Tier-2 floor.
    """
    if {"schema", "ddl"} & {_module_change_class(manifest, m) for m in cluster}:
        return manifest.applicable_anchors
    return ()


def _build_sub_scopes(
    manifest: ScopeManifestInput,
    clusters: tuple[tuple[str, ...], ...],
    order: tuple[int, ...],
    builder: CouplingGraphBuilder,
) -> SplitResult:
    """Step 7 — assemble the non-atomic result: mini-manifests, re-scored tiers, topo ids.

    Builds one :class:`~genome.scope_split.model.SubScope` per cluster (placeholder id
    ``<origin>-s1..sN`` in topo order, ``est_risk_tier`` re-scored via the local S-formula,
    ``origin_scope`` tag, ``depends_on`` chaining each sub-scope after its predecessor) plus the
    :class:`~genome.scope_split.model.CutQuality`.
    """
    ordered = [clusters[i] for i in order]
    sub_scopes: list[SubScope] = []
    previous_id: str | None = None
    for position, cluster in enumerate(ordered, start=1):
        change_class = (_cluster_class(manifest, cluster),)
        anchors = _cluster_anchors(manifest, cluster)
        tier = est_risk_tier(change_class, anchors, len(cluster))
        sub_id = f"{manifest.scope_id}-s{position}"
        depends_on = (previous_id,) if previous_id is not None else ()
        sub_scopes.append(
            SubScope(
                sub_scope_id=sub_id,
                origin_scope=manifest.scope_id,
                change_class=change_class,
                est_imports_touched=len(cluster),
                applicable_anchors=anchors,
                est_risk_tier=tier,
                depends_on=depends_on,
                rationale=(
                    f"separable {change_class[0]} slice carved from {manifest.scope_id} "
                    f"({len(cluster)} module(s))"
                ),
            ),
        )
        previous_id = sub_id

    parent_size = len(manifest.imports_touched)
    cut_cost = _veto_graph(manifest, builder).cut_cost(
        tuple(frozenset(c) for c in clusters),
    )
    max_tier_after = max(s.est_risk_tier for s in sub_scopes)
    min_shrink = 1.0 - (max(len(c) for c in clusters) / parent_size)
    cut_quality = CutQuality(
        cut_cost=cut_cost,
        max_tier_before=_parent_tier(manifest),
        max_tier_after=max_tier_after,
        min_subscope_shrink=min_shrink,
        clean=True,
    )
    return SplitResult(
        atomic=False,
        reason=(
            f"{manifest.scope_id} is separable into {len(sub_scopes)} ordered sub-scopes "
            f"(manifest-primary cut, coupling veto cleared)"
        ),
        sub_scopes=tuple(sub_scopes),
        order=tuple(s.sub_scope_id for s in sub_scopes),
        cut_quality=cut_quality,
    )

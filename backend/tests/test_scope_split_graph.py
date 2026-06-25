"""Coupling-graph units — pure WCC / cut_cost math + builder factory.

Plan-blind spec source: FROZEN-INTERFACE graph.py section (CouplingGraph.
weakly_connected_components + cut_cost "IMPLEMENTED; 0 total edges→0.0"; StaticCouplingBuilder
"restricts edges to requested modules"; make_coupling_builder "auto→git-grep …; unknown→
ValueError"; GitGrepCouplingBuilder.build "STUBBED → NotImplementedError"); SYNTHESIZED-PLAN §5
("graph (pure math incl zero-total-edge→0.0; … make_coupling_builder('auto') returns git-grep
+ logs)").

The pure-math + factory tests are GREEN from freeze. The single GitGrepCouplingBuilder.build
test is RED-until-filled: it asserts the BEHAVIOR (build returns a CouplingGraph whose nodes are
the requested modules) and so goes RED on NotImplementedError now and GREEN when the body lands
— it does NOT assert pytest.raises(NotImplementedError).

test->spec provenance noted per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import pytest

from genome.scope_split.graph import (
    CouplingGraph,
    CouplingGraphBuilder,
    GitGrepCouplingBuilder,
    StaticCouplingBuilder,
    make_coupling_builder,
)

# ── CouplingGraph.weakly_connected_components (pure, GREEN) ────────────────────


def test_weakly_connected_components_splits_disconnected_clusters() -> None:
    """from: FROZEN-INTERFACE CouplingGraph.weakly_connected_components (IMPLEMENTED).

    Two edge-disjoint pairs form two weakly-connected components.
    """
    g = CouplingGraph(
        nodes=frozenset({"a", "b", "c", "d"}),
        edges=frozenset({("a", "b", 0.5), ("c", "d", 0.1)}),
    )
    components = {frozenset(c) for c in g.weakly_connected_components()}
    assert components == {frozenset({"a", "b"}), frozenset({"c", "d"})}


def test_weakly_connected_components_isolated_node_is_its_own_component() -> None:
    """from: FROZEN-INTERFACE CouplingGraph.weakly_connected_components (IMPLEMENTED).

    A node with no incident edge is a singleton component (the git-grep "ran, no matches →
    isolated node" case, mech #1).
    """
    g = CouplingGraph(nodes=frozenset({"a", "b", "c"}), edges=frozenset({("a", "b", 0.9)}))
    components = {frozenset(c) for c in g.weakly_connected_components()}
    assert frozenset({"c"}) in components
    assert frozenset({"a", "b"}) in components


# ── CouplingGraph.cut_cost (pure, GREEN) ──────────────────────────────────────


def test_cut_cost_is_inter_over_total_edge_weight() -> None:
    """from: FROZEN-INTERFACE CouplingGraph.cut_cost (IMPLEMENTED) + SYNTHESIZED-PLAN §5
    ("cut_cost(partition)->float (inter/total)").

    Severing the only edge of a 2-node graph costs the full edge weight ratio → 1.0.
    """
    g = CouplingGraph(nodes=frozenset({"a", "b"}), edges=frozenset({("a", "b", 0.8)}))
    cost = g.cut_cost((frozenset({"a"}), frozenset({"b"})))
    assert cost == pytest.approx(1.0)


def test_cut_cost_zero_when_partition_severs_no_edge() -> None:
    """from: FROZEN-INTERFACE cut_cost + SYNTHESIZED-PLAN §5 (inter/total).

    A partition that keeps both intra-cluster edges intact severs nothing → cost 0.0.
    """
    g = CouplingGraph(
        nodes=frozenset({"a", "b", "c", "d"}),
        edges=frozenset({("a", "b", 0.5), ("c", "d", 0.1)}),
    )
    cost = g.cut_cost((frozenset({"a", "b"}), frozenset({"c", "d"})))
    assert cost == pytest.approx(0.0)


def test_cut_cost_zero_total_edges_is_zero() -> None:
    """from: FROZEN-INTERFACE cut_cost ("0 total edges→0.0") + SYNTHESIZED-PLAN §5
    ("zero-total-edge→0.0").

    A graph with no edges has zero total weight; cut_cost must return 0.0 (no divide-by-zero),
    the undecidable-low signal the quality gate treats as a fail-closed reason.
    """
    g = CouplingGraph(nodes=frozenset({"a", "b"}), edges=frozenset())
    cost = g.cut_cost((frozenset({"a"}), frozenset({"b"})))
    assert cost == pytest.approx(0.0)


# ── StaticCouplingBuilder (GREEN test seam) ───────────────────────────────────


def test_static_builder_restricts_edges_to_requested_modules() -> None:
    """from: FROZEN-INTERFACE StaticCouplingBuilder ("restricts edges to requested modules").

    Building over a subset of modules drops edges that touch a module outside the request — the
    graph returned for ('a','b') keeps a↔b but not b↔c.
    """
    sb = StaticCouplingBuilder(
        nodes=frozenset({"a", "b", "c"}),
        edges=frozenset({("a", "b", 0.9), ("b", "c", 0.2)}),
    )
    g = sb.build(("a", "b"))
    assert g.nodes == frozenset({"a", "b"})
    assert g.edges == frozenset({("a", "b", 0.9)})


def test_static_builder_satisfies_protocol() -> None:
    """from: FROZEN-INTERFACE ("@runtime_checkable Protocol CouplingGraphBuilder") +
    SYNTHESIZED-PLAN §4 (Protocol KEEP, D-1 divergence).

    StaticCouplingBuilder is a structural CouplingGraphBuilder (the runtime-checkable Protocol).
    """
    sb = StaticCouplingBuilder()
    assert isinstance(sb, CouplingGraphBuilder)


# ── make_coupling_builder factory (GREEN) ─────────────────────────────────────


def test_make_coupling_builder_static_returns_static_seam() -> None:
    """from: FROZEN-INTERFACE make_coupling_builder ("'static' returns the static seam") +
    SYNTHESIZED-PLAN §4 ("'static' = no-scan from explicit edges (tests)").
    """
    assert isinstance(make_coupling_builder("static"), StaticCouplingBuilder)


def test_make_coupling_builder_auto_returns_a_builder() -> None:
    """from: FROZEN-INTERFACE make_coupling_builder ("auto→git-grep + INFO log") +
    SYNTHESIZED-PLAN §5 ("make_coupling_builder('auto') returns git-grep + logs").

    'auto' resolves to a concrete CouplingGraphBuilder (the git-grep engine); we assert it is a
    builder, not the precise INFO log wording (logging is not the contract surface).
    """
    builder = make_coupling_builder("auto")
    assert isinstance(builder, CouplingGraphBuilder)


def test_make_coupling_builder_unknown_engine_raises() -> None:
    """from: FROZEN-INTERFACE make_coupling_builder ("unknown→ValueError").

    An unrecognized engine name fails closed with ValueError, never a silent default.
    """
    with pytest.raises(ValueError):  # noqa: PT011 — contract is "a ValueError on unknown engine"
        make_coupling_builder("bogus")  # type: ignore[arg-type]


# ── GitGrepCouplingBuilder.build — RED until the body lands ────────────────────


def test_git_grep_builder_build_returns_graph_over_requested_modules() -> None:
    """from: FROZEN-INTERFACE GitGrepCouplingBuilder ("build STUBBED → NotImplementedError;
    filled concurrently") + SYNTHESIZED-PLAN §4 (git-grep scan of import forms).

    RED-until-filled: assert the BEHAVIOR (build over a tuple of real modules returns a
    CouplingGraph whose node set is exactly the requested modules). This goes RED on
    NotImplementedError now and GREEN when GitGrepCouplingBuilder.build is implemented. It does
    NOT assert pytest.raises(NotImplementedError) — that would lock the stub in place.
    """
    builder = GitGrepCouplingBuilder(repo_root=".")
    modules = ("genome.scope_split.model", "genome.scope_split.graph")
    graph = builder.build(modules)
    assert isinstance(graph, CouplingGraph)
    assert graph.nodes == frozenset(modules)

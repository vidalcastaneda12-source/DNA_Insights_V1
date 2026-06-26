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

import re
import subprocess
from typing import TYPE_CHECKING

import pytest

from genome.scope_split import graph as graph_mod
from genome.scope_split.graph import (
    CouplingGraph,
    CouplingGraphBuilder,
    GitGrepCouplingBuilder,
    StaticCouplingBuilder,
    _grep_count_line,
    _import_pattern,
    _path_to_module,
    make_coupling_builder,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

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


# ── B1 regression: file-path manifest must still yield a real import edge ──────


def test_git_grep_builder_file_path_manifest_yields_weight_one_edge() -> None:
    """from: FIX-LIST B1 (the coupling veto was DEAD on real file-path manifests — the import
    regex was built from the FILE-PATH string, never matching a real ``import genome.x.y`` line).

    The Stage-0 dispatcher manifest carries footprint entries as repo-relative FILE PATHS, not
    dotted module names. ``splitter.py`` imports ``model``, so a build over the two file paths MUST
    surface that as a weight-1.0 undirected edge — proof the veto sees real coupling. Before the
    B1 fix this returned ``frozenset()`` (a dead veto). This guards the fix from regressing.
    """
    builder = GitGrepCouplingBuilder(repo_root=".")
    paths = (
        "backend/src/genome/scope_split/splitter.py",
        "backend/src/genome/scope_split/model.py",
    )
    graph = builder.build(paths)
    assert graph.edges, "file-path manifest produced no edge — the coupling veto is dead (B1)"
    a, b = sorted(paths)
    # The indented-import fix (review: the ^-anchor missed non-top-level imports) means every
    # import site of model in splitter is now counted, so the weight is >= 1.0 (was pinned at the
    # single top-level import before). The load-bearing assertion is that real coupling is SEEN.
    edge = next((e for e in graph.edges if e[0] == a and e[1] == b), None)
    assert edge is not None, "splitter↔model coupling not surfaced"
    assert edge[2] >= 1.0
    # And both real files resolve, so the scan is complete (no unresolved → veto can trust it).
    assert graph.unresolved == frozenset()


def test_path_to_module_inverts_module_to_path() -> None:
    """from: FIX-LIST B1 (path→module normalization for the import regex).

    A repo-relative ``backend/src`` path maps to its dotted module name; a name that is already
    dotted passes through unchanged (the manifest may carry either shape).
    """
    assert _path_to_module("backend/src/genome/scope_split/model.py") == "genome.scope_split.model"
    assert _path_to_module("genome.scope_split.model") == "genome.scope_split.model"


# ── W6: the three import forms the _import_pattern alternation must match ──────


def test_import_pattern_matches_all_three_import_forms() -> None:
    """from: FIX-LIST W6 (ptest-6: the 3 import forms in ``_import_pattern`` were untested).

    The pattern is built from a DOTTED module name (post-B1) and must match ``import x.y``,
    ``from x.y import Z``, and the relative ``from .y import Z`` — but NOT an unrelated line. This
    also guards B1 from regressing (a pattern built from a file path would match none of these).
    """
    # The pattern targets git grep's POSIX ERE (`[[:space:]]` for the indent prefix); Python's re
    # does not understand the POSIX class, so translate it to `\s` for this in-process proxy. The
    # real engine is exercised end-to-end by the git-grep builder tests above.
    pattern = re.compile(_import_pattern("genome.scope_split.model").replace("[[:space:]]", r"\s"))
    assert pattern.search("import genome.scope_split.model")
    assert pattern.search("from genome.scope_split.model import ScopeManifestInput")
    assert pattern.search("from .model import ScopeManifestInput")
    # Indented imports (inside a function / TYPE_CHECKING / try) must match — the ^-anchor bug that
    # made the scanner blind to non-top-level coupling (review: the false-split blocker).
    assert pattern.search("    import genome.scope_split.model")
    assert pattern.search("        from genome.scope_split.model import X")
    assert not pattern.search("import genome.scope_split.graph")
    # The absolute form requires a trailing boundary so a name-prefix sibling is not over-counted.
    assert not pattern.search("import genome.scope_split.model_other")
    assert not pattern.search("# a comment mentioning genome.scope_split.model in prose")


# ── type-4: CouplingGraph canonicalizes reversed-edge duplicates ──────────────


def test_coupling_graph_canonicalizes_reversed_edge_duplicate() -> None:
    """from: FIX-LIST type-4 (a reversed-edge duplicate could double-count cut_cost).

    ``(b, a, w)`` is the same undirected edge as ``(a, b, w)``. Construction must collapse the
    two to one canonical ``a < b`` triple with summed weight, so ``cut_cost`` cannot
    double-count the severed weight.
    """
    g = CouplingGraph(
        nodes=frozenset({"a", "b"}),
        edges=frozenset({("a", "b", 0.5), ("b", "a", 0.5)}),
    )
    assert g.edges == frozenset({("a", "b", 1.0)})
    # The single canonical edge severs to exactly 1.0 (not 2.0/2.0 double-count artifact).
    assert g.cut_cost((frozenset({"a"}), frozenset({"b"}))) == pytest.approx(1.0)


def test_coupling_graph_drops_self_loop() -> None:
    """from: FIX-LIST type-4 (canonical-ordering normalization).

    A self-loop (``a == b``) cannot be severed by any partition; construction drops it.
    """
    g = CouplingGraph(nodes=frozenset({"a", "b"}), edges=frozenset({("a", "a", 0.9)}))
    assert g.edges == frozenset()


# ── GitGrepCouplingBuilder returncode handling (mech #1) ──────────────────────


class _FakeCompleted:
    """A minimal stand-in for ``subprocess.CompletedProcess`` for the monkeypatched runs."""

    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_git_grep_returncode_two_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: FIX-LIST B5 (ptest-3: returncode>=2 → RuntimeError was untested) + mech #1.

    A ``git grep`` returncode of 2 (a real error, not "no matches") must raise a RuntimeError that
    references the returncode — never a silently-swallowed zero count.
    """

    def _fake_run(_argv: Sequence[str], **_kwargs: object) -> _FakeCompleted:
        return _FakeCompleted(returncode=2, stderr="fatal: bad revision")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    builder = GitGrepCouplingBuilder(repo_root=".")
    with pytest.raises(RuntimeError, match="2"):
        builder.build(("genome.a", "genome.b"))


def test_git_grep_returncode_one_is_zero_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """from: FIX-LIST ptest-4 (returncode=1 → 0-count) + mech #1 ("1 = ran, no matches").

    Returncode 1 means ``git grep`` ran and found no matches — an isolated node, not an error:
    the build returns a graph with the nodes but no edges.
    """

    def _fake_run(_argv: Sequence[str], **_kwargs: object) -> _FakeCompleted:
        return _FakeCompleted(returncode=1)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    builder = GitGrepCouplingBuilder(repo_root=".")
    graph = builder.build(("genome.a", "genome.b"))
    assert graph.nodes == frozenset({"genome.a", "genome.b"})
    assert graph.edges == frozenset()


def test_git_grep_returncode_zero_match_yields_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """from: FIX-LIST ptest-9 (returncode=0 match → edge weight) + mech #1.

    Returncode 0 with a ``path:count`` line is a match: the directed counts sum into one
    undirected edge whose weight is the total match count.
    """

    def _fake_run(_argv: Sequence[str], **_kwargs: object) -> _FakeCompleted:
        return _FakeCompleted(returncode=0, stdout="some/path.py:3\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    builder = GitGrepCouplingBuilder(repo_root=".")
    graph = builder.build(("genome.a", "genome.b"))
    # Both directions match (3 each) → one undirected edge with summed weight 6.0.
    assert graph.edges == frozenset({("genome.a", "genome.b", 6.0)})


def test_git_grep_nonexistent_path_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """from: FIX-LIST NEW-1 (the ``--`` was BEFORE the pattern → ``git grep`` parsed a
    NONEXISTENT path as a revision → returncode 128 → RuntimeError → crash; a manifest naming a
    not-yet-created module is a DOCUMENTED input, so the SAFETY INVARIANT requires fail-closed,
    never crash). FIX: ``--`` AFTER the pattern → a nonexistent path is a clean no-match
    (returncode 1 → 0 count → isolated node).

    The live ``git grep`` builder over a footprint that includes a REAL module and a NONEXISTENT
    module path must return a CouplingGraph (the nonexistent module an isolated node, no edge to
    it) and must NOT raise. Uses ``check=False`` real ``git grep`` (no monkeypatch of subprocess)
    to exercise the actual argv ordering that NEW-1 fixed.
    """
    monkeypatch.setattr(graph_mod.shutil, "which", lambda _name: "/usr/bin/git")
    builder = GitGrepCouplingBuilder(repo_root=".")
    real = "backend/src/genome/scope_split/model.py"
    ghost = "backend/src/genome/scope_split/does_not_exist_yet.py"
    graph = builder.build((real, ghost))
    assert isinstance(graph, CouplingGraph)
    assert graph.nodes == frozenset({real, ghost})
    # The nonexistent module is isolated: no edge mentions it.
    assert all(ghost not in (a, b) for (a, b, _w) in graph.edges)


def test_git_grep_missing_git_binary_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: FIX-LIST W3 (silent-2: missing ``git`` binary → uncaught FileNotFoundError).

    When ``git`` is not on PATH the build fails closed with a clean RuntimeError naming git, not a
    raw FileNotFoundError traceback.
    """
    monkeypatch.setattr(graph_mod.shutil, "which", lambda _name: None)
    builder = GitGrepCouplingBuilder(repo_root=".")
    with pytest.raises(RuntimeError, match="git"):
        builder.build(("genome.a", "genome.b"))


# ── _grep_count_line bare/unparsable branches (B2-Phase1 follow-up #6) ─────────


def test_grep_count_line_parses_bare_and_unparsable_lines() -> None:
    """from: B2-Phase1 deferred follow-up #6 (test-coverage nit) — ``_grep_count_line``'s
    bare-count and unparsable branches (existing tests reach it only via a ``path:count`` line).

    A bare integer (no ``path:`` prefix) parses to itself; a ``path:count`` line yields the count;
    an unparsable token (with or without a colon) fails closed to 0 (the warn-and-skip branch),
    never raising.
    """
    assert _grep_count_line("5") == 5  # bare count, no colon
    assert _grep_count_line("backend/src/genome/x.py:3") == 3  # path:count
    assert _grep_count_line("garbage") == 0  # unparsable, no colon → warn → 0
    assert _grep_count_line("backend/src/genome/x.py:notanumber") == 0  # unparsable with colon

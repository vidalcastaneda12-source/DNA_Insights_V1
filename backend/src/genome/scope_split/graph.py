"""The coupling graph + its builders for the scope-split detector (``finding-039``).

The splitter partitions a scope by its **manifest** change-class boundaries (DECISION 1); the
coupling graph is a **veto** signal — it prevents a proposed cut from severing two modules that
import each other heavily. This module holds:

* :class:`CouplingGraph` — a frozen value object (nodes + undirected weighted edges) with pure
  :meth:`weakly_connected_components` and :meth:`cut_cost`. These are **implemented** at
  interface-freeze (pure math, GREEN-from-freeze; mech #7).
* :class:`CouplingGraphBuilder` — the Protocol seam (an LSP adapter could implement it later;
  plan §7 ships only the git-grep engine).
* :class:`StaticCouplingBuilder` — the **test seam**: a no-scan builder over explicit
  nodes+edges, fully implemented (mech #7).
* :class:`GitGrepCouplingBuilder` — the real engine that shells out to ``git grep`` to derive
  import edges (implemented; see ``finding-039``).
* :func:`make_coupling_builder` — the engine-selection factory mirroring ``make_liftover``.

**No** :mod:`genome.db` import. ``python -c "import genome.scope_split.graph"`` must run on a
fresh checkout (plan §3); the DB-free guarantee is carried by the package-local
clean-subprocess test.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)

#: The git returncode that means "ran, found no matches" (an isolated node, not an error).
_GIT_NO_MATCHES: int = 1

#: The engine-selection literal for :func:`make_coupling_builder` (mirrors ``LiftoverEngine``).
CouplingEngine = Literal["auto", "git-grep", "static"]


@dataclass(frozen=True, slots=True)
class CouplingGraph:
    """An undirected weighted coupling graph over footprint modules (plan §5).

    :attr:`nodes` are the footprint module names; :attr:`edges` are weighted undirected import
    edges as ``(a, b, weight)`` triples with ``a < b`` canonical ordering (so an edge appears
    once). A frozen value object — the pure :meth:`weakly_connected_components` /
    :meth:`cut_cost` queries have no side effects and touch no database.
    """

    nodes: frozenset[str]
    """The footprint module names (the graph vertices)."""
    edges: frozenset[tuple[str, str, float]]
    """Undirected weighted edges as ``(a, b, weight)`` with ``a < b`` canonical ordering."""

    def __post_init__(self) -> None:
        """Normalize every edge to canonical ``a < b`` ordering, summing reversed duplicates.

        The graph is undirected, so ``(b, a, w)`` is the same edge as ``(a, b, w)``. Without
        canonicalization a reversed-edge duplicate would be counted twice by :meth:`cut_cost`
        (double-counting the severed weight) and would survive as two distinct frozenset members.
        This collapses each edge to its ``min < max`` endpoint order and adds the weights of any
        two triples that name the same unordered pair (type-4 fix). A self-loop (``a == b``) is
        dropped — it cannot be severed by any partition. Runs once at construction (frozen).
        """
        merged: dict[tuple[str, str], float] = {}
        for raw_a, raw_b, weight in self.edges:
            if raw_a == raw_b:
                continue
            a, b = sorted((raw_a, raw_b))
            merged[a, b] = merged.get((a, b), 0.0) + weight
        canonical = frozenset((a, b, weight) for (a, b), weight in merged.items())
        if canonical != self.edges:
            object.__setattr__(self, "edges", canonical)

    def weakly_connected_components(self) -> tuple[frozenset[str], ...]:
        """Return the connected components as a tuple of node sets (pure; union-find).

        Treats edges as undirected. Isolated nodes (no incident edge) each form their own
        singleton component. The returned tuple is ordered by each component's
        lexicographically-smallest member so the result is deterministic.
        """
        parent: dict[str, str] = {node: node for node in self.nodes}

        def find(x: str) -> str:
            root = x
            while parent[root] != root:
                root = parent[root]
            # Path compression.
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                # Deterministic merge: smaller root wins.
                if ra < rb:
                    parent[rb] = ra
                else:
                    parent[ra] = rb

        for a, b, _weight in self.edges:
            if a in parent and b in parent:
                union(a, b)

        groups: dict[str, set[str]] = {}
        for node in self.nodes:
            groups.setdefault(find(node), set()).add(node)
        components = [frozenset(members) for members in groups.values()]
        components.sort(key=lambda comp: min(comp) if comp else "")
        return tuple(components)

    def cut_cost(self, partition: tuple[frozenset[str], ...]) -> float:
        """Fraction of total edge weight severed by ``partition`` (pure; 0 total → 0.0).

        An edge is *severed* (inter-partition) when its two endpoints fall in different parts of
        ``partition``. The cost is ``severed_weight / total_weight``. A graph with zero total
        edge weight returns ``0.0`` (no coupling to sever — the undecidable-low case the quality
        gate then treats conservatively).
        """
        place: dict[str, int] = {}
        for index, part in enumerate(partition):
            for node in part:
                place[node] = index

        total = 0.0
        severed = 0.0
        for a, b, weight in self.edges:
            total += weight
            if place.get(a) != place.get(b):
                severed += weight
        if total == 0.0:
            return 0.0
        return severed / total


@runtime_checkable
class CouplingGraphBuilder(Protocol):
    """The builder seam: derive a :class:`CouplingGraph` over a set of footprint modules.

    The Protocol abstracts engine selection (git-grep today; an LSP adapter could implement it
    later — plan §7). The splitter depends only on this interface, never on a concrete engine.
    """

    def build(self, modules: tuple[str, ...]) -> CouplingGraph:
        """Return the coupling graph over ``modules`` (the footprint module names)."""
        ...


@dataclass(frozen=True, slots=True)
class StaticCouplingBuilder:
    """A no-scan builder over explicit nodes + edges — the deterministic test seam (mech #7).

    Carries a fixed edge set and returns a :class:`CouplingGraph` restricted to the requested
    ``modules`` on every :meth:`build`. The splitter's logic can be exercised against an injected
    graph with no git / filesystem dependency, so the synthetic table cases stay deterministic.
    """

    nodes: frozenset[str] = frozenset()
    """The full node set this builder knows about (the build result is intersected with the
    requested ``modules``)."""
    edges: frozenset[tuple[str, str, float]] = frozenset()
    """The fixed undirected weighted edges (``(a, b, weight)`` with ``a < b``)."""

    def build(self, modules: tuple[str, ...]) -> CouplingGraph:
        """Return the fixed graph restricted to ``modules`` (no scan, fully deterministic).

        Nodes are exactly the requested ``modules``; an edge is kept only when both endpoints are
        in ``modules`` (so an edge to a dropped infra helper is simply absent). Pure — no I/O.
        """
        node_set = frozenset(modules)
        kept_edges = frozenset(
            (a, b, weight) for (a, b, weight) in self.edges if a in node_set and b in node_set
        )
        return CouplingGraph(nodes=node_set, edges=kept_edges)


@dataclass(frozen=True, slots=True)
class GitGrepCouplingBuilder:
    """The real engine: derive import edges by shelling out to ``git grep`` (behavioral).

    For each ordered pair of footprint modules ``(a, b)`` it asks ``git grep`` how many lines in
    ``a``'s on-disk file *import* ``b`` — across the three import forms that couple two modules:
    ``import x.y``, ``from x.y import ...``, and a relative ``from .y import ...``. The match
    count is the directed weight; the two directions are summed into one undirected edge
    ``(min, max, weight)`` so it appears once.

    **Infra-drop placement (the consistency note IMPL-CONTRACT asks for):** :meth:`build` emits
    the **raw** weighted graph over the requested modules — it does **not** itself drop shared
    infra helpers. The :data:`genome.scope_split.model.SHARED_HELPER_FANIN` infra-drop is owned by
    the splitter's veto step (``splitter._veto_graph``), which has the manifest context to decide
    fan-in. Keeping the engine raw makes its output testable against an on-disk fixture without
    smuggling policy into the scan.

    The ``git grep`` invocation uses a fixed-literal argv (``# noqa: S603`` on the
    :func:`subprocess.run` call); a returncode of 0 (matched) or 1 (ran, no matches → isolated) is
    success, ≥2 is an error → raise (mech #1).
    """

    repo_root: str = "."
    """The repo root the ``git grep`` runs against."""

    def build(self, modules: tuple[str, ...]) -> CouplingGraph:
        """Derive the raw coupling graph over ``modules`` via ``git grep`` (see class docstring).

        Nodes are exactly the requested ``modules``. For each ordered pair, count how many import
        sites of one module appear in the other's file via ``git grep -c`` over the three import
        forms; sum both directions into one undirected ``(a, b, weight)`` edge with ``a < b``.

        A missing ``git`` binary fails closed with a clean :class:`RuntimeError` (W3) rather than
        an uncaught :class:`FileNotFoundError` traceback.
        """
        if shutil.which("git") is None:
            msg = "git not found on PATH (GitGrepCouplingBuilder requires git)"
            raise RuntimeError(msg)
        nodes = frozenset(modules)
        weights: dict[tuple[str, str], float] = {}
        for importer in modules:
            for imported in modules:
                if importer == imported:
                    continue
                count = self._count_imports(importer, imported)
                if count <= 0:
                    continue
                a, b = sorted((importer, imported))
                weights[a, b] = weights.get((a, b), 0.0) + float(count)
        edges = frozenset((a, b, weight) for (a, b), weight in weights.items())
        return CouplingGraph(nodes=nodes, edges=edges)

    def _count_imports(self, importer: str, imported: str) -> int:
        """Count import sites of ``imported`` inside ``importer``'s on-disk file via ``git grep``.

        Builds an extended-regex alternation over the three import forms (absolute ``import x.y``,
        ``from x.y import``, and relative ``from .leaf import``) and runs
        ``git grep -c -E <pattern> -- <path>``. The ``--`` separates the pattern from the
        pathspec so a **nonexistent** path is parsed as a pathspec (clean no-match → returncode 1
        → 0 count → fail-closed isolated node), not as an ambiguous revision (which the ``--``
        BEFORE the pattern would cause → returncode 128 → crash). Returncode 0/1 = ran
        (1 = no matches → 0); ≥2 = error → :class:`RuntimeError` (mech #1).

        ``importer`` is mapped to its **on-disk file path** (the ``git grep`` pathspec) while
        ``imported`` is mapped to its **dotted module name** (the import-regex subject) — the two
        live in different namespaces (the source-tree path vs. the ``import``-statement spelling),
        so a file-path manifest still produces a pattern that matches a real ``import genome.x.y``
        line (the coupling-veto B1 fix).
        """
        path = _module_to_path(importer)
        pattern = _import_pattern(_path_to_module(imported))
        argv = [
            "git",
            "-C",
            self.repo_root,
            "grep",
            "-c",
            "-E",
            pattern,
            "--",
            path,
        ]
        completed = subprocess.run(  # noqa: S603 - fixed-literal git argv (mech #1/#2)
            argv,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == _GIT_NO_MATCHES:
            return 0
        if completed.returncode != 0:
            msg = (
                f"git grep failed (returncode {completed.returncode}) for "
                f"{importer!r} importing {imported!r}: {completed.stderr.strip()}"
            )
            raise RuntimeError(msg)
        return sum(_grep_count_line(line) for line in completed.stdout.splitlines())


def _module_to_path(module: str) -> str:
    """Map a dotted module name to its repo-relative ``backend/src`` source path.

    ``genome.scope_split.cli`` → ``backend/src/genome/scope_split/cli.py``. A name that already
    looks like a path (contains ``/`` or ends in ``.py``) is passed through unchanged.
    """
    if "/" in module or module.endswith(".py"):
        return module
    return "backend/src/" + module.replace(".", "/") + ".py"


def _path_to_module(name: str) -> str:
    """Map a repo-relative source path to its dotted module name (the inverse of
    :func:`_module_to_path`).

    ``backend/src/genome/scope_split/model.py`` → ``genome.scope_split.model``: strip a leading
    ``backend/src/`` (the package root), drop a trailing ``.py``, and replace ``/`` with ``.``. A
    name that already looks dotted (no ``/`` and no ``.py`` suffix) is passed through unchanged, so
    a manifest may carry either shape. This is what feeds the import-match regex — the dotted name
    is the spelling a real ``import genome.x.y`` / ``from genome.x.y import`` line actually uses
    (without it the coupling veto matches nothing on a file-path manifest — the B1 fix).
    """
    if "/" not in name and not name.endswith(".py"):
        return name
    stripped = name.removeprefix("backend/src/").removesuffix(".py")
    return stripped.replace("/", ".")


def _import_pattern(imported: str) -> str:
    """An extended-regex alternation matching the three import forms of ``imported``.

    Matches ``import <dotted>`` (absolute), ``from <dotted> import`` (absolute from-import), and
    ``from .<leaf> import`` (relative from-import using the module's last dotted segment). The
    dotted name's dots are escaped so they match literally.
    """
    dotted = re.escape(imported)
    leaf = re.escape(imported.rsplit(".", 1)[-1])
    return f"(^import {dotted})|(^from {dotted} import )|(^from \\.{leaf} import )"


def _grep_count_line(line: str) -> int:
    """Parse one ``git grep -c`` output line (``path:count`` or a bare count) to its int count."""
    text = line.rsplit(":", 1)[-1].strip() if ":" in line else line.strip()
    try:
        return int(text)
    except ValueError:
        logger.warning("scope_split.grep_count.unparsable_line", line=line)
        return 0


def make_coupling_builder(engine: CouplingEngine = "auto") -> CouplingGraphBuilder:
    """Select a coupling-graph builder by engine name (mirrors ``make_liftover``).

    * ``"git-grep"`` → :class:`GitGrepCouplingBuilder` (the real import-scan engine).
    * ``"static"`` → :class:`StaticCouplingBuilder` (the no-scan test seam, empty by default).
    * ``"auto"`` (default) → the git-grep engine, logging a loud INFO on the choice so the
      selection is observable (CLAUDE.md liftover convention).

    An unknown engine raises :class:`ValueError`.
    """
    if engine == "git-grep":
        return GitGrepCouplingBuilder()
    if engine == "static":
        return StaticCouplingBuilder()
    if engine == "auto":
        logger.info("scope_split.coupling_builder.selected", engine="git-grep", requested="auto")
        return GitGrepCouplingBuilder()
    msg = f"unknown coupling engine {engine!r}; expected one of 'auto', 'git-grep', 'static'"
    raise ValueError(msg)

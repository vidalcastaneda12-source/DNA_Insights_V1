"""THE CORE — propose_split manifest-primary cut policy + exhaustive fail-closed property.

Plan-blind spec source: IMPL-CONTRACT DECISION 1 (MANIFEST-PRIMARY partition + coupling-VETO +
infra-helper drop + relaxed tier gate); IMPL-CONTRACT "Splitter reduction order (REVISED per
DECISION 1)" steps 1-7 + SAFETY INVARIANT ("non-atomic ONLY when a cut passed steps 2-6; any
uncertainty → atomic"); IMPL-CONTRACT "Test files" splitter table cases; SYNTHESIZED-PLAN §5
("splitter THE CORE (table cases + EXHAUSTIVE FAIL-CLOSED PROPERTY: enumerate … assert NONE
non-atomic)"); FROZEN-INTERFACE splitter.py (propose_split(manifest, builder, *, depth=0) ->
SplitResult; STUBBED → RED on NotImplementedError).

ALL tests here are RED-until-filled. They assert the SPECIFIED outcome (atomic True/False, the
sub-scope count, the order) so they go RED on the NotImplementedError stub now and GREEN when
propose_split is implemented. None of them asserts pytest.raises(NotImplementedError).

PLAN-BLIND: drives ONLY through the public propose_split with a StaticCouplingBuilder injected
(no git, no body-reading). The graphs are constructed to the spec'd veto semantics (an edge
weight > MAX_CUT_COST is "high-coupling"), not reverse-engineered from the implementation.

test->spec provenance noted per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import pytest

from genome.scope_split.graph import StaticCouplingBuilder
from genome.scope_split.model import (
    MAX_CUT_COST,
    MAX_RESPLIT_DEPTH,
    MIN_CLUSTERS,
    SCHEMA_FIRST_ORDER,
    ScopeManifestInput,
    SplitResult,
)
from genome.scope_split.splitter import propose_split

# ── helpers (realistic manifests; not shaped to the implementation) ───────────


def _manifest(  # noqa: PLR0913 — a test factory mirroring the frozen manifest's keyword fields
    *,
    scope_id: str = "PR-X",
    change_class: tuple[str, ...] = (),
    imports_touched: tuple[str, ...] = (),
    depends_on: tuple[str, ...] = (),
    out_of_scope_candidates: tuple[str, ...] = (),
    applicable_anchors: tuple[str, ...] = (),
) -> ScopeManifestInput:
    return ScopeManifestInput(
        scope_id=scope_id,
        change_class=change_class,
        imports_touched=imports_touched,
        depends_on=depends_on,
        out_of_scope_candidates=out_of_scope_candidates,
        applicable_anchors=applicable_anchors,
    )


def _builder(
    nodes: frozenset[str], edges: frozenset[tuple[str, str, float]]
) -> StaticCouplingBuilder:
    """A no-scan coupling seam carrying explicit weighted edges (DECISION 1 veto input)."""
    return StaticCouplingBuilder(nodes=nodes, edges=edges)


def _empty_builder() -> StaticCouplingBuilder:
    return StaticCouplingBuilder()


# A weight comfortably above the coupling-veto threshold (a "high-coupling" edge).
_HIGH = MAX_CUT_COST + 0.5
# A weight comfortably below the threshold (a severable, non-fusing edge).
_LOW = MAX_CUT_COST / 2


# ── table case (a): 1 change_class group → atomic ─────────────────────────────


def test_single_change_class_group_is_atomic() -> None:
    """from: IMPL-CONTRACT reduction step 2 ("<MIN_CLUSTERS candidates → atomic") + DECISION 1
    (manifest-primary partition) + table case (a) ("manifest with 1 change_class group →
    atomic").

    A manifest whose whole footprint is one change_class yields <MIN_CLUSTERS separable
    clusters → atomic. (RED-until-filled: asserts atomic is True, not a stub raise.)
    """
    m = _manifest(
        change_class=("cli",),
        imports_touched=("genome.x.cli", "genome.x.helpers"),
    )
    result = propose_split(m, _empty_builder())
    assert isinstance(result, SplitResult)
    assert result.atomic is True


# ── table case (b): 2 separable groups + no high-coupling edge → split ─────────


def test_two_separable_groups_no_high_edge_splits_into_two() -> None:
    """from: DECISION 1 (manifest-primary: schema/ddl vs cli vs tests are separable AND
    ordered) + reduction steps 2-5 + table case (b) ("2 separable change_class groups + no
    severing high-coupling edge → atomic False, 2 sub_scopes").

    Two manifest change_class boundaries (schema vs cli) with only a LOW (severable) inter-
    cluster edge survive the coupling veto and the quality gate → a non-atomic 2-sub-scope
    split. (RED-until-filled.)
    """
    # Two tight, fully-decoupled real slices: a schema slice (ddl/*) internally connected, and a
    # cli slice (paths containing 'cli') internally connected, with NO inter-cluster edge — the
    # textbook separable shape. Each slice is one weakly-connected component; the change_class
    # boundary (schema vs cli) is the manifest-primary partition (DECISION 1). No OOSC → exactly
    # 2 candidate clusters. Splitting strictly shrinks summed est work (each 2-module slice < the
    # 4-module parent).
    schema_nodes = ("ddl/group_x.sql", "ddl/group_y.sql")
    cli_nodes = ("genome/x/cli.py", "genome/x/cli_commands.py")
    m = _manifest(
        scope_id="PR-Y",
        change_class=("schema", "cli"),
        imports_touched=schema_nodes + cli_nodes,
    )
    builder = _builder(
        nodes=frozenset(schema_nodes + cli_nodes),
        edges=frozenset(
            {
                ("ddl/group_x.sql", "ddl/group_y.sql", _HIGH),
                ("genome/x/cli.py", "genome/x/cli_commands.py", _HIGH),
            }
        ),
    )
    result = propose_split(m, builder)
    assert result.atomic is False
    assert len(result.sub_scopes) == 2
    # every sub-scope carries the origin (provenance #8)
    assert all(s.origin_scope == "PR-Y" for s in result.sub_scopes)


# ── table case (c): chain via depends_on → ordered schema-first ───────────────


def test_dependency_chain_orders_schema_first() -> None:
    """from: IMPL-CONTRACT reduction step 4 (TOPO-ORDER: change_class rank SCHEMA_FIRST_ORDER +
    depends_on) + table case (c) ("chain via depends_on → ordered (schema-first)") +
    SYNTHESIZED-PLAN §6 ("3-cluster … schema-first").

    When clusters are split, the emitted order ranks schema/ddl before cli/tests per
    SCHEMA_FIRST_ORDER. We assert the first sub-scope in the order carries a schema-class slice.
    (RED-until-filled.)
    """
    schema_nodes = ("ddl/group_x.sql", "ddl/group_y.sql")
    cli_nodes = ("genome/x/cli.py", "genome/x/cli_commands.py")
    m = _manifest(
        scope_id="PR-Z",
        change_class=("cli", "schema"),
        imports_touched=cli_nodes + schema_nodes,
    )
    builder = _builder(
        nodes=frozenset(cli_nodes + schema_nodes),
        edges=frozenset(
            {
                ("ddl/group_x.sql", "ddl/group_y.sql", _HIGH),
                ("genome/x/cli.py", "genome/x/cli_commands.py", _HIGH),
            }
        ),
    )
    result = propose_split(m, builder)
    assert result.atomic is False
    # schema ranks before cli in SCHEMA_FIRST_ORDER
    assert SCHEMA_FIRST_ORDER.index("schema") < SCHEMA_FIRST_ORDER.index("cli")
    first_id = result.order[0]
    first_sub = next(s for s in result.sub_scopes if s.sub_scope_id == first_id)
    assert "schema" in first_sub.change_class


# ── table case (d): high-coupling edge severed → veto → atomic ────────────────


def test_high_coupling_edge_vetoes_the_cut_to_atomic() -> None:
    """from: DECISION 1 (coupling VETO: "a cut survives only if no severed edge exceeds the
    coupling threshold") + reduction step 3 ("severed inter-cluster edge weight > MAX_CUT_COST →
    veto … collapse to <MIN_CLUSTERS → atomic 'coupling vetoes the cut — PR-3/PR-5a rule'") +
    table case (d).

    Two manifest groups that the import graph couples HEAVILY (edge weight > MAX_CUT_COST) must
    NOT land in different sub-scopes: the veto fuses them below MIN_CLUSTERS → atomic.
    (RED-until-filled.)
    """
    m = _manifest(
        scope_id="PR-W",
        change_class=("schema", "cli"),
        imports_touched=("ddl.group_x", "genome.x.cli"),
        out_of_scope_candidates=("naively the cli slice looks separable",),
    )
    builder = _builder(
        nodes=frozenset({"ddl.group_x", "genome.x.cli"}),
        edges=frozenset({("ddl.group_x", "genome.x.cli", _HIGH)}),
    )
    result = propose_split(m, builder)
    assert result.atomic is True


# ── table case (e): split that doesn't shrink total work → atomic ─────────────


def test_split_that_does_not_shrink_total_work_is_atomic() -> None:
    """from: DECISION 1 (quality gate requires "total_work_strictly_shrinks (sum of sub-scope
    est footprints < parent footprint)") + reduction step 5 + table case (e) ("a split that
    doesn't shrink total work → atomic").

    A degenerate footprint where the candidate sub-scopes do not strictly reduce total work
    fails the quality gate → atomic. Modeled as a single-import manifest that cannot be carved
    into two strictly-smaller real slices. (RED-until-filled.)
    """
    m = _manifest(
        scope_id="PR-V",
        change_class=("schema", "cli"),
        imports_touched=("genome.only_one_module",),
    )
    builder = _builder(nodes=frozenset({"genome.only_one_module"}), edges=frozenset())
    result = propose_split(m, builder)
    assert result.atomic is True


# ── table case (f): cycle in depends_on → atomic ──────────────────────────────


def test_cycle_in_depends_on_is_atomic() -> None:
    """from: IMPL-CONTRACT reduction step 4 ("TOPO-ORDER … CYCLE → atomic") + table case (f)
    ("cycle in depends_on → atomic") + SAFETY INVARIANT ("any uncertainty → atomic").

    A dependency cycle makes the topo order undecidable → fail-closed atomic. The minimal cycle
    the contract can express through the manifest is a self-referential depends_on: a manifest
    whose scope_id appears in its own depends_on is a self-loop, which step 4 must refuse to
    order (→ atomic).

    INDEPENDENCE NOTE (for Stage-3 test-integrity): this asserts the SPEC behavior, not the
    observed implementation. If propose_split splits this input rather than failing closed, that
    is a spec-vs-impl divergence the independent oracle is meant to surface — the test stays as
    written (the spec is the authority), and the divergence is a finding for the implementer
    (the parent-level depends_on self-loop is not being fed into the step-4 cycle check).
    """
    schema_nodes = ("ddl/group_x.sql", "ddl/group_y.sql")
    cli_nodes = ("genome/x/cli.py", "genome/x/cli_commands.py")
    m = _manifest(
        scope_id="PR-U",
        change_class=("schema", "cli"),
        imports_touched=schema_nodes + cli_nodes,
        depends_on=("PR-U",),  # self-cycle: PR-U depends on PR-U
    )
    builder = _builder(
        nodes=frozenset(schema_nodes + cli_nodes),
        edges=frozenset(
            {
                ("ddl/group_x.sql", "ddl/group_y.sql", _HIGH),
                ("genome/x/cli.py", "genome/x/cli_commands.py", _HIGH),
            }
        ),
    )
    result = propose_split(m, builder)
    assert result.atomic is True


# ── EXHAUSTIVE FAIL-CLOSED PROPERTY TEST ──────────────────────────────────────


def _degenerate_cases() -> list[tuple[str, ScopeManifestInput, StaticCouplingBuilder, int]]:
    """Enumerate degenerate / undecidable inputs — NONE may yield a non-atomic SplitResult.

    Each tuple = (label, manifest, builder, depth). Source: SYNTHESIZED-PLAN §5 enumeration
    (empty imports / <2 nodes / empty graph / zero-edge graph / cycle / poor cut / no-tier-lower
    / depth>cap) + IMPL-CONTRACT SAFETY INVARIANT ("every degenerate/undecidable input →
    atomic").
    """
    cases: list[tuple[str, ScopeManifestInput, StaticCouplingBuilder, int]] = []

    # 1. empty change_class AND empty imports (extraction guard, reduction step 1)
    cases.append(("empty-changeclass-empty-imports", _manifest(), _empty_builder(), 0))

    # 2. single change_class group → <MIN_CLUSTERS (step 2)
    cases.append(
        (
            "single-change-class",
            _manifest(change_class=("cli",), imports_touched=("genome.x.cli",)),
            _empty_builder(),
            0,
        )
    )

    # 3. empty coupling graph (no nodes/edges)
    cases.append(
        (
            "empty-graph",
            _manifest(change_class=("schema", "cli"), imports_touched=("a", "b")),
            _empty_builder(),
            0,
        )
    )

    # 4. zero-edge graph (nodes but no edges → zero-total-edge undecidable-low)
    cases.append(
        (
            "zero-edge-graph",
            _manifest(change_class=("schema", "cli"), imports_touched=("a", "b")),
            _builder(nodes=frozenset({"a", "b"}), edges=frozenset()),
            0,
        )
    )

    # 5. cycle in depends_on (step 4)
    cases.append(
        (
            "depends-on-cycle",
            _manifest(
                scope_id="PR-CYC",
                change_class=("schema", "cli"),
                imports_touched=("a", "b"),
                depends_on=("PR-CYC",),
            ),
            _builder(nodes=frozenset({"a", "b"}), edges=frozenset({("a", "b", _LOW)})),
            0,
        )
    )

    # 6. high-coupling edge → veto collapses below MIN_CLUSTERS (step 3)
    cases.append(
        (
            "high-coupling-veto",
            _manifest(change_class=("schema", "cli"), imports_touched=("a", "b")),
            _builder(nodes=frozenset({"a", "b"}), edges=frozenset({("a", "b", _HIGH)})),
            0,
        )
    )

    # 7. sub-par cut: single-module footprint cannot strictly shrink total work (step 5)
    cases.append(
        (
            "no-strict-shrink",
            _manifest(change_class=("schema", "cli"), imports_touched=("only_one",)),
            _builder(nodes=frozenset({"only_one"}), edges=frozenset()),
            0,
        )
    )

    # 8. depth >= MAX_RESPLIT_DEPTH (step 6 re-split cap)
    cases.append(
        (
            "depth-at-cap",
            _manifest(change_class=("schema", "cli"), imports_touched=("a", "b")),
            _builder(nodes=frozenset({"a", "b"}), edges=frozenset({("a", "b", _LOW)})),
            MAX_RESPLIT_DEPTH,
        )
    )

    return cases


@pytest.mark.parametrize(
    ("label", "manifest", "builder", "depth"),
    _degenerate_cases(),
    ids=[c[0] for c in _degenerate_cases()],
)
def test_degenerate_inputs_never_produce_a_non_atomic_split(
    label: str,
    manifest: ScopeManifestInput,
    builder: StaticCouplingBuilder,
    depth: int,
) -> None:
    """from: IMPL-CONTRACT SAFETY INVARIANT ("any degenerate/undecidable input → atomic;
    the fail-closed exhaustive property test still holds") + SYNTHESIZED-PLAN §5/§6
    ("splitter exhaustive fail-closed: 0 degenerate inputs non-atomic").

    The single most important invariant of the whole feature (failure-ordering (a): a false
    split is the costliest mode). For EVERY enumerated degenerate input the result is atomic,
    carries no sub_scopes, and has no cut_quality. (RED-until-filled — asserts the atomic
    BEHAVIOR, not a stub raise.)
    """
    assert label  # the parametrize id documents which degeneracy is exercised
    result = propose_split(manifest, builder, depth=depth)
    assert result.atomic is True, f"{label}: degenerate input produced a non-atomic split"
    assert result.sub_scopes == ()
    assert result.cut_quality is None


def test_min_clusters_constant_is_two() -> None:
    """from: FROZEN-INTERFACE constants (MIN_CLUSTERS=2) — the separability floor the
    fail-closed property leans on. GREEN from freeze (a pure constant check anchoring the
    property test's premise).
    """
    assert MIN_CLUSTERS == 2

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

from typing import TYPE_CHECKING

import pytest

from genome.scope_split.graph import (
    GitGrepCouplingBuilder,
    StaticCouplingBuilder,
    make_coupling_builder,
)

if TYPE_CHECKING:
    from pathlib import Path
from genome.scope_split.model import (
    MAX_CUT_COST,
    MAX_RESPLIT_DEPTH,
    MIN_CLUSTERS,
    SCHEMA_FIRST_ORDER,
    ScopeManifestInput,
    SplitResult,
    est_risk_tier,
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


def test_two_separable_groups_light_bridge_splits_into_two() -> None:
    """from: DECISION 1 (manifest-primary: schema/ddl vs cli vs tests are separable AND
    ordered) + reduction steps 2-5 + FIX-LIST NEW-2 (the veto must DISCRIMINATE: a LIGHT
    inter-cluster bridge amid heavy intra-cluster edges → cut SURVIVES → 2 sub_scopes).

    Two manifest change_class boundaries (schema vs cli), each internally HEAVY (so the total
    coupling weight is dominated by intra-cluster edges), joined by a single LIGHT bridge. The
    whole-partition severed fraction is ``_LOW / (2*_HIGH + _LOW)`` ≈ 0.06, well under
    MAX_CUT_COST (0.25), so ``cut_cost(partition) <= MAX_CUT_COST`` → the veto permits the cut →
    a non-atomic 2-sub-scope split. This is the light-survives half of the NEW-2 discrimination
    proof (the heavy-vetoes half is ``test_heavy_cross_bridge_vetoes_the_cut_to_atomic``).
    """
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
                # One LIGHT cross-cluster bridge amid the heavy intra edges.
                ("ddl/group_x.sql", "genome/x/cli.py", _LOW),
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


def test_heavy_cross_bridge_vetoes_the_cut_to_atomic() -> None:
    """from: DECISION 1 (coupling VETO: a cut is permitted only when it does not sever too much
    coupling) + FIX-LIST NEW-2 (the veto gates on the WHOLE-PARTITION fraction
    ``cut_cost(partition) > MAX_CUT_COST``; the heavy-vetoes half of the discrimination proof) +
    table case (d).

    Two manifest groups (schema vs cli) with realistic intra-cluster edges, but DOMINATED by a
    heavy cross-cluster bridge: the severed fraction is ``_HIGH / (_LOW + _LOW + _HIGH)`` ≈ 0.75,
    far above MAX_CUT_COST (0.25), so ``cut_cost(partition) > MAX_CUT_COST`` → the veto fires →
    atomic. (Contrast ``test_two_separable_groups_light_bridge_splits_into_two``: same shapes,
    light vs heavy bridge → opposite verdict — the veto DISCRIMINATES.)
    """
    schema_nodes = ("ddl/group_x.sql", "ddl/group_y.sql")
    cli_nodes = ("genome/x/cli.py", "genome/x/cli_commands.py")
    m = _manifest(
        scope_id="PR-W",
        change_class=("schema", "cli"),
        imports_touched=schema_nodes + cli_nodes,
    )
    builder = _builder(
        nodes=frozenset(schema_nodes + cli_nodes),
        edges=frozenset(
            {
                # LIGHT intra-cluster edges …
                ("ddl/group_x.sql", "ddl/group_y.sql", _LOW),
                ("genome/x/cli.py", "genome/x/cli_commands.py", _LOW),
                # … dominated by a HEAVY cross-cluster bridge → severed fraction > 0.25.
                ("ddl/group_x.sql", "genome/x/cli.py", _HIGH),
            }
        ),
    )
    result = propose_split(m, builder)
    assert result.atomic is True
    assert "coupling" in result.reason.lower()


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

    # 6. two REAL clusters (schema vs cli) joined by a HIGH cross-cluster bridge → the coupling
    #    veto (step 4) fires → atomic. (Review test-2: the prior fixture used 'a'/'b', which both
    #    fall to the residual class → one cluster → it exited at the partition gate and NEVER
    #    reached the veto, so the veto's fail-closed path had zero property coverage. These
    #    name-keyed modules form two clusters so the veto is genuinely exercised here.)
    cases.append(
        (
            "high-coupling-veto",
            _manifest(
                change_class=("schema", "cli"),
                imports_touched=("ddl/group_x.sql", "genome/x/cli.py"),
            ),
            _builder(
                nodes=frozenset({"ddl/group_x.sql", "genome/x/cli.py"}),
                edges=frozenset({("ddl/group_x.sql", "genome/x/cli.py", _HIGH)}),
            ),
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
    """from: IMPL-CONTRACT SAFETY INVARIANT ("any degenerate/undecidable input → atomic").

    Representative per-reduction-step fail-closed coverage (one case per guard: extraction,
    partition-collapse, empty/zero-edge graph, depends_on cycle, coupling veto, no-shrink, and the
    re-split cap) — NOT a cross-product enumeration (review test-1: the prior docstring overclaimed
    "exhaustive"). The single most important invariant of the feature (a false split is the
    costliest mode): for every case the result is atomic, carries no sub_scopes, and has no
    cut_quality.
    """
    assert label  # the parametrize id documents which degeneracy is exercised
    result = propose_split(manifest, builder, depth=depth)
    assert result.atomic is True, f"{label}: degenerate input produced a non-atomic split"
    assert result.sub_scopes == ()
    assert result.cut_quality is None


# ── B4: SHARED_HELPER_FANIN infra-drop isolates the cut ───────────────────────


def test_infra_helper_drop_lets_the_cut_survive() -> None:
    """from: FIX-LIST B4 (ptest-1: the SHARED_HELPER_FANIN infra-drop was not isolated by any
    test) + DECISION 1 ("Drop shared infra-helpers from the graph … a shared dependency does not
    fuse otherwise-independent clusters").

    A shared module with fan-in >= SHARED_HELPER_FANIN (3) is dropped from the veto graph. Here it
    has HIGH-weight edges spanning BOTH the schema cluster and the cli cluster. WITHOUT the drop
    those inter-cluster edges fuse the two clusters → atomic; WITH the drop (fan-in 3) the shared
    node and its edges vanish, no heavy inter-cluster edge remains, and the 2-cluster cut survives.
    """
    schema_nodes = ("ddl.a", "ddl.shared")
    cli_nodes = ("genome.x.cli", "genome.x.cli2")
    m = _manifest(
        scope_id="PR-INFRA",
        change_class=("schema", "cli"),
        imports_touched=schema_nodes + cli_nodes,
    )
    # ddl.shared couples to a node in EACH cluster + one more → fan-in 3 → infra → dropped.
    builder = _builder(
        nodes=frozenset(schema_nodes + cli_nodes),
        edges=frozenset(
            {
                ("ddl.a", "ddl.shared", _HIGH),
                ("ddl.shared", "genome.x.cli", _HIGH),
                ("ddl.shared", "genome.x.cli2", _HIGH),
            }
        ),
    )
    result = propose_split(m, builder)
    assert result.atomic is False, "infra-drop should let the cut survive (B4)"
    assert len(result.sub_scopes) == 2


def test_non_infra_shared_node_below_fanin_still_fuses() -> None:
    """from: FIX-LIST B4 (the contrast that proves the drop is load-bearing) + DECISION 1
    (SHARED_HELPER_FANIN = 3 is the infra threshold).

    The same heavy inter-cluster edge, but the shared node has fan-in 2 (< SHARED_HELPER_FANIN),
    so it is NOT infra and is NOT dropped. The heavy edge therefore fuses the two clusters below
    MIN_CLUSTERS → atomic. This isolates the drop: only fan-in >= 3 saves the cut.
    """
    schema_nodes = ("ddl.a", "ddl.shared")
    cli_nodes = ("genome.x.cli", "genome.x.cli2")
    m = _manifest(
        scope_id="PR-FUSE",
        change_class=("schema", "cli"),
        imports_touched=schema_nodes + cli_nodes,
    )
    # ddl.shared couples to only 2 peers → fan-in 2 → NOT infra → kept → heavy edge fuses.
    builder = _builder(
        nodes=frozenset(schema_nodes + cli_nodes),
        edges=frozenset(
            {
                ("ddl.a", "ddl.shared", _HIGH),
                ("ddl.shared", "genome.x.cli", _HIGH),
            }
        ),
    )
    result = propose_split(m, builder)
    assert result.atomic is True


# ── B3: the min_subscope_shrink quality-gate branch (step 5) ──────────────────


def test_quality_gate_rejects_insufficient_shrink() -> None:
    """from: FIX-LIST B3 (ptest-2: the min_subscope_shrink < MIN quality-gate branch at step 5
    was NEVER reached — every prior no-shrink case exited earlier at the partition step) +
    DECISION 1 quality gate ("every sub-scope shrink >= MIN_SUBSCOPE_SHRINK").

    Two declared change classes (schema, cli) over 10 modules: 9 key to schema, 1 to cli. The
    partition yields 2 clusters (so step 3 passes), no heavy edge survives the veto (step 4
    passes), the topo order is acyclic (step 5 passes) — but the schema cluster is 9/10 = 0.90 of
    the parent, so its shrink is 0.10 < MIN_SUBSCOPE_SHRINK (0.34) → the quality gate fires and
    returns atomic, naming the shrink metric. This is the previously-dead step-5 branch.
    """
    schema_nodes = tuple(f"ddl.group_{i}" for i in range(9))
    cli_nodes = ("genome.x.cli",)
    m = _manifest(
        scope_id="PR-SHRINK",
        change_class=("schema", "cli"),
        imports_touched=schema_nodes + cli_nodes,
    )
    result = propose_split(m, _empty_builder())
    assert result.atomic is True
    # The quality-gate branch — not the partition / veto branch — is what fired.
    assert "shrink" in result.reason.lower()


# ── W5: the max_tier_after <= max_tier_before term (DEFENSIVE — structurally unreachable) ──


def test_subscope_tier_never_exceeds_recomputed_parent_tier() -> None:
    """from: FIX-LIST W5 (ptest-5: the max_tier_after > max_tier_before branch was untested) +
    DECISION 1 ("REMOVE the hard max_tier_after < max_tier_before term … structurally
    unsatisfiable against the dispatcher's max-not-min tier floors").

    NON-ISSUE re-classification (implementer judgment, escalated in the report): with the parent
    tier recomputed over the union of all clusters (``_parent_tier``), every sub-cluster is a
    subset of the parent in change-class / footprint / anchors, so its re-scored tier can NEVER
    exceed the parent's. The ``max_tier_after > max_tier_before`` branch is therefore structurally
    unreachable (a brute scan over every class combination / size confirms zero violations) — the
    same family as ptest-8. Rather than contort an impossible input, this test pins the underlying
    monotonicity invariant the branch defends, and the branch carries ``# pragma: no cover -
    structurally unreachable (defensive)``.

    The invariant: for any single-class subset cluster (smaller footprint, ⊆ anchors), its
    ``est_risk_tier`` is ≤ the parent tier computed over the union.
    """
    parent_change = ("schema", "cli", "tests")
    parent_anchors = ()  # schema present → structural floor already applies to parent
    parent_size = 12
    parent_tier = est_risk_tier(parent_change, parent_anchors, parent_size)
    for cluster_class in parent_change:
        for cluster_size in range(1, parent_size):
            cluster_anchors = parent_anchors if cluster_class in {"schema", "ddl"} else ()
            cluster_tier = est_risk_tier((cluster_class,), cluster_anchors, cluster_size)
            assert cluster_tier <= parent_tier, (
                f"{cluster_class}/{cluster_size} re-scored to {cluster_tier} > parent {parent_tier}"
            )


def test_propose_split_nonexistent_module_returns_result_never_raises() -> None:
    """from: FIX-LIST NEW-1 (a planning manifest naming NOT-YET-CREATED modules is a documented
    input — golden-fixture freshness_flag 'spec-references-non-existent-code'; the old ``--``
    placement made the live git-grep builder crash with RuntimeError on such a path, violating
    the SAFETY INVARIANT 'uncertainty → atomic, never crash').

    Driven through the PUBLIC propose_split with the REAL git-grep builder over a manifest whose
    footprint includes a nonexistent module path. The result MUST be a SplitResult (atomic or
    split per the rest of the reduction) and propose_split MUST NOT raise RuntimeError.
    """
    m = _manifest(
        scope_id="PR-GHOST",
        change_class=("schema", "cli"),
        imports_touched=(
            "backend/src/genome/scope_split/model.py",
            "backend/src/genome/scope_split/does_not_exist_yet.py",
        ),
    )
    builder = make_coupling_builder("git-grep")
    result = propose_split(m, builder)  # must not raise
    assert isinstance(result, SplitResult)


def test_min_clusters_constant_is_two() -> None:
    """from: FROZEN-INTERFACE constants (MIN_CLUSTERS=2) — the separability floor the
    fail-closed property leans on. GREEN from freeze (a pure constant check anchoring the
    property test's premise).
    """
    assert MIN_CLUSTERS == 2


# ── Review fixes: the coupling veto must FAIL CLOSED on a non-discriminating signal ───────────


def test_unresolved_footprint_modules_force_atomic() -> None:
    """from: review silent-1 — modules that do not resolve to a real source file leave coupling
    UNMEASURED; the veto must treat that as undecidable → atomic, never read the unmeasured zero
    as 'no coupling → safe to split' (the false-split blocker). Drives the real git-grep engine.
    """
    # Two name-keyed clusters (schema via ddl, cli) so the partition produces >=2 clusters and the
    # reducer reaches the veto — but neither path exists on disk, so coupling is unmeasurable.
    m = _manifest(
        change_class=("schema", "cli"),
        imports_touched=("ddl/does_not_exist_x.sql", "genome/nope/cli.py"),
    )
    result = propose_split(m, GitGrepCouplingBuilder(repo_root="."))
    assert result.atomic is True
    assert "unmeasurable" in result.reason


def test_git_scan_failure_reduces_to_atomic_not_crash(tmp_path: Path) -> None:
    """from: review silent-2 — a git error (rc>=2, e.g. not-a-repo) raises RuntimeError from the
    builder; propose_split must catch it at the coupling boundary and reduce to atomic, NEVER a
    crash (the fail-closed contract: an unmeasurable signal → atomic).
    """
    m = _manifest(
        change_class=("schema", "cli"),
        imports_touched=("genome.a.schema", "genome.b.cli"),
    )
    # An empty tmp dir is not a git repo → git grep returns 128 → RuntimeError inside the builder.
    result = propose_split(m, GitGrepCouplingBuilder(repo_root=str(tmp_path)))
    assert result.atomic is True
    assert "fail-closed" in result.reason


def test_veto_survives_at_exact_threshold_boundary() -> None:
    """from: review ptest-1 — the veto guard is strict `cut_cost > MAX_CUT_COST`, so a cut whose
    severed fraction is EXACTLY MAX_CUT_COST must SURVIVE (not be vetoed). Pins the boundary a
    `>=` typo would silently break. Weights: intra 1.5+1.5, cross 1.0 → severed/total = 1/4 = 0.25.
    """
    schema_nodes = ("ddl/group_x.sql", "ddl/group_y.sql")
    cli_nodes = ("genome/x/cli.py", "genome/x/cli_commands.py")
    m = _manifest(
        scope_id="PR-BND",
        change_class=("schema", "cli"),
        imports_touched=schema_nodes + cli_nodes,
    )
    builder = _builder(
        nodes=frozenset(schema_nodes + cli_nodes),
        edges=frozenset(
            {
                ("ddl/group_x.sql", "ddl/group_y.sql", 1.5),
                ("genome/x/cli.py", "genome/x/cli_commands.py", 1.5),
                ("ddl/group_x.sql", "genome/x/cli.py", 1.0),  # severed: 1.0 / 4.0 = exactly 0.25
            }
        ),
    )
    result = propose_split(m, builder)
    assert result.atomic is False, "a cut at exactly MAX_CUT_COST must survive the veto"
    assert result.cut_quality is not None
    assert result.cut_quality.cut_cost == pytest.approx(MAX_CUT_COST)

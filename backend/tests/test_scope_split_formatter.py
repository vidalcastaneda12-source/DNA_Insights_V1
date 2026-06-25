"""Formatter units — atomic sentinel rendering + per-sub-scope block, no hard-coded magnitudes.

Plan-blind spec source: FROZEN-INTERFACE formatter.py (ATOMIC_SENTINEL / MICRO_GATE_HEADER are
"real"; format_split_proposal / format_roadmap_block are "STUBBED"); SYNTHESIZED-PLAN §5
("formatter (sentinel; N renders N; origin_scope; no hard-coded magnitudes)") + §4 step 6
("atomic sentinel vs split header + per-sub-scope + cut_quality; no hard-coded magnitudes").

ATOMIC_SENTINEL / MICRO_GATE_HEADER assertions are GREEN from freeze (real constants). The two
rendering tests are RED-until-filled: they assert the BEHAVIOR (the sentinel appears on atomic;
N sub-scopes render N blocks each carrying origin_scope) and so go RED on NotImplementedError
now and GREEN when the bodies land — never pytest.raises(NotImplementedError).

test->spec provenance noted per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

from genome.scope_split.formatter import (
    ATOMIC_SENTINEL,
    MICRO_GATE_HEADER,
    format_split_proposal,
)
from genome.scope_split.model import CutQuality, SplitResult, SubScope


def _sub(idx: int, origin: str, change_class: tuple[str, ...]) -> SubScope:
    return SubScope(
        sub_scope_id=f"{origin}-s{idx}",
        origin_scope=origin,
        change_class=change_class,
        est_imports_touched=idx + 1,
        applicable_anchors=(),
        est_risk_tier=1,
        depends_on=(),
        rationale=f"slice {idx}",
    )


def _split_result(origin: str, n: int) -> SplitResult:
    subs = tuple(_sub(i + 1, origin, ("schema" if i == 0 else "cli",)) for i in range(n))
    cq = CutQuality(
        cut_cost=0.1,
        max_tier_before=2,
        max_tier_after=2,
        min_subscope_shrink=0.5,
        clean=True,
    )
    return SplitResult(
        atomic=False,
        reason=f"{n} separable clusters",
        sub_scopes=subs,
        order=tuple(s.sub_scope_id for s in subs),
        cut_quality=cq,
    )


# ── real constants (GREEN from freeze) ────────────────────────────────────────


def test_atomic_sentinel_constant_is_the_spec_string() -> None:
    """from: FROZEN-INTERFACE ("ATOMIC_SENTINEL = 'atomic — no split (this scope is one
    indivisible unit)'").

    The sentinel is the exact frozen string the dry-run / proposal renders for an atomic scope.
    GREEN from freeze.
    """
    assert ATOMIC_SENTINEL == "atomic — no split (this scope is one indivisible unit)"


def test_micro_gate_header_constant_is_the_spec_string() -> None:
    """from: FROZEN-INTERFACE ("MICRO_GATE_HEADER = 'SCOPE-SPLIT PROPOSAL — Stage 0.5
    micro-gate'"). GREEN from freeze.
    """
    assert MICRO_GATE_HEADER == "SCOPE-SPLIT PROPOSAL — Stage 0.5 micro-gate"


# ── format_split_proposal rendering (RED until filled) ────────────────────────


def test_atomic_proposal_renders_the_sentinel() -> None:
    """from: SYNTHESIZED-PLAN §5 ("formatter … sentinel") + §4 step 6 (atomic sentinel branch).

    An atomic result renders the ATOMIC_SENTINEL string. (RED-until-filled.)
    """
    result = SplitResult(atomic=True, reason="not separable by manifest")
    rendered = format_split_proposal(result, origin_scope="PR-7")
    assert ATOMIC_SENTINEL in rendered


def test_split_proposal_renders_n_blocks_each_with_origin_scope() -> None:
    """from: SYNTHESIZED-PLAN §5 ("N renders N; origin_scope") + §4 step 6 (per-sub-scope block)
    + plan §3 (provenance #8: origin_scope on every sub-scope).

    A 3-sub-scope result renders all three sub-scope ids and the origin scope (provenance).
    (RED-until-filled.)
    """
    result = _split_result("PR-7", 3)
    rendered = format_split_proposal(result, origin_scope="PR-7")
    for i in range(1, 4):
        assert f"PR-7-s{i}" in rendered
    # origin provenance is visible in the rendered proposal
    assert "PR-7" in rendered


def test_split_proposal_has_no_hard_coded_magnitude() -> None:
    """from: SYNTHESIZED-PLAN §5 ("no hard-coded magnitudes") + §4 step 6.

    The proposal renders the result's OWN sub-scope count, not a literal baked into the
    formatter: a 2-sub-scope and a 4-sub-scope result render different numbers of sub-scope ids.
    Guards against a formatter that hard-codes "3 sub-scopes". (RED-until-filled.)
    """
    two = format_split_proposal(_split_result("PR-A", 2), origin_scope="PR-A")
    four = format_split_proposal(_split_result("PR-B", 4), origin_scope="PR-B")
    assert two.count("PR-A-s") == 2
    assert four.count("PR-B-s") == 4

"""Integration smoke for ``genome.campaign`` — the propose_split→campaign seam end to end.

Spec source: SYNTHESIZED-PLAN §5 (``test_campaign_integration_smoke.py`` — 'a multi-cluster
scope → ordered sub-scopes → a campaign that would run them in order'). A composition test over the
already-unit-tested reducers: it exercises the REAL ``propose_split``
output (via the no-scan ``static`` engine) flowing into ``seed_campaign`` and the deps-gated
sequencing, plus an explicit three-sub-scope ordered progression.

Nothing is launched: the whole progression is pure state over the append-only ledger — there is no
``/scope-run`` invocation in PR 1 (the live launch is PR 2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genome.campaign.model import CampaignStatus
from genome.campaign.state_machine import (
    advance_on_merge,
    next_ready,
    reduce_current,
    seed_campaign,
    tee_up,
    transition,
)
from genome.scope_split.graph import make_coupling_builder
from genome.scope_split.model import ScopeManifestInput, SplitResult, SubScope
from genome.scope_split.splitter import propose_split

if TYPE_CHECKING:
    from genome.campaign.model import SubScopeState


def _two_cluster_manifest() -> ScopeManifestInput:
    """A manifest the static engine splits into two ordered sub-scopes (schema before cli)."""
    return ScopeManifestInput(
        scope_id="PR-CLI",
        change_class=("schema", "cli"),
        imports_touched=(
            "ddl/group_x.sql",
            "ddl/group_y.sql",
            "genome/x/cli.py",
            "genome/x/cli_commands.py",
        ),
    )


def _linear_split(origin: str, n: int) -> SplitResult:
    """A non-atomic SplitResult of ``n`` sub-scopes in a linear dependency chain (s1←s2←…←sN)."""
    subs = tuple(
        SubScope(
            sub_scope_id=f"{origin}-s{i}",
            origin_scope=origin,
            change_class=("cli",),
            est_imports_touched=2,
            applicable_anchors=(),
            est_risk_tier=1,
            depends_on=() if i == 1 else (f"{origin}-s{i - 1}",),
            rationale=f"cluster {i}",
        )
        for i in range(1, n + 1)
    )
    return SplitResult(
        atomic=False,
        reason="clean cut",
        sub_scopes=subs,
        order=tuple(s.sub_scope_id for s in subs),
        cut_quality=None,
    )


def _run_campaign_to_completion(history: list[SubScopeState], campaign_id: str) -> list[str]:
    """Drive the campaign loop, returning the order sub-scopes became next-ready (deps-gated).

    Mirrors the design §2 loop without any live launch: tee up, pick the next ready, drive it
    through its two (external) human gates to merged, advance, repeat — purely over the ledger.
    """
    order: list[str] = []
    history += tee_up(history)
    while (nxt := next_ready(reduce_current(history, campaign_id=campaign_id))) is not None:
        order.append(nxt.sub_scope_id)
        history.append(transition(history, nxt.sub_scope_id, CampaignStatus.PLANNING))
        history.append(
            transition(history, nxt.sub_scope_id, CampaignStatus.IMPLEMENTING, external_event=True),
        )
        history += advance_on_merge(history, nxt.sub_scope_id)
    return order


def test_real_propose_split_output_seeds_and_runs_deps_gated() -> None:
    """from: §5 (the propose_split→seed_campaign seam on REAL splitter output, deps-gated)."""
    manifest = _two_cluster_manifest()
    result = propose_split(manifest, make_coupling_builder("static"))
    assert not result.atomic
    assert len(result.sub_scopes) == 2  # schema, cli — and cli depends on schema (topo)

    history = list(seed_campaign(result, manifest.scope_id))
    assert all(r.status is CampaignStatus.PENDING for r in history)

    order = _run_campaign_to_completion(history, manifest.scope_id)
    assert order == ["PR-CLI-s1", "PR-CLI-s2"]  # s2 only ran once s1 merged (deps-gated)
    assert reduce_current(history, campaign_id=manifest.scope_id).is_done()


def test_three_sub_scope_campaign_runs_in_dependency_order() -> None:
    """from: §5 ('a campaign that would run them in order' — three ordered, deps-gated)."""
    history = list(seed_campaign(_linear_split("PR-X", 3), "PR-X"))
    assert len(history) == 3
    assert all(r.status is CampaignStatus.PENDING for r in history)

    order = _run_campaign_to_completion(history, "PR-X")
    assert order == ["PR-X-s1", "PR-X-s2", "PR-X-s3"]
    assert reduce_current(history, campaign_id="PR-X").is_done()


def test_a_dependent_is_gated_until_its_dependency_resolves() -> None:
    """from: §5 (deps-gating — s2 is not ready until s1 is merged/moot)."""
    history = list(seed_campaign(_linear_split("PR-X", 3), "PR-X"))
    history += tee_up(history)
    state = reduce_current(history, campaign_id="PR-X")
    nxt = next_ready(state)
    assert nxt is not None
    assert nxt.sub_scope_id == "PR-X-s1"  # only the deps-free head is ready
    s2 = state.by_id("PR-X-s2")
    assert s2 is not None
    assert s2.status is CampaignStatus.PENDING  # gated behind s1

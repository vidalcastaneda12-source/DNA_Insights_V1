"""Formatter + ROADMAP-reflection tests for ``genome.campaign``.

Spec source: SYNTHESIZED-PLAN §4 (``formatter.py``), §5 (``test_campaign_roadmap_reflection.py``),
and Gate-1 refinement B (eject fails loud — visible in ``format_campaign_status``). Plan-blind:
written from the contract.

Covers: ``format_campaign_status`` shows each sub-scope's live status + deps; an ejected sub-scope
is visible WITH its escalation note (refinement B); ``format_campaign_roadmap_block`` carries live
statuses (not bare slots) and reuses the existing ``append_roadmap_block`` (the campaign owns only
its block renderer, not a second writer / region); the reflection writes only inside the
B2-SUBSCOPES sentinels, is byte-idempotent, and is clobber-guarded.
"""

from __future__ import annotations

import pytest

from genome.campaign.formatter import format_campaign_roadmap_block, format_campaign_status
from genome.campaign.model import CampaignStatus, RevalidationDecision
from genome.campaign.state_machine import (
    advance_on_merge,
    apply_revalidation,
    reduce_current,
    seed_campaign,
    tee_up,
    transition,
)
from genome.scope_split.model import SplitResult, SubScope
from genome.scope_split.roadmap_writer import BLOCK_BEGIN, BLOCK_END, append_roadmap_block

_ROADMAP = (
    f"# ROADMAP\n\nhand-authored intro\n\n{BLOCK_BEGIN}\n{BLOCK_END}\n\nhand-authored outro\n"
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


def test_status_shows_each_sub_scope_status_and_dependencies() -> None:
    """from: §4 step 4 (format_campaign_status — id, status, deps, origin_scope)."""
    history = list(seed_campaign(_linear_split("PR-X", 2), "PR-X"))
    out = format_campaign_status(reduce_current(history, campaign_id="PR-X"))
    assert "PR-X" in out  # the campaign id header
    assert "PR-X-s1" in out
    assert "pending" in out
    assert "depends_on: PR-X-s1" in out  # s2's dependency is surfaced


def test_ejection_is_visible_in_status_output_with_its_note() -> None:
    """from: refinement B (eject fails loud — status + note, not a silent drop).

    A sub-scope ejected on the re-split path surfaces both its ``ejected`` status AND its
    human-readable escalation note in ``format_campaign_status``.
    """
    history = list(seed_campaign(_linear_split("PR-X", 1), "PR-X"))
    history += tee_up(history)  # s1 ready
    history += apply_revalidation(
        history, "PR-X-s1", RevalidationDecision.GROWN
    )  # no children → eject
    out = format_campaign_status(reduce_current(history, campaign_id="PR-X"))
    assert "ejected" in out
    assert "escalat" in out.lower()  # the escalation reason is visible, not silently dropped


def test_roadmap_block_carries_live_statuses_not_bare_slots() -> None:
    """from: §5 ('the rendered body carries LIVE statuses (not bare slots)')."""
    history = list(seed_campaign(_linear_split("PR-X", 2), "PR-X"))
    history.append(transition(history, "PR-X-s1", CampaignStatus.READY))
    history.append(transition(history, "PR-X-s1", CampaignStatus.PLANNING))
    history.append(transition(history, "PR-X-s1", CampaignStatus.IMPLEMENTING, external_event=True))
    history += advance_on_merge(history, "PR-X-s1")  # s1 merged, s2 readied

    block = format_campaign_roadmap_block(reduce_current(history, campaign_id="PR-X"))
    assert "- [x] **PR-X-s1** — merged" in block  # merged → checked
    assert "- [ ] **PR-X-s2** — ready" in block  # in-flight → unchecked
    assert "origin_scope: PR-X" in block  # provenance #8 carried per slot


def test_reflection_writes_only_inside_the_managed_region() -> None:
    """from: §5 ('reuses append_roadmap_block; every byte outside the sentinels identical')."""
    history = list(seed_campaign(_linear_split("PR-X", 2), "PR-X"))
    block = format_campaign_roadmap_block(reduce_current(history, campaign_id="PR-X"))

    updated = append_roadmap_block(_ROADMAP, block, origin_scope="PR-X")

    assert updated.startswith(f"# ROADMAP\n\nhand-authored intro\n\n{BLOCK_BEGIN}")
    assert updated.endswith(f"{BLOCK_END}\n\nhand-authored outro\n")
    assert block in updated


def test_reflection_is_byte_idempotent() -> None:
    """from: §5 ('idempotent — reflecting the same state twice is byte-identical')."""
    history = list(seed_campaign(_linear_split("PR-X", 2), "PR-X"))
    block = format_campaign_roadmap_block(reduce_current(history, campaign_id="PR-X"))
    once = append_roadmap_block(_ROADMAP, block, origin_scope="PR-X")
    twice = append_roadmap_block(once, block, origin_scope="PR-X")
    assert once == twice


def test_reflection_is_clobber_guarded() -> None:
    """from: §5 ('clobber-guarded — a ROADMAP missing the B2-SUBSCOPES sentinels raises')."""
    history = list(seed_campaign(_linear_split("PR-X", 1), "PR-X"))
    block = format_campaign_roadmap_block(reduce_current(history, campaign_id="PR-X"))
    with pytest.raises(ValueError, match="sentinel"):
        append_roadmap_block("# ROADMAP\n\nno managed block here\n", block, origin_scope="PR-X")


def test_empty_campaign_renders_no_roadmap_block() -> None:
    """from: §4 step 4 (an empty / atomic campaign → the empty block, nothing to write)."""
    empty = reduce_current([], campaign_id="PR-X")
    assert format_campaign_roadmap_block(empty) == ""

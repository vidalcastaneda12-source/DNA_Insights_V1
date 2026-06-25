"""Triage formatter — ``format_triage_plan`` / ``format_eject_draft`` render snapshot.

Plan-blind spec source: synthesized-plan §4 ("``formatter.py`` — human-readable triage block;
NO anchor magnitudes hard-coded in source"), §5 test list item 3 ("no anchor magnitude
hard-coded; discards rendered"), OQ-3 (eject drafts-to-stdout for human paste), and the FROZEN
INTERFACE CONTRACT (``NOTHING_DRAINABLE_SENTINEL`` literal; ``format_triage_plan(plan) -> str``;
``format_eject_draft(triaged) -> str``; "(No anchor magnitudes in source.)").

The render tests assert that the counts/reasons placed INTO the plan appear in the formatter's
output — the specified behaviour (every number in the output comes from the plan), never a
value reverse-engineered from the stubbed bodies (``raise NotImplementedError`` now — RED is
correct for the render tests). The source-discipline test is a static-source guard (does not
call the stub) so it is GREEN-eligible the moment the module exists.
"""

from __future__ import annotations

import inspect
import re

from genome.fast_follow import formatter
from genome.fast_follow.formatter import (
    NOTHING_DRAINABLE_SENTINEL,
    format_eject_draft,
    format_triage_plan,
)
from genome.fast_follow.model import (
    Classification,
    Triage,
    TriagePlan,
)


def _drain_triage(cid: str) -> Triage:
    return Triage(
        candidate_id=cid,
        classification=Classification.DRAIN,
        retier=None,
        reason="tier-0 docs nit, no anchors, small diff",
        drains="roadmap-backlog-item-x",
    )


def _eject_triage(cid: str) -> Triage:
    return Triage(
        candidate_id=cid,
        classification=Classification.EJECT,
        retier="tier-1",
        reason="touches docs/schemas/** — schema is immutable",
        drains=None,
    )


def _discard_triage(cid: str) -> Triage:
    return Triage(
        candidate_id=cid,
        classification=Classification.DISCARD,
        retier=None,
        reason="already handled in a prior run",
        drains=None,
    )


def _mixed_plan() -> TriagePlan:
    return TriagePlan(
        triaged=(_drain_triage("d1"), _drain_triage("d2"), _eject_triage("e1")),
        overflow=(),
        discards=(_discard_triage("s1"),),
        dry=True,
        termination=None,
    )


def _all_ejected_plan() -> TriagePlan:
    return TriagePlan(
        triaged=(_eject_triage("e1"),),
        overflow=(),
        discards=(_discard_triage("s1"),),
        dry=True,
        termination="dry",
    )


# ── format_triage_plan: renders drain/eject/discard counts + reasons ──────────


def test_format_triage_plan_renders_counts() -> None:
    """from: plan §5 item 3 (drain/eject/discard counts) + §4.

    The rendered block surfaces the drain / eject / discard tallies the plan reports — the
    operator's triage-approval surface (touchpoint 1).
    """
    rendered = format_triage_plan(_mixed_plan())
    lowered = rendered.lower()
    assert "drain" in lowered
    assert "eject" in lowered
    assert "discard" in lowered


def test_format_triage_plan_renders_reasons() -> None:
    """from: plan §5 item 3 (reasons rendered) + §4 (per-item reason).

    Each triage carries a reason; the rendered block surfaces them so the human approval is
    informed (not a bare count). The reason text placed on the plan appears in the output.
    """
    rendered = format_triage_plan(_mixed_plan())
    assert "schema is immutable" in rendered


def test_format_triage_plan_renders_discards() -> None:
    """from: plan §5 item 3 ("discards rendered") + frozen ``TriagePlan.discards``.

    The discard channel is rendered (visible, not silently swallowed) — its reason text
    appears in the block.
    """
    rendered = format_triage_plan(_mixed_plan())
    assert "already handled in a prior run" in rendered


# ── All-eject/discard plan renders the NOTHING_DRAINABLE sentinel ─────────────


def test_all_eject_discard_plan_renders_nothing_drainable_sentinel() -> None:
    """from: plan §4 (dry / nothing-drainable) + frozen ``NOTHING_DRAINABLE_SENTINEL``.

    A plan with no DRAIN items renders the literal sentinel string instead of an empty drain
    block — the explicit "nothing to do this batch" signal that the dry/loop_done predicate
    surfaces to the operator.
    """
    rendered = format_triage_plan(_all_ejected_plan())
    assert NOTHING_DRAINABLE_SENTINEL in rendered
    assert NOTHING_DRAINABLE_SENTINEL == (
        "Nothing drainable this batch — every candidate ejected or discarded"
    )


# ── format_eject_draft: a ROADMAP-style draft string ──────────────────────────


def test_format_eject_draft_produces_a_roadmap_style_draft() -> None:
    """from: plan §4 / OQ-3 (eject-draft produces a ROADMAP draft for human paste) + frozen
    ``format_eject_draft``.

    Given ejected triages, ``format_eject_draft`` returns a non-empty draft string carrying the
    ejected candidate(s) — the stdout content the human pastes into ROADMAP. (It returns a
    STRING; it never writes a file — that discipline is asserted in the CLI test.)
    """
    draft = format_eject_draft((_eject_triage("e1"),))
    assert isinstance(draft, str)
    assert draft.strip() != ""
    assert "e1" in draft


# ── Source discipline: no comma-grouped magnitude baked into formatter source ─


def test_formatter_source_contains_no_comma_grouped_magnitude() -> None:
    """from: plan §4 ("NO anchor magnitudes hard-coded in source") + frozen contract ("(No
    anchor magnitudes in source.)").

    Read the formatter module's OWN source and assert it carries no comma-grouped magnitude
    number (the anchor shape ``\\d{1,3}(?:,\\d{3})+``) — every number in the output must
    originate from the plan at runtime, never be transcribed into the formatter code. Static
    guard; does not call the stub, so GREEN-eligible immediately.
    """
    source = inspect.getsource(formatter)
    anchor_shape = re.compile(r"\d{1,3}(?:,\d{3})+")
    found = anchor_shape.findall(source)
    assert found == [], f"formatter source contains comma-grouped magnitudes: {found}"


def test_format_triage_plan_renders_overflow_items() -> None:
    """from: review ptest-7 — a non-empty overflow partition is rendered (never silently hidden)."""
    plan = TriagePlan(
        triaged=(_drain_triage("d1"),),
        overflow=(_drain_triage("ov1"),),
        discards=(),
        dry=False,
        termination=None,
    )
    rendered = format_triage_plan(plan)
    assert "ov1" in rendered


def test_counts_includes_overflow_items() -> None:
    """from: review sweep-2 — counts() spans overflow too, so the headline never under-reports."""
    plan = TriagePlan(
        triaged=(_drain_triage("d1"),),
        overflow=(_drain_triage("ov1"), _drain_triage("ov2")),
        discards=(_discard_triage("s1"),),
        dry=False,
        termination=None,
    )
    counts = plan.counts()
    assert counts["drain"] == 3  # 1 triaged + 2 overflow
    assert counts["discard"] == 1

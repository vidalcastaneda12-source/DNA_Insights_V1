"""Render a scope-split proposal as human-readable text (``finding-039``; plan §4 / §6).

``format_split_proposal`` turns a :class:`~genome.scope_split.model.SplitResult` into the
plain-text block the operator reads at the Stage-0.5 micro-gate — the atomic sentinel, or the
per-sub-scope mini-manifests with the cut-quality summary. ``format_roadmap_block`` renders the
split as the ROADMAP-managed-block body :mod:`genome.scope_split.roadmap_writer` splices between
its sentinels.

**No** :mod:`genome.db` import. **No anchor magnitudes hard-coded in this module's source**
(plan §6): every number in the output originates from the result at runtime.

This file is a **stub** for the interface-freeze step: every render body raises
:class:`NotImplementedError` so plan-blind tests are honestly RED. The two literal constants are
real (the sentinel / header the doc-consistency + dry-run tests key on).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genome.scope_split.model import SplitResult

#: Emitted in place of the sub-scope section when the scope is atomic (indivisible). A literal
#: sentinel, never a number — the ``dry-run`` smoke and the doc-consistency test key on it.
ATOMIC_SENTINEL: str = "atomic — no split (this scope is one indivisible unit)"

#: The header line for a non-atomic split proposal at the Stage-0.5 micro-gate.
MICRO_GATE_HEADER: str = "SCOPE-SPLIT PROPOSAL — Stage 0.5 micro-gate"


def format_split_proposal(result: SplitResult, *, origin_scope: str) -> str:
    """Render the split proposal as the raw text block the operator reviews (plan §4).

    Atomic → a block led by :data:`ATOMIC_SENTINEL` with the reason. Split → a
    :data:`MICRO_GATE_HEADER` block with one numbered section per sub-scope (id, origin_scope,
    change classes, estimated footprint, re-scored tier, rationale) plus the cut-quality summary.
    Contains no hard-coded anchor magnitude — every number originates from ``result`` at call time.
    """
    if result.atomic:
        return f"{ATOMIC_SENTINEL}\n  origin_scope: {origin_scope}\n  reason: {result.reason}"

    lines: list[str] = [
        MICRO_GATE_HEADER,
        f"  origin_scope: {origin_scope}",
        f"  {result.reason}",
        "",
    ]
    for position, sub in enumerate(result.sub_scopes, start=1):
        depends = ", ".join(sub.depends_on) if sub.depends_on else "(none)"
        lines.extend(
            [
                f"{position}. {sub.sub_scope_id}  (origin_scope: {sub.origin_scope})",
                f"     change_class: {', '.join(sub.change_class)}",
                f"     est_imports_touched: {sub.est_imports_touched}",
                f"     est_risk_tier: {sub.est_risk_tier}",
                f"     depends_on: {depends}",
                f"     rationale: {sub.rationale}",
            ],
        )
    quality = result.cut_quality
    if quality is not None:
        lines.extend(
            [
                "",
                (
                    f"cut_quality: cut_cost={quality.cut_cost} "
                    f"max_tier_before={quality.max_tier_before} "
                    f"max_tier_after={quality.max_tier_after} "
                    f"min_subscope_shrink={quality.min_subscope_shrink} "
                    f"clean={quality.clean}"
                ),
            ],
        )
    return "\n".join(lines)


def format_roadmap_block(
    result: SplitResult,
    *,
    origin_scope: str,  # noqa: ARG001 - frozen signature; provenance is carried per-sub-scope
) -> str:
    """Render the split as the ROADMAP managed-block body (plan §4).

    The string :mod:`genome.scope_split.roadmap_writer` splices between its
    ``<!-- B2-SUBSCOPES:BEGIN -->`` / ``:END`` sentinels: an append-only block of one ``- [ ]``
    slot per sub-scope in topo order, each recording its ``origin_scope`` (provenance,
    locked decision #8 — taken from the sub-scope, which always equals ``origin_scope``). Atomic →
    the empty string (nothing to write).
    """
    if result.atomic:
        return ""
    return "\n".join(
        f"- [ ] **{sub.sub_scope_id}** — {sub.rationale} (origin_scope: {sub.origin_scope})"
        for sub in result.sub_scopes
    )

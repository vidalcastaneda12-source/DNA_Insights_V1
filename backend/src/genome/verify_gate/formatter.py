"""Render an evidence package as the raw, human-readable evidence block (plan §4.5).

``format_evidence`` turns an :class:`~genome.verify_gate.model.EvidencePackage` into the
plain-text block the operator reads before typing an approval token — the verification
steps with their exit-code statuses, the anchor comparisons (expected vs captured), the
integrity signals, and the reduced verdict. When no real-data anchors apply to the change
class it emits the literal N/A sentinel rather than any fabricated number.

**No** :mod:`genome.db` import. **No anchor digits in this module's source** — every number
in the output comes from the package at runtime; the formatter source itself must contain no
comma-grouped magnitude (the ``test_verify_gate_formatter`` guard asserts
``anchor_numbers(source) == frozenset()``), so the real-data anchors are never transcribed
into code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genome.verify_gate.verdict import reduce_verdict

if TYPE_CHECKING:
    from genome.verify_gate.model import EvidencePackage

#: Emitted in place of the anchor section when the change class has no applicable real-data
#: anchors (this PR's own run — plan §2 / §6 "N/A self-gate"). A literal sentinel, never a
#: number.
NO_ANCHORS_SENTINEL: str = "N/A — no real-data anchors apply to this change-class"


def _yes_no(*, flag: bool) -> str:
    return "yes" if flag else "no"


def format_evidence(pkg: EvidencePackage) -> str:
    """Render the evidence package as the raw text block the operator reviews (plan §4.5).

    Includes every verification step with its status, each anchor's expected-vs-captured
    comparison (or :data:`NO_ANCHORS_SENTINEL` when the anchor set is empty), the integrity
    flags, and the reduced verdict. Contains no hard-coded anchor magnitude — all numbers
    originate from ``pkg`` at call time.
    """
    verdict = reduce_verdict(pkg)
    lines: list[str] = []

    # Verdict headline.
    lines.append(f"VERDICT: {verdict.value.upper()}")
    lines.append(f"change_class: {', '.join(sorted(pkg.change_class)) or '(none)'}")
    lines.append("")

    # Verification steps — the verify.sh tail with each step's exit-code status.
    lines.append("Verification steps:")
    if pkg.steps:
        for name, status in pkg.steps:
            lines.append(f"  - {name}: {status.value.upper()}")
    else:
        lines.append("  (no steps recorded)")
    lines.append("")

    # Real-data anchors — expected vs captured, or the N/A sentinel.
    lines.append("Real-data anchors:")
    if pkg.anchors:
        for anchor in pkg.anchors:
            expected = "-" if anchor.expected is None else anchor.expected
            actual = "-" if anchor.actual is None else anchor.actual
            suffix = " [deferred]" if anchor.deferred else ""
            lines.append(f"  - {anchor.name}: expected={expected} actual={actual}{suffix}")
    else:
        lines.append(f"  {NO_ANCHORS_SENTINEL}")
    lines.append("")

    # Integrity signals.
    integrity = pkg.integrity
    before = "-" if integrity.test_count_before is None else str(integrity.test_count_before)
    after = "-" if integrity.test_count_after is None else str(integrity.test_count_after)
    lines.append("Integrity:")
    lines.append(f"  - test count: before={before} after={after}")
    lines.append(f"  - changelog present: {_yes_no(flag=integrity.changelog_present)}")
    lines.append(f"  - docs check clean: {_yes_no(flag=integrity.docs_check_clean)}")
    lines.append(f"  - weakened/removed test: {_yes_no(flag=integrity.weakened_or_removed_test)}")
    lines.append(f"  - gate-fill survivor: {_yes_no(flag=integrity.gate_fill_survivor)}")
    lines.append(f"  - schema rebuild pending: {_yes_no(flag=pkg.rebuild_pending)}")

    return "\n".join(lines)

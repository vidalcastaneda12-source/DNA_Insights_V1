"""Evidence formatter — ``format_evidence`` render snapshot (Sub Project A).

Plan-blind spec source: synthesized-plan §4.5 (the formatter renders the steps + anchor
comparisons + integrity + verdict as a raw text block; emits the N/A sentinel when no anchors
apply), §5 test list item 4 ("snapshot w/ REAL column names (gnomad_matches/row_count/
consensus_total) + REAL verify.sh 6-step tail (finding-013); N/A sentinel literal;
no-anchor-digit-in-formatter-source (anchor_numbers(source)==∅)"), §6 ("N/A self-gate"), and
the FROZEN INTERFACE CONTRACT (``format_evidence(pkg)->str``; the ``NO_ANCHORS_SENTINEL``
literal).

finding-013 fixture realism: the verify.sh tail uses the REAL 6 step labels from
``scripts/verify.sh`` and the anchors use the REAL verification.md capture column names —
only the pass/fail statuses and the magnitude VALUES are synthesized. The render tests assert
that the names/labels placed INTO the package appear in the formatter's output (the specified
behaviour: every number in the output comes from the package), never a value reverse-engineered
from the stubbed body (``format_evidence`` is ``NotImplementedError`` → RED is correct for the
render tests).

Pre-mortem coupling note (premortem-digest skeptic-2 #6, named): the verify.sh-tail snapshot
deliberately couples to verify.sh's exact 6 step labels; if those labels change, this fixture
updates with them — the coupling is intentional, not accidental.
"""

from __future__ import annotations

import inspect
import re

from genome.docs.validator import anchor_numbers
from genome.verify_gate import formatter
from genome.verify_gate.formatter import NO_ANCHORS_SENTINEL, format_evidence
from genome.verify_gate.model import (
    AnchorCheck,
    EvidencePackage,
    IntegrityFlags,
    StepStatus,
)

# The REAL six ``scripts/verify.sh`` step labels (finding-013 realism — only the
# pass/fail status per step is synthesized).
_VERIFY_SH_TAIL: tuple[tuple[str, StepStatus], ...] = (
    ("uv sync", StepStatus.PASS),
    ("pytest", StepStatus.PASS),
    ("ruff check", StepStatus.PASS),
    ("ruff format --check", StepStatus.PASS),
    ("mypy --strict backend/src", StepStatus.PASS),
    ("genome docs check", StepStatus.PASS),
)


def _affirmative_integrity() -> IntegrityFlags:
    return IntegrityFlags(
        changelog_present=True,
        docs_check_clean=True,
        weakened_or_removed_test=False,
        gate_fill_survivor=False,
        test_count_before=400,
        test_count_after=406,
    )


def _package_with_anchors() -> EvidencePackage:
    """A package with the REAL verify.sh tail + REAL-column-named anchors (synthesized values)."""
    return EvidencePackage(
        change_class=frozenset({"annotation", "pipeline"}),
        steps=_VERIFY_SH_TAIL,
        anchors=(
            AnchorCheck(name="gnomad_matches", expected="3054426", actual="3054426"),
            AnchorCheck(name="row_count", expected="3077001", actual="3077001"),
            AnchorCheck(name="consensus_total", expected="3160364", actual="3160364"),
        ),
        integrity=_affirmative_integrity(),
        rebuild_pending=False,
    )


def _package_without_anchors() -> EvidencePackage:
    """THIS PR's own self-gate shape: the N/A path (empty anchor tuple)."""
    return EvidencePackage(
        change_class=frozenset({"core"}),
        steps=_VERIFY_SH_TAIL,
        anchors=(),
        integrity=_affirmative_integrity(),
        rebuild_pending=False,
    )


# ── WITH anchors: real column names + real 6-step verify.sh tail ─────────────


def test_format_renders_real_anchor_column_names() -> None:
    """from: plan §5 item 4 (anchor table w/ REAL column names) + §4.5.

    The rendered block must include each anchor's real capture-column name — the formatter
    renders what is in the package, so the names placed in must appear in the output.
    """
    rendered = format_evidence(_package_with_anchors())
    assert "gnomad_matches" in rendered
    assert "row_count" in rendered
    assert "consensus_total" in rendered


def test_format_renders_the_real_verify_sh_six_step_tail() -> None:
    """from: plan §5 item 4 (REAL verify.sh 6-step tail, finding-013) + §4.5.

    Every one of the six real ``scripts/verify.sh`` step labels must appear in the rendered
    block (the verification-steps section). The coupling to the exact labels is intentional.
    """
    rendered = format_evidence(_package_with_anchors())
    for label, _status in _VERIFY_SH_TAIL:
        assert label in rendered, f"missing verify.sh step label: {label!r}"


def test_format_renders_anchor_expected_and_actual() -> None:
    """from: plan §4.5 (each anchor's expected-vs-captured comparison).

    A matching anchor renders both its expected and captured magnitudes (which originate from
    the package, never hard-coded in the formatter). The synthesized value here is what was
    placed into the package.
    """
    rendered = format_evidence(_package_with_anchors())
    # The value lives on the package; the formatter surfaces it in the comparison.
    assert "3054426" in rendered


# ── WITHOUT anchors: the literal N/A sentinel ────────────────────────────────


def test_format_emits_na_sentinel_when_no_anchors() -> None:
    """from: plan §5 item 4 (N/A sentinel literal) + §6 (N/A self-gate) + frozen
    ``NO_ANCHORS_SENTINEL``.

    The N/A path renders the literal sentinel string instead of any fabricated number — this
    is exactly what THIS PR's own verify-gate run emits (no real-data anchor asserted).
    """
    rendered = format_evidence(_package_without_anchors())
    assert NO_ANCHORS_SENTINEL in rendered
    assert NO_ANCHORS_SENTINEL == "N/A — no real-data anchors apply to this change-class"


# ── The formatter SOURCE carries no comma-grouped anchor magnitude ────────────


def test_formatter_source_contains_no_anchor_digit() -> None:
    """from: plan §5 item 4 (no-anchor-digit-in-formatter-source) + the formatter module
    docstring invariant ("No anchor digits in this module's source").

    Read the formatter module's OWN source and assert the existing
    ``genome.docs.validator.anchor_numbers`` finds no comma-grouped magnitude in it — every
    number in the output must originate from the package at runtime, never be transcribed into
    the formatter code. (Static-source guard; does not call the stub, so it is GREEN-eligible
    immediately.)

    TEST-AUTHOR NOTE (§5 ambiguity flagged for test-integrity): ``anchor_numbers`` scopes its
    scan to a ``## Real-data observations`` heading (it returns ``frozenset()`` for any text
    lacking that heading), so the §5-named assertion below is satisfied trivially by a source
    that has no such heading — it does NOT by itself prove the absence of a comma-grouped
    digit. The §5-literal assertion is kept verbatim for provenance; a second assertion
    realizes §5's STATED intent ("never bake a comma-grouped anchor digit into its source") by
    applying the anchor-shape regex to the FULL source. Neither reads the implementation logic.
    """
    source = inspect.getsource(formatter)
    # §5-literal assertion (kept verbatim for test→spec provenance).
    assert anchor_numbers(source) == frozenset()
    # §5-intent assertion: the formatter source carries no comma-grouped magnitude anywhere
    # (the anchor shape is ``\d{1,3}(?:,\d{3})+`` — the same shape the validator guards).
    anchor_shape = re.compile(r"\d{1,3}(?:,\d{3})+")
    found = anchor_shape.findall(source)
    assert found == [], f"formatter source contains comma-grouped anchor magnitudes: {found}"

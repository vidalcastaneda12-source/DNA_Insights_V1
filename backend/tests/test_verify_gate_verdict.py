"""Verdict reduction truth table — ``reduce_verdict`` + ``parse_step`` (Sub Project A core).

Plan-blind spec source: synthesized-plan §4.2 (``parse_step`` exit-code mapping; fail-closed
``IntegrityFlags`` defaults), §4.3 (the ``reduce_verdict`` truth table: ``UNKNOWN`` dominates
``BLOCKED`` dominates ``GREEN``; the GREEN-iff clause; the per-flip BLOCKED/UNKNOWN verdicts;
empty-anchor N/A path), §5 test list item 2 (fully-affirmative→GREEN; negative-control
single-flip sweep; UNKNOWN-distinct-from-BLOCKED; UNKNOWN-dominates; step-parser exit-codes;
empty-anchors→GREEN; DB-absent anchor→UNKNOWN; negative-delta→BLOCKED; count None→UNKNOWN),
and the FROZEN INTERFACE CONTRACT (the dataclass field names / enum members / signatures).

These assert the SPECIFIED reduction, never the stubbed body (``reduce_verdict`` and
``parse_step`` both ``raise NotImplementedError`` right now — RED is correct, and RED on
NotImplementedError, not ImportError). Each expected verdict comes from §4.3, which pins the
exact value for every flip; nothing is reverse-engineered from the code.

Pre-mortem coverage: this file is the negative-control core that pins the predicted surprise
"a false GREEN" (premortem-digest skeptic-2 #riskiest-2 — "the fail-closed guarantee must
live in the unit-tested core"). The single-flip sweep is the guard test proving no single
non-affirmative signal slips through to GREEN.
"""

from __future__ import annotations

import dataclasses

import pytest

from genome.verify_gate.model import (
    AnchorCheck,
    EvidencePackage,
    IntegrityFlags,
    StepStatus,
    Verdict,
    parse_step,
)
from genome.verify_gate.verdict import reduce_verdict

# ── Fixtures: a fully-affirmative package (every signal at its GREEN value) ───


def _affirmative_integrity() -> IntegrityFlags:
    """Integrity flags with every signal at its GREEN-eligible (affirmative) value.

    The frozen defaults are fail-closed (the opposite of these), so every field is set
    explicitly — this is the only ``IntegrityFlags`` value that is GREEN-eligible.
    """
    return IntegrityFlags(
        changelog_present=True,
        docs_check_clean=True,
        weakened_or_removed_test=False,
        gate_fill_survivor=False,
        test_count_before=400,
        test_count_after=406,
    )


def _affirmative_package() -> EvidencePackage:
    """A package that should reduce to GREEN: all steps PASS, one matching anchor, all
    integrity affirmative, rebuild not pending."""
    return EvidencePackage(
        change_class=frozenset({"annotation"}),
        steps=(
            ("pytest", StepStatus.PASS),
            ("ruff check", StepStatus.PASS),
            ("mypy --strict backend/src", StepStatus.PASS),
        ),
        anchors=(AnchorCheck(name="gnomad_matches", expected="3054426", actual="3054426"),),
        integrity=_affirmative_integrity(),
        rebuild_pending=False,
    )


def _with_integrity(**overrides: object) -> EvidencePackage:
    """The affirmative package with single ``IntegrityFlags`` fields overridden."""
    base = _affirmative_package()
    new_integrity = dataclasses.replace(base.integrity, **overrides)  # type: ignore[arg-type]
    return dataclasses.replace(base, integrity=new_integrity)


# ── Positive baseline: fully-affirmative → GREEN ─────────────────────────────


def test_fully_affirmative_package_is_green() -> None:
    """from: plan §4.3 GREEN-iff clause + §5 item 2 (fully-affirmative→GREEN).

    Every step PASS ∧ every non-deferred anchor matches ∧ changelog_present ∧
    docs_check_clean ∧ ¬weakened_or_removed_test ∧ ¬gate_fill_survivor ∧ count decided and
    non-decreasing ∧ ¬rebuild_pending → GREEN.
    """
    assert reduce_verdict(_affirmative_package()) is Verdict.GREEN


# ── Negative-control single-flip sweep ───────────────────────────────────────
# Start from the affirmative package; flip exactly ONE clause; assert NON-GREEN.
# Where §4.3 pins the specific verdict (BLOCKED vs UNKNOWN) we assert it exactly.


def test_flip_step_to_fail_blocks() -> None:
    """from: plan §4.3 ("Any step FAIL→BLOCKED") + §5 negative-control sweep."""
    pkg = _affirmative_package()
    flipped = dataclasses.replace(
        pkg,
        steps=(("pytest", StepStatus.FAIL), *pkg.steps[1:]),
    )
    verdict = reduce_verdict(flipped)
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.BLOCKED


def test_flip_step_to_unknown_is_unknown() -> None:
    """from: plan §4.3 ("any step UNKNOWN→UNKNOWN") + §5 negative-control sweep."""
    pkg = _affirmative_package()
    flipped = dataclasses.replace(
        pkg,
        steps=(("pytest", StepStatus.UNKNOWN), *pkg.steps[1:]),
    )
    verdict = reduce_verdict(flipped)
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.UNKNOWN


def test_flip_anchor_mismatch_blocks() -> None:
    """from: plan §4.3 ("anchor mismatch→BLOCKED") + §5 negative-control sweep.

    A non-deferred anchor whose captured ``actual`` differs from ``expected`` is a decided
    failure → BLOCKED (never a fabricated pass).
    """
    pkg = _affirmative_package()
    flipped = dataclasses.replace(
        pkg,
        anchors=(AnchorCheck(name="gnomad_matches", expected="3054426", actual="999999"),),
    )
    verdict = reduce_verdict(flipped)
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.BLOCKED


def test_flip_anchor_expected_none_is_unknown() -> None:
    """from: plan §4.3 ("anchor expected/actual None→UNKNOWN") + §5 negative-control sweep."""
    pkg = _affirmative_package()
    flipped = dataclasses.replace(
        pkg,
        anchors=(AnchorCheck(name="gnomad_matches", expected=None, actual="3054426"),),
    )
    verdict = reduce_verdict(flipped)
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.UNKNOWN


def test_flip_anchor_actual_none_is_unknown() -> None:
    """from: plan §4.3 ("anchor expected/actual None→UNKNOWN") + §5 item 2 (DB-absent
    anchor→UNKNOWN).

    A non-deferred anchor whose ``actual`` is None (DB absent / not captured) is undecidable
    → UNKNOWN, never fabricated. This is the DB-absent path the skill relies on.
    """
    pkg = _affirmative_package()
    flipped = dataclasses.replace(
        pkg,
        anchors=(AnchorCheck(name="gnomad_matches", expected="3054426", actual=None),),
    )
    verdict = reduce_verdict(flipped)
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.UNKNOWN


def test_flip_weakened_or_removed_test_blocks() -> None:
    """from: plan §4.3 ("weakened test→BLOCKED") + §5 negative-control sweep."""
    verdict = reduce_verdict(_with_integrity(weakened_or_removed_test=True))
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.BLOCKED


def test_flip_gate_fill_survivor_blocks() -> None:
    """from: plan §4.3 ("gate_fill→BLOCKED") + §5 negative-control sweep."""
    verdict = reduce_verdict(_with_integrity(gate_fill_survivor=True))
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.BLOCKED


def test_flip_changelog_absent_blocks() -> None:
    """from: plan §4.3 ("changelog missing→BLOCKED") + §5 negative-control sweep."""
    verdict = reduce_verdict(_with_integrity(changelog_present=False))
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.BLOCKED


def test_flip_docs_check_dirty_blocks() -> None:
    """from: plan §4.3 ("docs dirty→BLOCKED") + §5 negative-control sweep."""
    verdict = reduce_verdict(_with_integrity(docs_check_clean=False))
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.BLOCKED


def test_flip_test_count_after_less_than_before_blocks() -> None:
    """from: plan §4.3 ("after<before→BLOCKED") + §5 item 2 (negative-delta→BLOCKED).

    A net test loss is a decided integrity failure → BLOCKED.
    """
    verdict = reduce_verdict(_with_integrity(test_count_before=406, test_count_after=400))
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.BLOCKED


def test_flip_test_count_before_none_is_unknown() -> None:
    """from: plan §4.3 ("count None→UNKNOWN") + §5 item 2 (count None→UNKNOWN).

    An undecidable test count (None) cannot prove a non-negative delta → UNKNOWN.
    """
    verdict = reduce_verdict(_with_integrity(test_count_before=None))
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.UNKNOWN


def test_flip_test_count_after_none_is_unknown() -> None:
    """from: plan §4.3 ("count None→UNKNOWN") + §5 item 2 (count None→UNKNOWN).

    The ``after`` side being None is equally undecidable → UNKNOWN.
    """
    verdict = reduce_verdict(_with_integrity(test_count_after=None))
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.UNKNOWN


def test_flip_rebuild_pending_is_unknown() -> None:
    """from: plan §4.3 ("rebuild_pending→UNKNOWN") + §5 negative-control sweep.

    A schema rebuild still owed means the numbers cannot be trusted yet → UNKNOWN.
    """
    pkg = _affirmative_package()
    flipped = dataclasses.replace(pkg, rebuild_pending=True)
    verdict = reduce_verdict(flipped)
    assert verdict is not Verdict.GREEN
    assert verdict is Verdict.UNKNOWN


# ── UNKNOWN distinct from BLOCKED, and UNKNOWN dominates BLOCKED ──────────────


def test_unknown_is_distinct_from_blocked() -> None:
    """from: plan §5 item 2 (UNKNOWN-distinct-from-BLOCKED) + §4.3 three-valued axis.

    The two non-GREEN verdicts are not collapsed: an undecidable signal (a step UNKNOWN)
    reduces to UNKNOWN, while a decided failure (a step FAIL) reduces to BLOCKED — they are
    different enum members, not aliases.
    """
    pkg = _affirmative_package()
    green_pkg = pkg
    unknown_pkg = dataclasses.replace(pkg, steps=(("pytest", StepStatus.UNKNOWN), *pkg.steps[1:]))
    blocked_pkg = dataclasses.replace(pkg, steps=(("pytest", StepStatus.FAIL), *pkg.steps[1:]))
    # Collect the three reduction OUTPUTS and assert they are genuinely three distinct verdicts
    # (the axis is not collapsed to two). A set-cardinality check is a runtime comparison mypy
    # does not narrow to an always-true identity. This is computed BEFORE the precise per-value
    # asserts below (which would otherwise narrow the locals to single literals).
    outcomes = {
        reduce_verdict(green_pkg),
        reduce_verdict(unknown_pkg),
        reduce_verdict(blocked_pkg),
    }
    assert len(outcomes) == 3
    # And each maps to its specific member.
    assert reduce_verdict(unknown_pkg) is Verdict.UNKNOWN
    assert reduce_verdict(blocked_pkg) is Verdict.BLOCKED
    assert reduce_verdict(green_pkg) is Verdict.GREEN


def test_unknown_dominates_blocked_when_both_fire() -> None:
    """from: plan §4.3 ("UNKNOWN+BLOCKED both fire→UNKNOWN") + §5 item 2 (UNKNOWN-dominates).

    A package that simultaneously fires a BLOCKED condition (a step FAIL) AND an UNKNOWN
    condition (a None-sided anchor) reduces to UNKNOWN — UNKNOWN dominates BLOCKED.
    """
    pkg = _affirmative_package()
    both = dataclasses.replace(
        pkg,
        steps=(("pytest", StepStatus.FAIL), *pkg.steps[1:]),  # BLOCKED condition
        anchors=(AnchorCheck(name="gnomad_matches", expected="3054426", actual=None),),  # UNKNOWN
    )
    assert reduce_verdict(both) is Verdict.UNKNOWN


# ── Empty-anchor N/A path → GREEN-eligible ───────────────────────────────────


def test_empty_anchors_with_else_affirmative_is_green() -> None:
    """from: plan §4.3 ("Empty anchor set () + else-affirmative→GREEN") + §5 item 2
    (empty-anchors→GREEN).

    The N/A path: a change class with no applicable real-data anchors (empty anchor tuple)
    is GREEN-eligible when every other signal is affirmative. This is THIS PR's own
    self-gate shape.
    """
    pkg = _affirmative_package()
    na_pkg = dataclasses.replace(pkg, change_class=frozenset({"core"}), anchors=())
    assert reduce_verdict(na_pkg) is Verdict.GREEN


def test_empty_anchors_does_not_mask_a_blocked_signal() -> None:
    """from: plan §4.3 (empty-anchors is GREEN-eligible ONLY when else-affirmative) +
    §5 negative-control discipline.

    The N/A anchor path must not become a blanket GREEN: with empty anchors but a FAIL step,
    the verdict is still BLOCKED. This guards against the empty-anchor branch short-circuiting
    the rest of the reduction.
    """
    pkg = _affirmative_package()
    na_blocked = dataclasses.replace(
        pkg,
        anchors=(),
        steps=(("pytest", StepStatus.FAIL), *pkg.steps[1:]),
    )
    assert reduce_verdict(na_blocked) is Verdict.BLOCKED


def test_deferred_anchor_does_not_block_or_unknown() -> None:
    """from: plan §4.3 ("non-deferred anchor" qualifier — a deferred anchor is excluded) +
    frozen ``AnchorCheck.deferred`` semantics.

    A deferred anchor (schema rebuild pending, intentionally not evaluated this run) is NOT
    held to the full match — with everything else affirmative and rebuild not pending, a
    deferred anchor whose actual is None must not by itself drive UNKNOWN/BLOCKED. (The GREEN
    clause grades only NON-deferred anchors.)
    """
    pkg = _affirmative_package()
    deferred_anchor = AnchorCheck(
        name="gnomad_matches", expected="3054426", actual=None, deferred=True
    )
    deferred_pkg = dataclasses.replace(pkg, anchors=(deferred_anchor,))
    assert reduce_verdict(deferred_pkg) is Verdict.GREEN


# ── parse_step: keyed on EXIT CODE, never a stdout substring ──────────────────


def test_parse_step_exit_zero_is_pass() -> None:
    """from: plan §4.2 (0→PASS) + frozen ``parse_step`` signature."""
    assert parse_step("pytest", 0) is StepStatus.PASS


def test_parse_step_positive_exit_is_fail() -> None:
    """from: plan §4.2 (>0→FAIL) + §5 item 2 (exit-code keyed)."""
    assert parse_step("pytest", 1) is StepStatus.FAIL
    assert parse_step("mypy --strict backend/src", 2) is StepStatus.FAIL


def test_parse_step_none_exit_is_unknown() -> None:
    """from: plan §4.2 (None→UNKNOWN, never dropped) + §5 item 2."""
    assert parse_step("pytest", None) is StepStatus.UNKNOWN


def test_parse_step_keyed_on_exit_code_not_stdout() -> None:
    """from: plan §4.2 + §5 item 2 ("stdout 'FAILED' but exit 0→PASS").

    The parser is keyed on the process exit code, not on a stdout substring. The step name
    is carried only for diagnostics; a step named to look failing but exited 0 is a PASS.
    This is the explicit guard against substring-based status inference.
    """
    # A name that literally contains 'FAILED' but exit code 0 → PASS (exit code wins).
    assert parse_step("pytest [1 FAILED in log]", 0) is StepStatus.PASS


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (0, StepStatus.PASS),
        (1, StepStatus.FAIL),
        (255, StepStatus.FAIL),
        (None, StepStatus.UNKNOWN),
    ],
)
def test_parse_step_full_mapping(exit_code: int | None, expected: StepStatus) -> None:
    """from: plan §4.2 full mapping table (0→PASS / >0→FAIL / None→UNKNOWN)."""
    assert parse_step("any-step", exit_code) is expected

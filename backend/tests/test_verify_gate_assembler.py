"""Check-set assembly table — ``assemble_check_set(change_class) -> CheckSet`` (Sub Project A).

Plan-blind spec source: synthesized-plan §4.1 (the assembler folds into model.py as
``assemble_check_set(change_class)->CheckSet``), §5 test list item 3 ("change_class→check-set
table; schema→anchors deferred + rebuild_pending"), the §5 narrative (core/docs-only →
dev-loop steps, no anchors, rebuild_required=False; schema → rebuild_required=True + anchors
deferred; pipeline → merge anchors; annotation → index anchors), §6 (the merge/index anchors
are the verification.md capture columns), and the FROZEN INTERFACE CONTRACT
(``CHANGE_CLASS_VOCAB``; ``CheckSet`` 4-field dataclass; ``ChangeClass = frozenset[str]``).

Assertions reference the SPECIFIED shape only — the boolean ``rebuild_required`` (pinned per
class in §5) and MEMBERSHIP of the real capture column names (``consensus_total`` is the
headline merge anchor of verification.md / CLAUDE.md obs #3; ``gnomad_matches`` is the
headline index anchor of verification.md / CLAUDE.md obs #4). The exact full ``required_steps``
/ ``required_anchors`` tuples are NOT pinned by §5/§6, so they are NOT asserted (asserting them
would require reading the stubbed body — a plan-blindness violation). The dev-loop step names
used here are the real ``scripts/verify.sh`` labels (interface, not implementation).

RED on ``NotImplementedError`` (``assemble_check_set`` is a stub) is correct.
"""

from __future__ import annotations

from genome.verify_gate.model import CHANGE_CLASS_VOCAB, CheckSet, assemble_check_set

# Real ``scripts/verify.sh`` dev-loop step labels (interface; the always-run tail).
_DEV_LOOP_STEPS = frozenset(
    {"pytest", "ruff check", "mypy --strict backend/src", "genome docs check"}
)


# ── core / docs-only: dev-loop steps, no anchors, no rebuild ──────────────────


def test_core_class_has_dev_loop_steps_no_anchors_no_rebuild() -> None:
    """from: plan §5 item 3 (core/docs-only → dev-loop steps, no anchors, rebuild_required=False).

    The ``core`` class needs only the always-run dev-loop steps; it has no real-data anchors
    (the N/A path) and owes no DB rebuild.
    """
    result = assemble_check_set(frozenset({"core"}))
    assert isinstance(result, CheckSet)
    assert result.change_class == frozenset({"core"})
    # No real-data anchors apply to a docs/core-only change → the N/A path.
    assert result.required_anchors == ()
    # No schema rebuild owed.
    assert result.rebuild_required is False
    # The dev-loop steps are required (membership; exact full tuple not pinned by §5/§6).
    assert _DEV_LOOP_STEPS.issubset(set(result.required_steps))


# ── schema: rebuild_required=True + anchors deferred ─────────────────────────


def test_schema_class_requires_rebuild() -> None:
    """from: plan §5 item 3 (schema → rebuild_required=True) + §4.3 (schema defers anchors).

    A ``schema`` change owes a DB rebuild before its anchors can be trusted, so
    ``rebuild_required`` is True.
    """
    result = assemble_check_set(frozenset({"schema"}))
    assert result.rebuild_required is True


# ── pipeline: merge anchors ──────────────────────────────────────────────────


def test_pipeline_class_carries_merge_anchors() -> None:
    """from: plan §5 item 3 (pipeline → merge anchors) + §6 (verification.md merge captures).

    A ``pipeline`` change requires the merge real-data anchors. The headline merge capture
    column is ``consensus_total`` (verification.md pipeline section / CLAUDE.md obs #3); it
    must be among the required anchors (membership — the exact full set is not pinned by §5).
    """
    result = assemble_check_set(frozenset({"pipeline"}))
    assert result.required_anchors != ()
    assert "consensus_total" in set(result.required_anchors)


# ── annotation: index anchors ────────────────────────────────────────────────


def test_annotation_class_carries_index_anchors() -> None:
    """from: plan §5 item 3 (annotation → index anchors) + §6 (verification.md index captures).

    An ``annotation`` change requires the index real-data anchors. The headline index capture
    column is ``gnomad_matches`` (verification.md index section / CLAUDE.md obs #4); it must be
    among the required anchors (membership — the exact full set is not pinned by §5).
    """
    result = assemble_check_set(frozenset({"annotation"}))
    assert result.required_anchors != ()
    assert "gnomad_matches" in set(result.required_anchors)


# ── echo-back + vocabulary integrity ─────────────────────────────────────────


def test_change_class_is_echoed_back() -> None:
    """from: frozen ``CheckSet.change_class`` ("echoed back for traceability").

    The assembled set echoes the class it was built for — a multi-label class is carried
    through verbatim.
    """
    cls = frozenset({"schema", "pipeline"})
    result = assemble_check_set(cls)
    assert result.change_class == cls


def test_multi_label_schema_pipeline_unions_rebuild_and_merge_anchors() -> None:
    """from: plan §4.1 ("a schema change is also a pipeline change" — multi-label union) +
    §5 item 3 (schema → rebuild; pipeline → merge anchors).

    A change carrying both ``schema`` and ``pipeline`` folds both classes' requirements: the
    rebuild is owed (from schema) AND the merge anchors are present (from pipeline).
    """
    result = assemble_check_set(frozenset({"schema", "pipeline"}))
    assert result.rebuild_required is True
    assert "consensus_total" in set(result.required_anchors)


def test_change_class_vocab_is_the_four_known_classes() -> None:
    """from: FROZEN INTERFACE CONTRACT (``CHANGE_CLASS_VOCAB`` = the 4 classes).

    Pins the closed vocabulary the assembler validates against — a guard so a new class
    cannot be silently introduced without updating the gate. (This asserts the frozen
    constant, not stub logic, so it is GREEN-eligible immediately.)
    """
    assert set(CHANGE_CLASS_VOCAB) == {"core", "schema", "pipeline", "annotation"}

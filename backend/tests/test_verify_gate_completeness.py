"""Package-completeness enforcement at the assemble boundary (Stage-3 fix A1/A2/A3/A4/A5).

Spec source: the Stage-3 review reproduced two fail-closed BLOCKERS — an INCOMPLETE evidence
package reduced to GREEN because ``assemble_cmd`` built the package straight from the
skill-supplied flags without consulting ``assemble_check_set`` (which was dead code):

* a ``pipeline`` package with all dev-loop steps PASS but **no real-data anchors** → GREEN;
* a ``schema`` package assembled with ``--no-rebuild-pending`` → GREEN.

Package-completeness is a DECIDABLE function of ``change_class`` (the plan §2 thesis: every
decidable check belongs in the testable core, not the skill prose). The fix wires
``assemble_check_set`` into ``assemble_cmd`` so that:

* **A1** — a required real-data anchor the skill did not supply is injected all-``None`` →
  ``UNKNOWN`` (absence is not an affirmative pass);
* **A2** — ``rebuild_pending`` becomes ``user OR rebuild_required`` (a schema-containing class
  can never be assembled non-pending);
* **A3** — an unknown ``--change-class`` label is a ``typer.BadParameter`` (non-zero exit);
* **A4** — a ``deferred=true`` anchor is only honored when the class owes a rebuild;
* **A5** — a zero-step package reduces to ``UNKNOWN`` (a real run always ran ≥1 step).

These tests drive the CLI end-to-end (assemble → evidence.json → verdict), mirroring the
skill's flat-arg seam. The ``evidence.json`` shape is treated as opaque.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import click
import pytest
import structlog
from typer.testing import CliRunner

from genome.verify_gate.cli import _load_package, _parse_step, verify_gate_app
from genome.verify_gate.model import (
    _DEV_LOOP_STEPS,
    _INDEX_ANCHORS,
    _MERGE_ANCHORS,
    CHANGE_CLASS_VOCAB,
    StepStatus,
    assemble_check_set,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from click.testing import Result

# The six real ``scripts/verify.sh`` dev-loop step labels, all passing.
_ALL_STEPS_PASS: tuple[str, ...] = (
    "uv sync:0",
    "pytest:0",
    "ruff check:0",
    "ruff format --check:0",
    "mypy --strict backend/src:0",
    "genome docs check:0",
)


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    try:
        yield
    finally:
        structlog.reset_defaults()


def _steps_args() -> list[str]:
    out: list[str] = []
    for step in _ALL_STEPS_PASS:
        out += ["--step", step]
    return out


def _affirmative_integrity_args() -> list[str]:
    return [
        "--changelog-present",
        "--docs-check-clean",
        "--no-weakened-or-removed-test",
        "--no-gate-fill-survivor",
        "--test-count-before",
        "400",
        "--test-count-after",
        "406",
        "--no-rebuild-pending",
    ]


def _assemble(runner: CliRunner, args: list[str]) -> Result:
    return runner.invoke(verify_gate_app, args)


def _verdict(runner: CliRunner, package: Path) -> Result:
    return runner.invoke(verify_gate_app, ["verdict", "--package", str(package)])


def _format(runner: CliRunner, package: Path) -> Result:
    return runner.invoke(verify_gate_app, ["format", "--package", str(package)])


# Affirmative integrity block for a HAND-WRITTEN evidence.json (bypassing assemble).
_AFFIRMATIVE_INTEGRITY: dict[str, object] = {
    "changelog_present": True,
    "docs_check_clean": True,
    "weakened_or_removed_test": False,
    "gate_fill_survivor": False,
    "test_count_before": 400,
    "test_count_after": 406,
}


def _write_evidence(path: Path, payload: dict[str, object]) -> Path:
    """Write a hand-crafted evidence.json DIRECTLY — never via ``assemble`` — to prove the
    ``verdict`` boundary re-derives completeness on a bypassed package."""
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ── A1: pipeline / annotation missing its required anchors → UNKNOWN ──────────


def test_pipeline_package_with_no_anchors_is_unknown(tmp_path: Path) -> None:
    """The reproduced BLOCKER 1: a pipeline package with every step PASS but NO ``--anchor``
    must NOT be GREEN — the missing merge anchors inject as UNKNOWN, so ``verdict`` exits
    non-zero and offers no ``merge`` affordance.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    args = [
        "assemble",
        "--change-class",
        "pipeline",
        *_steps_args(),
        *_affirmative_integrity_args(),
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    assert result.exit_code != 0, result.output
    assert "merge" not in result.output.lower()
    assert "UNKNOWN" in result.output.upper()


def test_annotation_package_with_no_anchors_is_unknown(tmp_path: Path) -> None:
    """The same gap for the ``annotation`` class — its index anchors are required, so a
    package that omits them is UNKNOWN, never GREEN.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    args = [
        "assemble",
        "--change-class",
        "annotation",
        *_steps_args(),
        *_affirmative_integrity_args(),
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    assert result.exit_code != 0, result.output
    assert "merge" not in result.output.lower()


def test_pipeline_with_partial_anchors_is_unknown(tmp_path: Path) -> None:
    """Supplying SOME but not all required merge anchors is still incomplete → UNKNOWN.

    Only ``consensus_total`` is captured; the other required merge anchors inject all-``None``
    and drive the verdict to UNKNOWN.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    args = [
        "assemble",
        "--change-class",
        "pipeline",
        *_steps_args(),
        "--anchor",
        "name=consensus_total,expected=3160364,actual=3160364",
        *_affirmative_integrity_args(),
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    assert result.exit_code != 0, result.output


# ── A1 (steps): a missing required dev-loop step → UNKNOWN ────────────────────


def test_core_package_missing_a_required_dev_loop_step_is_unknown(tmp_path: Path) -> None:
    """A ``core`` package that supplies SOME dev-loop steps but omits a required one
    (``mypy --strict backend/src``) must NOT be GREEN — the missing step injects as UNKNOWN,
    so ``verdict`` exits non-zero. This is the step half of the completeness fix (Stage-3
    cycle 2), symmetric with the missing-anchor case.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    # All six canonical labels EXCEPT `mypy --strict backend/src`.
    partial = [s for s in _ALL_STEPS_PASS if not s.startswith("mypy")]
    assert "mypy --strict backend/src" in _DEV_LOOP_STEPS  # the omitted label is genuinely required
    step_args: list[str] = []
    for s in partial:
        step_args += ["--step", s]
    args = [
        "assemble",
        "--change-class",
        "core",
        *step_args,
        *_affirmative_integrity_args(),
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    assert result.exit_code != 0, result.output
    assert "merge" not in result.output.lower()
    assert "UNKNOWN" in result.output.upper()


def test_core_package_with_all_six_dev_loop_steps_is_green(tmp_path: Path) -> None:
    """The reconciled affirmative path: a ``core`` package supplying all six canonical
    ``verify.sh`` labels at PASS (a realistic complete package — finding-013) → GREEN. This
    proves the step-completeness enforcement does not block the legitimate happy path.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    args = [
        "assemble",
        "--change-class",
        "core",
        *_steps_args(),  # all six _ALL_STEPS_PASS labels
        *_affirmative_integrity_args(),
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    assert result.exit_code == 0, result.output
    assert "merge" in result.output.lower()


def test_core_package_with_a_failed_dev_loop_step_is_blocked(tmp_path: Path) -> None:
    """A ``core`` package where one of the six canonical steps FAILED (non-zero exit) →
    BLOCKED (a decided failure), with the other five PASS. Mirrors a `verify.sh` run that
    aborted at, e.g., `pytest`.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    step_args: list[str] = []
    for s in _ALL_STEPS_PASS:
        # pytest failed (exit 1); everything before it passed, nothing after ran — but for a
        # BLOCKED assertion we keep the rest PASS so the FAIL is the sole non-green signal.
        step_args += ["--step", "pytest:1" if s.startswith("pytest:") else s]
    args = [
        "assemble",
        "--change-class",
        "core",
        *step_args,
        *_affirmative_integrity_args(),
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    assert result.exit_code != 0, result.output
    assert "BLOCKED" in result.output.upper()


# ── Happy path: a COMPLETE pipeline package → GREEN (the fix didn't break it) ──


def test_complete_pipeline_package_is_green(tmp_path: Path) -> None:
    """A pipeline package with all dev-loop steps PASS AND every required merge anchor
    captured-and-matching reduces to GREEN — proving the completeness fix did not break the
    legitimate happy path.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    anchor_args: list[str] = []
    for i, name in enumerate(sorted(_MERGE_ANCHORS)):
        # Synthesized matching values (expected == actual); the magnitudes are arbitrary.
        value = str(1000 + i)
        anchor_args += ["--anchor", f"name={name},expected={value},actual={value}"]
    args = [
        "assemble",
        "--change-class",
        "pipeline",
        *_steps_args(),
        *anchor_args,
        *_affirmative_integrity_args(),
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    assert result.exit_code == 0, result.output
    assert "merge" in result.output.lower()


def test_complete_annotation_package_is_green(tmp_path: Path) -> None:
    """The annotation happy path: all index anchors captured-and-matching → GREEN."""
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    anchor_args: list[str] = []
    for i, name in enumerate(sorted(_INDEX_ANCHORS)):
        value = str(2000 + i)
        anchor_args += ["--anchor", f"name={name},expected={value},actual={value}"]
    args = [
        "assemble",
        "--change-class",
        "annotation",
        *_steps_args(),
        *anchor_args,
        *_affirmative_integrity_args(),
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    assert result.exit_code == 0, result.output
    assert "merge" in result.output.lower()


# ── A2: a schema (or schema-containing) class can't be assembled non-pending ──


def test_schema_package_no_rebuild_pending_is_unknown(tmp_path: Path) -> None:
    """The reproduced BLOCKER 2: assembling a ``schema`` package with ``--no-rebuild-pending``
    must still reduce to UNKNOWN — ``rebuild_pending`` is forced True for a schema class, so
    the operator cannot wave the rebuild away from the CLI.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    args = [
        "assemble",
        "--change-class",
        "schema",
        *_steps_args(),
        *_affirmative_integrity_args(),  # includes --no-rebuild-pending
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    assert result.exit_code != 0, result.output
    assert "merge" not in result.output.lower()


def test_schema_pipeline_multilabel_forces_rebuild_and_needs_anchors(tmp_path: Path) -> None:
    """A multi-label ``schema,pipeline`` class folds both: rebuild is forced (from schema) AND
    the merge anchors are required (from pipeline) — so even a fully-anchored package stays
    UNKNOWN until the rebuild is no longer owed.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    anchor_args: list[str] = []
    for i, name in enumerate(sorted(_MERGE_ANCHORS)):
        value = str(3000 + i)
        anchor_args += ["--anchor", f"name={name},expected={value},actual={value}"]
    args = [
        "assemble",
        "--change-class",
        "schema",
        "--change-class",
        "pipeline",
        *_steps_args(),
        *anchor_args,
        *_affirmative_integrity_args(),  # --no-rebuild-pending is overridden by schema
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    # rebuild_pending was forced True by the schema label → UNKNOWN.
    assert result.exit_code != 0, result.output


# ── A3: an unknown change-class label is a non-zero BadParameter ──────────────


def test_unknown_change_class_label_exits_nonzero(tmp_path: Path) -> None:
    """A typo'd ``--change-class`` (``piepline``) is not in the vocabulary, so ``assemble``
    surfaces it as a ``typer.BadParameter`` (non-zero) rather than building a silently-empty
    check set.
    """
    out = tmp_path / "evidence.json"
    args = [
        "assemble",
        "--change-class",
        "piepline",
        *_steps_args(),
        *_affirmative_integrity_args(),
        "--out",
        str(out),
    ]
    result = CliRunner().invoke(verify_gate_app, args)
    assert result.exit_code != 0, result.output
    assert not out.exists(), "a package was written for an unknown change class"


# ── A5: a zero-step package is UNKNOWN ───────────────────────────────────────


def test_zero_step_package_reduces_to_unknown(tmp_path: Path) -> None:
    """A package that recorded NO verification steps reduces to UNKNOWN — a real evidence
    package always ran at least one step, so an empty step list is undecidable, not GREEN.

    The CLI's ``--step`` is a required option (zero steps can't even be assembled there — a
    stronger guard), so this exercises the A5 reducer guard directly: a crafted
    ``evidence.json`` with an empty ``steps`` list, everything else affirmative, read back
    through ``from_json`` and reduced, must still exit non-zero (UNKNOWN).
    """
    out = tmp_path / "evidence.json"
    out.write_text(
        json.dumps(
            {
                "change_class": ["core"],
                "steps": [],
                "anchors": [],
                "integrity": {
                    "changelog_present": True,
                    "docs_check_clean": True,
                    "weakened_or_removed_test": False,
                    "gate_fill_survivor": False,
                    "test_count_before": 400,
                    "test_count_after": 406,
                },
                "rebuild_pending": False,
            }
        ),
        encoding="utf-8",
    )
    result = _verdict(CliRunner(), out)
    assert result.exit_code != 0, result.output
    assert "merge" not in result.output.lower()


# ── A4: a deferred=true anchor on a NON-rebuild class does not hide a mismatch ─


def test_deferred_flag_ignored_on_non_rebuild_class(tmp_path: Path) -> None:
    """A ``deferred=true`` anchor whose ``actual`` MISMATCHES ``expected`` on a ``pipeline``
    class (which owes no rebuild) must NOT be silently excluded — the deferred flag is honored
    only for a rebuild-owing class, so the mismatch still reduces to BLOCKED.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    # Capture every required merge anchor; one of them is a mismatch flagged deferred=true.
    anchor_args: list[str] = []
    for i, name in enumerate(sorted(_MERGE_ANCHORS)):
        value = str(4000 + i)
        if name == "consensus_total":
            anchor_args += [
                "--anchor",
                f"name={name},expected={value},actual=999999,deferred=true",
            ]
        else:
            anchor_args += ["--anchor", f"name={name},expected={value},actual={value}"]
    args = [
        "assemble",
        "--change-class",
        "pipeline",
        *_steps_args(),
        *anchor_args,
        *_affirmative_integrity_args(),
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    # The deferred=true flag was stripped (non-rebuild class) → the mismatch is BLOCKED.
    assert result.exit_code != 0, result.output
    assert "BLOCKED" in result.output.upper()


# ── A valid captured-anchor MISMATCH round-trips to BLOCKED ───────────────────


def test_captured_anchor_mismatch_round_trips_to_blocked(tmp_path: Path) -> None:
    """A complete pipeline package whose one captured anchor MISMATCHES (actual≠expected) is a
    decided failure → ``verdict`` exits non-zero with BLOCKED (not UNKNOWN — both sides are
    present), and offers no ``merge`` affordance.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    anchor_args: list[str] = []
    for i, name in enumerate(sorted(_MERGE_ANCHORS)):
        value = str(5000 + i)
        actual = "111111" if name == "consensus_total" else value
        anchor_args += ["--anchor", f"name={name},expected={value},actual={actual}"]
    args = [
        "assemble",
        "--change-class",
        "pipeline",
        *_steps_args(),
        *anchor_args,
        *_affirmative_integrity_args(),
        "--out",
        str(out),
    ]
    assert _assemble(runner, args).exit_code == 0
    result = _verdict(runner, out)
    assert result.exit_code != 0, result.output
    assert "BLOCKED" in result.output.upper()
    assert "merge" not in result.output.lower()


# ── D coverage: _load_package / _parse_step / assemble_check_set unit guards ──


def test_load_package_missing_file_raises(tmp_path: Path) -> None:
    """``_load_package`` on a non-existent file raises ``BadParameter`` (a clean non-zero CLI
    exit), never an uncaught ``OSError``.
    """
    with pytest.raises(click.exceptions.UsageError):
        _load_package(tmp_path / "does-not-exist.json")


def test_load_package_malformed_json_raises(tmp_path: Path) -> None:
    """Non-JSON content → ``BadParameter``."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(click.exceptions.UsageError):
        _load_package(bad)


def test_load_package_non_object_json_raises(tmp_path: Path) -> None:
    """A JSON value that is not an object (a bare list) → ``BadParameter``."""
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(click.exceptions.UsageError):
        _load_package(arr)


def test_parse_step_no_colon_raises() -> None:
    """``_parse_step`` on a token with no ``:`` separator → ``BadParameter``."""
    with pytest.raises(click.exceptions.UsageError):
        _parse_step("pytest")


def test_parse_step_empty_name_raises() -> None:
    """``_parse_step`` on a token with an empty name before ``:`` → ``BadParameter``."""
    with pytest.raises(click.exceptions.UsageError):
        _parse_step(":0")


def test_parse_step_empty_code_is_unknown() -> None:
    """The documented UNKNOWN case: an empty exit-code token (``pytest:``) parses to UNKNOWN,
    not an error.
    """
    name, status = _parse_step("pytest:")
    assert name == "pytest"
    assert status is StepStatus.UNKNOWN


def test_assemble_check_set_unknown_class_raises_value_error() -> None:
    """``assemble_check_set`` rejects a label outside :data:`CHANGE_CLASS_VOCAB` with a
    ``ValueError`` (the CLI converts this to a non-zero ``BadParameter``).
    """
    assert "bogus" not in CHANGE_CLASS_VOCAB
    with pytest.raises(ValueError, match="unknown change class"):
        assemble_check_set(frozenset({"bogus"}))


# ── verdict-boundary self-sufficiency (Stage-3 cycle 3: close silent-1/silent-2) ──
# The skill gates on `verdict`'s exit code, so `verdict` must complete a package itself.
# These write the evidence.json DIRECTLY (bypassing `assemble`) and prove `verdict` re-derives
# completeness — a hand-crafted incomplete package cannot read GREEN at the read boundary.


def test_verdict_completes_handcrafted_core_missing_steps_to_unknown(tmp_path: Path) -> None:
    """Hunt F: a hand-written ``core`` package with only ``[["pytest","pass"]]`` (five of the
    six dev-loop steps absent) + affirmative + ``rebuild_pending:false`` fed straight to
    ``verdict`` must be completed (missing steps → UNKNOWN) → exit non-zero, no ``merge``.
    """
    out = _write_evidence(
        tmp_path / "huntF.json",
        {
            "change_class": ["core"],
            "steps": [["pytest", "pass"]],
            "anchors": [],
            "integrity": _AFFIRMATIVE_INTEGRITY,
            "rebuild_pending": False,
        },
    )
    result = _verdict(CliRunner(), out)
    assert result.exit_code != 0, result.output
    assert "merge" not in result.output.lower()
    assert "UNKNOWN" in result.output.upper()


def test_verdict_completes_handcrafted_pipeline_missing_anchors_to_unknown(
    tmp_path: Path,
) -> None:
    """Hunt G: a hand-written ``pipeline`` package with all six steps PASS but ``anchors:[]``
    fed straight to ``verdict`` must be completed (missing merge anchors → UNKNOWN) → non-zero.
    """
    out = _write_evidence(
        tmp_path / "huntG.json",
        {
            "change_class": ["pipeline"],
            "steps": [[label, "pass"] for label in _DEV_LOOP_STEPS],
            "anchors": [],
            "integrity": _AFFIRMATIVE_INTEGRITY,
            "rebuild_pending": False,
        },
    )
    result = _verdict(CliRunner(), out)
    assert result.exit_code != 0, result.output
    assert "merge" not in result.output.lower()


def test_verdict_completes_handcrafted_schema_deferred_mismatch_not_green(
    tmp_path: Path,
) -> None:
    """Hunt J: a hand-written ``schema`` package with a ``deferred:true`` MISMATCH anchor +
    ``rebuild_pending:false`` must NOT read GREEN — a schema class re-forces ``rebuild_pending``
    (→ UNKNOWN), so the bypassed package is caught at ``verdict``.
    """
    out = _write_evidence(
        tmp_path / "huntJ.json",
        {
            "change_class": ["schema"],
            "steps": [[label, "pass"] for label in _DEV_LOOP_STEPS],
            "anchors": [
                {
                    "name": "consensus_total",
                    "expected": "100",
                    "actual": "999999",
                    "deferred": True,
                }
            ],
            "integrity": _AFFIRMATIVE_INTEGRITY,
            "rebuild_pending": False,
        },
    )
    result = _verdict(CliRunner(), out)
    assert result.exit_code != 0, result.output
    assert "merge" not in result.output.lower()


def test_verdict_handcrafted_pipeline_deferred_mismatch_non_rebuild_is_blocked(
    tmp_path: Path,
) -> None:
    """A hand-written ``pipeline`` (non-rebuild) package with a ``deferred:true`` MISMATCH but
    otherwise complete must surface the mismatch — ``verdict`` unmasks the deferred flag (the
    class owes no rebuild) → BLOCKED, never a hidden GREEN.
    """
    anchors: list[dict[str, object]] = []
    for i, name in enumerate(sorted(_MERGE_ANCHORS)):
        value = str(6000 + i)
        if name == "consensus_total":
            anchors.append({"name": name, "expected": value, "actual": "999999", "deferred": True})
        else:
            anchors.append({"name": name, "expected": value, "actual": value})
    out = _write_evidence(
        tmp_path / "pipe_deferred.json",
        {
            "change_class": ["pipeline"],
            "steps": [[label, "pass"] for label in _DEV_LOOP_STEPS],
            "anchors": anchors,
            "integrity": _AFFIRMATIVE_INTEGRITY,
            "rebuild_pending": False,
        },
    )
    result = _verdict(CliRunner(), out)
    assert result.exit_code != 0, result.output
    assert "BLOCKED" in result.output.upper()


def test_verdict_handcrafted_complete_package_is_green(tmp_path: Path) -> None:
    """A hand-written COMPLETE ``core`` package (all six steps PASS, affirmative, not pending)
    fed straight to ``verdict`` is GREEN — the read-side completion is idempotent on a package
    that is already complete, so the happy path is intact.
    """
    out = _write_evidence(
        tmp_path / "complete.json",
        {
            "change_class": ["core"],
            "steps": [[label, "pass"] for label in _DEV_LOOP_STEPS],
            "anchors": [],
            "integrity": _AFFIRMATIVE_INTEGRITY,
            "rebuild_pending": False,
        },
    )
    result = _verdict(CliRunner(), out)
    assert result.exit_code == 0, result.output
    assert "merge" in result.output.lower()


def test_format_of_incomplete_handcrafted_package_shows_injected_unknowns(
    tmp_path: Path,
) -> None:
    """``format`` of an incomplete hand-written ``pipeline`` package shows the injected
    not-captured anchors AND the declared ``change_class`` — so the human review surface
    reflects the completed package, not a misleadingly clean view.
    """
    out = _write_evidence(
        tmp_path / "fmt.json",
        {
            "change_class": ["pipeline"],
            "steps": [["pytest", "pass"]],
            "anchors": [],
            "integrity": _AFFIRMATIVE_INTEGRITY,
            "rebuild_pending": False,
        },
    )
    result = _format(CliRunner(), out)
    assert result.exit_code == 0, result.output
    # The declared change_class is visible for the human approval backstop (silent-3).
    assert "pipeline" in result.output
    # The required merge anchors were injected (not-captured) and appear in the block.
    assert "consensus_total" in result.output
    # A required dev-loop step the package omitted is shown as UNKNOWN.
    assert "UNKNOWN" in result.output.upper()

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

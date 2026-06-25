"""CLI surface — ``genome verify-gate {assemble,verdict,format}`` exit codes + seam.

Plan-blind spec source: synthesized-plan §4.5 + R1 (the serialization seam: ``assemble``
builds the EvidencePackage from FLAT primitive args and writes ``evidence.json``;
``verdict``/``format`` READ that file; ``verdict`` exits 0 on GREEN printing a ``merge``
affordance, non-zero on BLOCKED or UNKNOWN), §5 test list item 5 (every exit-code test uses
``_assert_clean_exit``; green→0, blocked→nonzero, unknown→nonzero), R1 (an
assemble→json→verdict ROUND-TRIP proves the flat-arg seam; a malformed ``--step``/``--anchor``
arg → non-zero, not a silent coerce), and the FROZEN INTERFACE CONTRACT (the exact ``--``
flag spellings + defaults).

``_assert_clean_exit`` (adapted from ``test_docs_cli.py``) distinguishes a deliberate
``typer.Exit(code)`` from a stub ``NotImplementedError`` crash — so every exit-code test is
honestly RED until the bodies are filled, instead of passing on the stub's crash
(``assemble_cmd`` / ``verdict_cmd`` / ``format_cmd`` all ``raise NotImplementedError`` now).

R1 discipline: the round-trip assembles via the CLI and reads ``evidence.json`` back through
the CLI — ``evidence.json``'s internal JSON shape is treated as OPAQUE (the test never
hand-builds nested JSON, mirroring the bash skill, and never asserts the serialization
format). ``gh``/``rm``/the merge itself live in the SKILL, never in the CLI — so the
"no merge on BLOCKED" guarantee is the SKILL invariant (the skill only reaches the merge step
if ``verdict`` exited 0); here we assert what the CLI CAN prove: BLOCKED/UNKNOWN exit non-zero
and print no ``merge`` affordance.

Pre-mortem coupling (premortem-digest skeptic-1 #1 → R1): this file pins the predicted
surprise "nothing wrote the nested EvidencePackage JSON" — the round-trip is its guard test.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome.verify_gate.cli import verify_gate_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from click.testing import Result


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Restore structlog defaults after each test (mirrors test_docs_cli)."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def _assert_clean_exit(result: Result, code: int) -> None:
    """Assert a deliberate Typer exit with ``code`` — NOT an uncaught stub crash.

    Adapted from ``test_docs_cli.py``: a stubbed ``assemble_cmd`` / ``verdict_cmd`` /
    ``format_cmd`` raises ``NotImplementedError``, which ``CliRunner.invoke`` reports as
    ``exit_code == 1`` with ``result.exception`` set to that ``NotImplementedError``. That
    must NOT be mistaken for a real gate exit. This helper requires the exit not be an
    uncaught ``NotImplementedError`` — which keeps these tests honestly RED until the bodies
    land, instead of passing on the stub's crash.
    """
    assert result.exit_code == code, result.output
    exc = result.exception
    assert not isinstance(exc, NotImplementedError), (
        f"exit came from an unfilled stub, not the gate: {exc!r}"
    )


# ── Flat-arg builders (the skill passes only flat strings — R1) ──────────────


def _affirmative_assemble_args(out: Path, *, change_class: str = "core") -> list[str]:
    """Flat ``assemble`` args for a GREEN-eligible N/A-path package (no ``--anchor``).

    Supplies the **six canonical ``scripts/verify.sh`` dev-loop labels** each PASS — a
    *realistic complete* package (finding-013), since ``assemble_check_set`` requires the full
    dev-loop tail for every change class. (Reconciled from the earlier 2-label
    ``pytest``/``ruff`` shorthand when step-completeness enforcement landed — Stage-3 cycle 2:
    a realism strengthening; the affirmative case stays GREEN and no assertion changed.)
    """
    return [
        "assemble",
        "--change-class",
        change_class,
        "--step",
        "uv sync:0",
        "--step",
        "pytest:0",
        "--step",
        "ruff check:0",
        "--step",
        "ruff format --check:0",
        "--step",
        "mypy --strict backend/src:0",
        "--step",
        "genome docs check:0",
        "--changelog-present",
        "--docs-check-clean",
        "--no-weakened-or-removed-test",
        "--no-gate-fill-survivor",
        "--test-count-before",
        "400",
        "--test-count-after",
        "406",
        "--no-rebuild-pending",
        "--out",
        str(out),
    ]


def _assemble(runner: CliRunner, args: list[str]) -> Result:
    return runner.invoke(verify_gate_app, args)


# ── assemble: flat args → writes evidence.json ───────────────────────────────


def test_assemble_writes_evidence_json(tmp_path: Path) -> None:
    """from: plan §4.5 / R1 (assemble builds the package from flat args → evidence.json) +
    §5 item 5.

    The ``assemble`` command exits 0 and writes the evidence file at ``--out`` (its contents
    are opaque to this test — only that the seam produced a file).
    """
    out = tmp_path / "evidence.json"
    result = _assemble(CliRunner(), _affirmative_assemble_args(out))
    _assert_clean_exit(result, 0)
    assert out.exists(), "assemble did not write evidence.json"
    # It is valid JSON (the seam the verdict/format commands read back). Shape is opaque.
    json.loads(out.read_text(encoding="utf-8"))


# ── verdict: GREEN → 0 + merge affordance; BLOCKED/UNKNOWN → non-zero ─────────


def test_verdict_green_exits_zero_and_prints_merge_affordance(tmp_path: Path) -> None:
    """from: plan §4.5 (verdict exits 0 on GREEN, prints the ``merge`` affordance) + §5 item 5
    (green→0) + R1 (round-trip).

    Round-trip: assemble a GREEN-eligible package, then ``verdict --package`` it → exit 0 and
    an output that offers the ``merge`` affordance the operator types next.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    _assert_clean_exit(_assemble(runner, _affirmative_assemble_args(out)), 0)

    result = runner.invoke(verify_gate_app, ["verdict", "--package", str(out)])
    _assert_clean_exit(result, 0)
    assert "merge" in result.output.lower()


def test_verdict_blocked_exits_nonzero_and_offers_no_merge(tmp_path: Path) -> None:
    """from: plan §4.5 (non-zero on BLOCKED; the skill's whole gate) + §5 item 5 (blocked→nonzero).

    Flip one decided-failure flag (``--no-changelog-present`` → BLOCKED). ``verdict`` exits
    non-zero and does NOT print a ``merge`` affordance — the signal the skill stops on.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    args = _affirmative_assemble_args(out)
    # Flip changelog to absent → a decided failure → BLOCKED.
    args[args.index("--changelog-present")] = "--no-changelog-present"
    _assert_clean_exit(_assemble(runner, args), 0)

    result = runner.invoke(verify_gate_app, ["verdict", "--package", str(out)])
    assert result.exit_code != 0, result.output
    # A clean Typer exit, not a stub crash.
    assert not isinstance(result.exception, NotImplementedError), result.exception
    assert "merge" not in result.output.lower()


def test_verdict_unknown_exits_nonzero(tmp_path: Path) -> None:
    """from: plan §4.5 (non-zero on UNKNOWN too) + §5 item 5 (unknown→nonzero).

    Leave ``--rebuild-pending`` set (an undecidable signal) → UNKNOWN. ``verdict`` exits
    non-zero (BLOCKED and UNKNOWN both stop the skill).
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    args = _affirmative_assemble_args(out)
    # Keep rebuild pending → UNKNOWN.
    args[args.index("--no-rebuild-pending")] = "--rebuild-pending"
    _assert_clean_exit(_assemble(runner, args), 0)

    result = runner.invoke(verify_gate_app, ["verdict", "--package", str(out)])
    assert result.exit_code != 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception


# ── format: prints the evidence block ────────────────────────────────────────


def test_format_prints_the_evidence_block(tmp_path: Path) -> None:
    """from: plan §4.5 (format reads evidence.json and prints the raw block) + §5 item 5.

    A round-trip through ``format`` exits 0 and emits a non-empty block (the operator's
    review surface). For an N/A-path package it carries the N/A sentinel.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    _assert_clean_exit(_assemble(runner, _affirmative_assemble_args(out)), 0)

    result = runner.invoke(verify_gate_app, ["format", "--package", str(out)])
    _assert_clean_exit(result, 0)
    assert result.output.strip() != ""
    # This is the N/A path (no --anchor) → the sentinel is rendered.
    assert "N/A — no real-data anchors apply to this change-class" in result.output


# ── round-trip seam: assemble → json → verdict (R1) ──────────────────────────


def test_assemble_to_verdict_round_trip(tmp_path: Path) -> None:
    """from: R1 (proves the flat-arg serialization seam) + §5 item 5.

    The whole seam end-to-end: flat args → ``assemble`` writes evidence.json → ``verdict``
    reads it back and reduces it. Bash never assembled nested JSON; this proves the file the
    skill produces is the file the gate consumes. GREEN-eligible inputs → exit 0.
    """
    runner = CliRunner()
    out = tmp_path / "evidence.json"
    assemble_result = _assemble(runner, _affirmative_assemble_args(out))
    _assert_clean_exit(assemble_result, 0)
    assert out.exists()

    verdict_result = runner.invoke(verify_gate_app, ["verdict", "--package", str(out)])
    _assert_clean_exit(verdict_result, 0)


# ── malformed flat args → non-zero exit (no silent coerce) ───────────────────


def test_malformed_step_arg_exits_nonzero(tmp_path: Path) -> None:
    """from: R1 (a malformed ``--step`` arg → non-zero, not a silent coerce) + §5 item 5.

    A ``--step`` whose exit-code token is non-numeric garbage (``pytest:notanumber``) must NOT
    be silently coerced (e.g. swallowed to UNKNOWN/PASS without signal) — ``assemble`` exits
    non-zero. (``pytest:`` with an EMPTY code is the documented UNKNOWN case and is NOT
    malformed; this test uses a genuinely non-numeric token.)
    """
    out = tmp_path / "evidence.json"
    args = [
        "assemble",
        "--change-class",
        "core",
        "--step",
        "pytest:notanumber",
        "--out",
        str(out),
    ]
    result = CliRunner().invoke(verify_gate_app, args)
    assert result.exit_code != 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception


def test_malformed_anchor_arg_exits_nonzero(tmp_path: Path) -> None:
    """from: R1 (a malformed ``--anchor`` arg → non-zero, not a silent coerce) + §5 item 5.

    A ``--anchor`` missing its required ``name=``/``expected=``/``actual=`` structure
    (``garbage-no-equals``) must exit non-zero rather than be silently dropped — a fabricated
    or skipped anchor is exactly the false-GREEN risk the gate exists to prevent.
    """
    out = tmp_path / "evidence.json"
    args = [
        "assemble",
        "--change-class",
        "annotation",
        "--step",
        "pytest:0",
        "--anchor",
        "garbage-no-equals",
        "--out",
        str(out),
    ]
    result = CliRunner().invoke(verify_gate_app, args)
    assert result.exit_code != 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception

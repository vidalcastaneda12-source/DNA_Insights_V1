"""CLI surface — ``genome fast-follow {scan-assemble,triage,eject-draft}`` exit codes + seam.

Plan-blind spec source: synthesized-plan §4 (the three commands: ``scan-assemble`` flat
``--candidate 'k=v,...'`` → candidates.json; ``triage --candidates <json> [--dry-run]``;
``eject-draft`` ROADMAP draft to STDOUT, never writes ROADMAP; "Malformed → clean non-zero
BadParameter"), R2 (the JSON file is the CANONICAL input seam; ``scan-assemble`` keeps the
``|`` intra-field sub-delimiter for collection fields; REQUIRED test: a ``docs/schemas/**``
path survives the round-trip and STILL EJECTs — proves the touched_paths guard is not defeated
by parsing), §5 test list item 4 (scan-assemble→triage round-trip; --dry-run exits 0;
malformed → non-zero; eject-draft returns STRING to stdout, no in-place ROADMAP write;
structlog-reset fixture), OQ-3, and the FROZEN INTERFACE CONTRACT (the exact ``--`` flag
spellings + the ``|`` sub-delimiter for touched_paths/change_class).

``_assert_clean_exit`` (adapted from ``test_verify_gate_cli.py`` / ``test_docs_cli.py``)
distinguishes a deliberate ``typer.Exit(code)`` from a stub ``NotImplementedError`` crash — so
every exit-code test is honestly RED until the bodies are filled, instead of passing on the
stub's crash.

Pre-mortem coupling (R2 / RANKED riskiest #1): the ``|`` sub-delimiter + ``docs/schemas/**``
round-trip test is the guard test for the predicted surprise "a mis-parsed touched_paths is a
FALSE-DRAIN path" — it proves the parser does not defeat the §2 safety guard.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome.fast_follow.cli import fast_follow_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from click.testing import Result


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Restore structlog defaults after each test (mirrors test_verify_gate_cli)."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def _assert_clean_exit(result: Result, code: int) -> None:
    """Assert a deliberate Typer exit with ``code`` — NOT an uncaught stub crash.

    A stubbed command raises ``NotImplementedError``, which ``CliRunner.invoke`` reports as
    ``exit_code == 1`` with ``result.exception`` set. That must NOT be mistaken for a real CLI
    exit — this helper keeps the tests honestly RED until the bodies land.
    """
    assert result.exit_code == code, result.output
    exc = result.exception
    assert not isinstance(exc, NotImplementedError), (
        f"exit came from an unfilled stub, not the CLI: {exc!r}"
    )


# ── A flat --candidate token for a DRAIN-eligible candidate ───────────────────


def _drain_candidate_token() -> str:
    """A flat 'k=v,...' token for a guard-clearing DRAIN candidate (scalar fields only)."""
    return (
        "candidate_id=cand-d1,"
        "source=repo-sweep,"
        "kind=doc-nit,"
        "change_class=core,"
        "blast_radius=1,"
        "applicable_anchors=0,"
        "tier=tier-0,"
        "touched_paths=docs/notes/foo.md,"
        "is_stale=false"
    )


def _schema_path_candidate_token() -> str:
    """A flat token whose touched_paths use the ``|`` sub-delimiter and include a schema path,
    while change_class is (deliberately) the benign ``core`` — the R2 false-DRAIN guard case."""
    return (
        "candidate_id=cand-schema,"
        "source=repo-sweep,"
        "kind=schema-edit,"
        "change_class=core,"
        "blast_radius=1,"
        "applicable_anchors=0,"
        "tier=tier-0,"
        "touched_paths=docs/schemas/x.md|ddl/y.sql,"
        "is_stale=false"
    )


# ── scan-assemble → JSON → triage round-trip ──────────────────────────────────


def test_scan_assemble_writes_candidates_json(tmp_path: Path) -> None:
    """from: plan §4 (scan-assemble flat --candidate → candidates.json) + §5 item 4.

    ``scan-assemble`` exits 0 and writes a candidates JSON at ``--out``; the file is valid JSON
    (the canonical seam ``triage`` reads back). Its shape is treated as opaque here.
    """
    out = tmp_path / "candidates.json"
    result = CliRunner().invoke(
        fast_follow_app,
        ["scan-assemble", "--candidate", _drain_candidate_token(), "--out", str(out)],
    )
    _assert_clean_exit(result, 0)
    assert out.exists(), "scan-assemble did not write candidates.json"
    json.loads(out.read_text(encoding="utf-8"))


def test_scan_assemble_to_triage_round_trip(tmp_path: Path) -> None:
    """from: R2 (the JSON file is the canonical seam; round-trip proves it) + §5 item 4.

    The whole seam: flat ``--candidate`` token → ``scan-assemble`` writes candidates.json →
    ``triage --candidates`` reads it back and reduces it. A DRAIN-eligible candidate round-trips
    to exit 0.
    """
    runner = CliRunner()
    out = tmp_path / "candidates.json"
    _assert_clean_exit(
        runner.invoke(
            fast_follow_app,
            ["scan-assemble", "--candidate", _drain_candidate_token(), "--out", str(out)],
        ),
        0,
    )
    result = runner.invoke(fast_follow_app, ["triage", "--candidates", str(out)])
    _assert_clean_exit(result, 0)


# ── The ``|`` sub-delimiter + touched_paths safety case (R2) ──────────────────


def test_schema_path_survives_round_trip_and_still_ejects(tmp_path: Path) -> None:
    """from: R2 (REQUIRED test — a docs/schemas/** path survives scan-assemble→JSON and STILL
    EJECTs, proving the ``|`` parser does not defeat the touched_paths guard) + plan §2 safety
    invariant + A2 (literal-path guard).

    A ``--candidate`` whose ``touched_paths=docs/schemas/x.md|ddl/y.sql`` and (mislabelled)
    ``change_class=core`` is assembled to JSON, then triaged. The schema/ddl literal path must
    survive the ``|`` sub-delimiter parse and route the candidate to EJECT — never DRAIN. The
    triage output names the eject so a mis-parse (which would DRAIN) is caught.
    """
    runner = CliRunner()
    out = tmp_path / "candidates.json"
    _assert_clean_exit(
        runner.invoke(
            fast_follow_app,
            ["scan-assemble", "--candidate", _schema_path_candidate_token(), "--out", str(out)],
        ),
        0,
    )
    result = runner.invoke(fast_follow_app, ["triage", "--candidates", str(out)])
    _assert_clean_exit(result, 0)
    lowered = result.output.lower()
    assert "eject" in lowered, result.output
    # The safety invariant: this candidate must NOT be reported as drained.
    assert "cand-schema" in result.output
    assert "would drain cand-schema" not in lowered


# ── --dry-run exits 0 (the first-class test target, OQ-2) ─────────────────────


def test_triage_dry_run_exits_zero(tmp_path: Path) -> None:
    """from: plan §4 / OQ-2 (--dry-run = scan+triage only, first-class test target) + §5 item 4.

    ``triage --candidates <json> --dry-run`` over a DRAIN-eligible candidate exits 0 (a
    classify-only pass that performs no merge / no eject write).
    """
    runner = CliRunner()
    out = tmp_path / "candidates.json"
    _assert_clean_exit(
        runner.invoke(
            fast_follow_app,
            ["scan-assemble", "--candidate", _drain_candidate_token(), "--out", str(out)],
        ),
        0,
    )
    result = runner.invoke(fast_follow_app, ["triage", "--candidates", str(out), "--dry-run"])
    _assert_clean_exit(result, 0)


# ── Malformed --candidate → non-zero (BadParameter), never a silent coerce ────


def test_malformed_candidate_token_exits_nonzero(tmp_path: Path) -> None:
    """from: plan §4 ("Malformed → clean non-zero BadParameter") + §5 item 4 (malformed →
    non-zero) + R2 (never a silent coerce — a mis-parsed candidate is a false-DRAIN risk).

    A ``--candidate`` token that is malformed (a bare token with no ``key=value`` structure)
    must exit non-zero rather than be silently coerced into a partial/empty Candidate.
    """
    out = tmp_path / "candidates.json"
    result = CliRunner().invoke(
        fast_follow_app,
        ["scan-assemble", "--candidate", "garbage-no-equals", "--out", str(out)],
    )
    assert result.exit_code != 0, result.output
    assert not isinstance(result.exception, NotImplementedError), result.exception


# ── eject-draft prints to stdout, exits 0, never writes/modifies ROADMAP ──────


def test_eject_draft_prints_to_stdout_and_writes_no_roadmap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from: plan §4 / OQ-3 (eject-draft drafts to STDOUT, never writes ROADMAP) + §5 item 4
    (returns STRING to stdout, no in-place ROADMAP write) + §7 (autonomous ROADMAP writes are
    out of scope).

    Build a candidates.json containing an EJECT candidate, run ``eject-draft``: it exits 0,
    prints a non-empty draft to stdout, and creates NO ``ROADMAP*`` file in the (chdir'd) tmp
    working tree — the draft is stdout-only, never an in-place write.
    """
    runner = CliRunner()
    out = tmp_path / "candidates.json"
    eject_token = _schema_path_candidate_token()
    _assert_clean_exit(
        runner.invoke(
            fast_follow_app,
            ["scan-assemble", "--candidate", eject_token, "--out", str(out)],
        ),
        0,
    )

    # chdir into an isolated work dir so any stray ROADMAP write is detectable in-tree.
    sandbox = tmp_path / "work"
    sandbox.mkdir()
    monkeypatch.chdir(sandbox)
    result = runner.invoke(fast_follow_app, ["eject-draft", "--candidates", str(out)])
    _assert_clean_exit(result, 0)
    assert result.output.strip() != "", "eject-draft printed nothing to stdout"
    roadmap_writes = list(sandbox.glob("ROADMAP*"))
    assert roadmap_writes == [], f"eject-draft wrote a ROADMAP file: {roadmap_writes}"

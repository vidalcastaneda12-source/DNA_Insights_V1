"""CLI surface — ``genome docs build-index`` / ``genome docs check`` exit codes.

Plan-blind spec source: the approved ``decision-tracking-leak-fix`` plan §5 ("Injected-gap
CLI" — a decision-finding with no DEC row makes ``genome docs check`` exit 1) + §6 (the
single gate; ``build-index --no-write`` drift guard), Task 4 (the sub-app is registered on
the ``genome`` CLI), and the frozen interface contract (``genome.docs.cli`` surface,
behavioural contracts #5, #9). Expected exit codes / output come from that spec — never from
the stubbed bodies (``cli.py`` ``_repo_root`` + ``validator.check`` + ``index.build_index``
all ``raise NotImplementedError`` right now; RED is correct).

The ``docs`` subcommands anchor on the repo root by walking up from cwd to the first dir
holding ``CLAUDE.md`` (contract ``_repo_root``), so the CLI tests ``chdir`` into a minimal
fixture repo. The Typer sub-app is exercised directly via ``CliRunner`` (the frozen surface,
independent of whether the Task-4 ``add_typer`` wiring has landed); one test additionally
asserts the wiring on the real ``genome`` CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome.docs.cli import docs_app
from genome.docs.model import INDEX_BEGIN_MARKER, INDEX_END_MARKER

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from click.testing import Result


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Restore structlog defaults after each test (mirrors test_annotate_cli)."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def _finding(  # noqa: PLR0913 — a fixture builder; each kwarg is one frontmatter key
    *,
    number: str,
    type_: str = "decision",
    status: str = "active",
    supersedes: str = "[]",
    superseded_by: str = "[]",
    title: str = "a finding",
) -> str:
    return (
        "---\n"
        f"type: {type_}\n"
        f"status: {status}\n"
        "actors: [VSC-User]\n"
        "date: 2026-06-23\n"
        f"supersedes: {supersedes}\n"
        f"superseded_by: {superseded_by}\n"
        "---\n"
        f"# Finding {number} — {title}\n"
        "\n"
        "Body text.\n"
    )


def _ledger(rows: str) -> str:
    columns = (
        "| DEC | kind | date | status | superseded_by | actors | provenance |"
        " decision | detail-link |\n"
    )
    separator = "|---|---|---|---|---|---|---|---|---|\n"
    return (
        "# MEMORY — decision ledger\n"
        "\n"
        "<!-- BEGIN decision-ledger -->\n"
        "\n" + columns + separator + rows + "\n"
        "<!-- END decision-ledger -->\n"
    )


def _readme() -> str:
    return f"# Findings\n\nPreamble.\n\n{INDEX_BEGIN_MARKER}\n{INDEX_END_MARKER}\n\nTrailer.\n"


# A clean, cross-resolved supersession pair (DEC-0001 → DEC-0002) whose finding
# frontmatter mirrors the edge — the positive baseline for the gate.
_CLEAN_LEDGER_ROWS = (
    "| DEC-0001 | architectural | 2026-05-22 | superseded | DEC-0002 | VSC-User |"
    " finding-011 | gnomAD filter scoped three-way; see CLAUDE.md obs #4 |"
    " docs/findings/finding-011-gnomad-three-way-intersection.md |\n"
    "| DEC-0002 | architectural | 2026-06-21 | active | — | VSC-User |"
    " finding-035 | gnomAD filter narrowed to user_only |"
    " docs/findings/finding-035-gnomad-filter-set-consumer-audit.md |\n"
)


def _clean_findings() -> dict[str, str]:
    return {
        "finding-011-gnomad-three-way-intersection.md": _finding(
            number="011",
            status="superseded",
            superseded_by="[finding-035]",
            title="gnomAD three-way intersection",
        ),
        "finding-035-gnomad-filter-set-consumer-audit.md": _finding(
            number="035",
            status="active",
            supersedes="[finding-011]",
            title="gnomAD filter-set consumer audit",
        ),
    }


def _write_repo(
    root: Path,
    *,
    findings: dict[str, str],
    ledger: str,
) -> None:
    (root / "CLAUDE.md").write_text(
        "# Project\n\n## Real-data observations\n",
        encoding="utf-8",
    )
    (root / "MEMORY.md").write_text(ledger, encoding="utf-8")
    findings_dir = root / "docs" / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    for name, text in findings.items():
        (findings_dir / name).write_text(text, encoding="utf-8")
    findings_dir.joinpath("README.md").write_text(_readme(), encoding="utf-8")


def _run_in_repo(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    args: list[str],
) -> Result:
    """Invoke ``docs_app`` with cwd inside ``root`` so ``_repo_root`` anchors there."""
    monkeypatch.chdir(root)
    return CliRunner().invoke(docs_app, args)


def _assert_clean_exit(result: Result, code: int) -> None:
    """Assert a deliberate Typer exit with ``code`` — NOT an uncaught crash.

    A stubbed ``_repo_root`` / ``check`` / ``build_index`` raises ``NotImplementedError``,
    which ``CliRunner.invoke`` reports as ``exit_code == 1`` with
    ``result.exception`` set to that ``NotImplementedError``. That must NOT be mistaken
    for a real gate failure: this helper additionally requires the exit not be an uncaught
    ``NotImplementedError`` (a real ``typer.Exit(code)`` surfaces as ``SystemExit``, a clean
    exit; an OK exit-0 surfaces as no exception). This is what keeps the exit-code tests
    honestly RED until the bodies are filled, instead of passing on the stub's crash.
    """
    assert result.exit_code == code, result.output
    exc = result.exception
    assert not isinstance(exc, NotImplementedError), (
        f"exit came from an unfilled stub, not the gate: {exc!r}"
    )


# ---------------------------------------------------------------------------
# Contract #9 — injected-gap CLI: a decision finding with no DEC row → exit 1.
# ---------------------------------------------------------------------------


def test_check_exits_1_when_decision_finding_has_no_dec_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: plan §5 injected-gap CLI + §6 single gate + contract #9.

    Inject a ``type: decision`` finding (finding-099) that has NO corresponding DEC row in
    ``MEMORY.md``. ``genome docs check`` must exit 1 and print a violation line (the
    ``[dimension/CODE] location: message`` shape).
    """
    findings = _clean_findings()
    findings["finding-099-undocumented-decision.md"] = _finding(
        number="099",
        type_="decision",
        status="active",
        title="a decision with no DEC row",
    )
    _write_repo(tmp_path, findings=findings, ledger=_ledger(_CLEAN_LEDGER_ROWS))
    result = _run_in_repo(monkeypatch, tmp_path, ["check"])
    _assert_clean_exit(result, 1)
    # The per-violation line shape: [dimension/CODE] location: message.
    assert "[" in result.output
    assert "]" in result.output


def test_check_exits_0_on_clean_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: plan §6 single gate (positive) + contract (OK line, exit 0).

    A clean repo (parseable frontmatter everywhere, cross-resolved ledger, index in sync
    via build-index first) exits 0 with the OK line.
    """
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(_CLEAN_LEDGER_ROWS))
    # Bring the README index into sync first so RETRIEVAL is clean.
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(docs_app, ["build-index"])
    result = CliRunner().invoke(docs_app, ["check"])
    _assert_clean_exit(result, 0)
    assert "OK" in result.output


def test_check_exits_1_on_non_canonical_actor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: plan §5 negative (non-canonical actor) via the CLI + contract #5.

    The gate is enforcement, not a rubber stamp: a ledger with a non-canonical actor makes
    ``genome docs check`` exit 1.
    """
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | active | — | NovelUnmappedActor |"
        " PR #1 | a decision | docs/findings/finding-011-gnomad-three-way-intersection.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    result = _run_in_repo(monkeypatch, tmp_path, ["check"])
    _assert_clean_exit(result, 1)


# ---------------------------------------------------------------------------
# Contract — build-index --no-write is the CI drift guard (exit 1 when stale).
# ---------------------------------------------------------------------------


def test_build_index_no_write_exits_1_when_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: plan §6 RETRIEVAL + contract (``build-index --no-write`` exits 1 when it would change).

    With an empty marker block, ``build-index --no-write`` would change the README, so the
    drift guard exits 1 (and writes nothing).
    """
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(_CLEAN_LEDGER_ROWS))
    readme_path = tmp_path / "docs" / "findings" / "README.md"
    before = readme_path.read_text(encoding="utf-8")
    result = _run_in_repo(monkeypatch, tmp_path, ["build-index", "--no-write"])
    _assert_clean_exit(result, 1)
    assert readme_path.read_text(encoding="utf-8") == before


def test_build_index_no_write_exits_0_when_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: plan §6 RETRIEVAL + contract #8 (in-sync index → no drift).

    After a real ``build-index`` write, a subsequent ``build-index --no-write`` finds the
    index in sync and exits 0 (normalize-then-compare stable).
    """
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(_CLEAN_LEDGER_ROWS))
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(docs_app, ["build-index"])
    result = CliRunner().invoke(docs_app, ["build-index", "--no-write"])
    _assert_clean_exit(result, 0)


# ---------------------------------------------------------------------------
# Task 4 wiring — the sub-app is registered on the real ``genome`` CLI.
# ---------------------------------------------------------------------------


def test_docs_subapp_registered_on_genome_cli() -> None:
    """from: plan Task 4 + §5 ("one add_typer line"; membership-based --help tests).

    The Task-4 ``app.add_typer(docs_app, name="docs")`` wiring registers a ``docs`` group
    on the root CLI. Asserted STRUCTURALLY against ``app.registered_groups`` (not a
    substring of ``--help``) — the help text already contains the literal ``docs/`` from
    ``docs/runbooks/...`` references, so a substring check would be a false positive. This
    fails RED until the wiring lands. (Imported lazily so a collection-time import of
    ``genome.cli`` cannot couple this file's other tests to the DB-importing root CLI.)
    """
    from genome.cli import app  # noqa: PLC0415 — local import keeps the module DB-free

    group_names = {group.name for group in app.registered_groups if group.name is not None}
    assert "docs" in group_names, sorted(group_names)


def test_docs_help_lists_both_subcommands() -> None:
    """from: plan Task 4 (subcommands ``build-index`` + ``check``) + contract CLI surface."""
    result = CliRunner().invoke(docs_app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "build-index" in result.output
    assert "check" in result.output


def test_check_decision_gap_prints_the_capture_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: Stage-3 test-adequacy — the injected-gap check must print the SPECIFIC
    DECISION_WITHOUT_DEC_ROW code, so a different violation can't produce a false pass."""
    findings = _clean_findings()
    findings["finding-099-undocumented-decision.md"] = _finding(
        number="099",
        type_="decision",
        status="active",
        title="a decision with no DEC row",
    )
    _write_repo(tmp_path, findings=findings, ledger=_ledger(_CLEAN_LEDGER_ROWS))
    result = _run_in_repo(monkeypatch, tmp_path, ["check"])
    _assert_clean_exit(result, 1)
    assert "DECISION_WITHOUT_DEC_ROW" in result.output

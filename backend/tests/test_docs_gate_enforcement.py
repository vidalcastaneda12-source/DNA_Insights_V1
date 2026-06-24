"""Anti-theatre substance test for the ``genome docs check`` enforcement gate.

Plan-blind spec source: the approved ``docs-gate-enforcement`` plan §4 Task 2 (the gate is
wired so that ``genome docs check`` is the single decision-tracking enforcement command — it
passes on a clean tree, blocks a seeded-bad tree, and runs to a verdict config-free) + §5
(the anti-theatre tests: clean-pass / seeded-block / config-free), driven against the
**frozen interface contract** only:

* invocation: the console command ``genome docs check`` (no args; it auto-discovers the repo
  root by walking up from ``cwd`` to a ``CLAUDE.md``), run as a subprocess with
  ``cwd=<fixture repo>``;
* exit contract: **0** + ``"docs check: OK — capture + retrieval + lifecycle all hold"`` on a
  clean tree; non-zero + ``"docs check: FAIL — N violation(s)"`` and one
  ``[<dimension>/<CODE>] <location>: <message>`` line per violation otherwise;
* config-free target behaviour: ``genome docs check`` reaches a verdict **without**
  ``APP_DB_PASSPHRASE`` in the environment.

This module is **blind to the implementation diff produced this session** — it reads only the
frozen interface above and the already-existing system-under-test (the ``genome.docs``
sub-app, validator, ledger). It asserts the gate's *observable CLI behaviour*, never an
internal. The clean-fixture builders are mirrored from ``test_docs_validator.py``
(``_readme_with_index`` / ``_write_repo``) so the clean tree genuinely passes the gate rather
than being shaped to any implementation.

Expected initial state is **RED** for the two passphrase-stripped tests
(``test_gate_passes_clean_ledger``, ``test_gate_runs_config_free``): until the config-free
fix lands, ``genome docs check`` crashes in the root CLI callback with a pydantic
``ValidationError`` for the missing ``app_db_passphrase`` before it can reach a verdict. That
crash is exactly what this PR removes; the implementer drives these green.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from genome.docs.model import INDEX_BEGIN_MARKER, INDEX_END_MARKER

# ---------------------------------------------------------------------------
# Subprocess harness — invoke the real ``genome`` console script with cwd inside
# the fixture repo (mirrors the clean-subprocess style of test_docs_no_db_import.py,
# but exercises the frozen `genome docs check` CLI surface end-to-end).
# ---------------------------------------------------------------------------


def _src_root() -> str:
    """Absolute path to ``backend/src`` so the subprocess imports ``genome`` without install."""
    # backend/tests/<this file> -> parents[1] == backend/ -> backend/src
    return str(Path(__file__).resolve().parents[1] / "src")


def _genome_bin() -> str:
    """Resolve the ``genome`` console script (``[project.scripts] genome``)."""
    found = shutil.which("genome")
    if found:
        return found
    # Fall back to the interpreter's own bin dir (the venv that is running pytest).
    return str(Path(sys.executable).parent / "genome")


def _run_docs_check(cwd: Path, *, with_passphrase: bool) -> subprocess.CompletedProcess[str]:
    """Run ``genome docs check`` with ``cwd`` inside the fixture repo.

    When ``with_passphrase`` is False the env has ``APP_DB_PASSPHRASE`` **removed** — the
    config-free contract this PR delivers. ``backend/src`` is on ``PYTHONPATH`` so the
    subprocess imports ``genome`` without an editable install.
    """
    env = {k: v for k, v in os.environ.items() if k != "APP_DB_PASSPHRASE"}
    if with_passphrase:
        # A throwaway value: this is a test fixture passphrase, never a real secret, and it
        # never reaches a database (the gate is pure-filesystem). It only proves the gate's
        # behaviour is independent of the config crash for the seeded-block case.
        env["APP_DB_PASSPHRASE"] = "test-fixture-passphrase-not-a-secret"
    env["PYTHONPATH"] = _src_root()
    return subprocess.run(  # noqa: S603 — fixed argv (resolved console script + literal args)
        [_genome_bin(), "docs", "check"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _run_docs_build_index(cwd: Path, *, with_passphrase: bool) -> subprocess.CompletedProcess[str]:
    """Run ``genome docs build-index`` to bring the fixture README index into sync.

    The clean tree's findings README starts with an EMPTY index marker block; without this
    sync the gate (correctly) reports ``STALE_INDEX`` and the clean case false-fails. This
    mirrors test_docs_cli.py's clean-positive flow (build-index, then check).
    """
    env = {k: v for k, v in os.environ.items() if k != "APP_DB_PASSPHRASE"}
    if with_passphrase:
        env["APP_DB_PASSPHRASE"] = "test-fixture-passphrase-not-a-secret"
    env["PYTHONPATH"] = _src_root()
    return subprocess.run(  # noqa: S603 — fixed argv (resolved console script + literal args)
        [_genome_bin(), "docs", "build-index"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# ---------------------------------------------------------------------------
# Fixture-repo building blocks (real on-disk shapes; finding-013 realism).
# Mirrors test_docs_validator.py::_readme_with_index / _write_repo so the clean
# tree genuinely passes the gate. A COMPLETE clean tree needs (a) CLAUDE.md (so the
# repo root anchors), (b) a MEMORY.md ledger marker block of well-formed rows, and
# (c) a findings README index marker block consistent with the findings present.
# ---------------------------------------------------------------------------

_CLAUDE_MD = "# Project Context — DNA Insights App\n\n## Real-data observations\n"


def _finding(  # noqa: PLR0913 — a fixture builder; each kwarg is one frontmatter key
    *,
    number: str,
    type_: str = "decision",
    status: str = "active",
    supersedes: str = "[]",
    superseded_by: str = "[]",
    title: str = "a finding",
) -> str:
    """Render a finding file: a frontmatter block prepended above its ``# Finding`` H1."""
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
        "Body text below the fence.\n"
    )


def _ledger(rows: str) -> str:
    """Render a ``MEMORY.md`` with header + separator + the supplied data ``rows``.

    The data rows are inserted immediately after the ``|---|`` separator with NO blank line
    between them (the ledger parser stops at the first non-``|`` line), then a single blank
    line before the END marker — matching the real ledger's shape.
    """
    columns = (
        "| DEC | kind | date | status | superseded_by | actors | provenance |"
        " decision | detail-link |\n"
    )
    separator = "|---|---|---|---|---|---|---|---|---|\n"
    return (
        "# MEMORY — decision ledger\n"
        "\n"
        "Insert-then-flip is the only sanctioned transition.\n"
        "\n"
        "<!-- BEGIN decision-ledger -->\n"
        "\n" + columns + separator + rows + "\n"
        "<!-- END decision-ledger -->\n"
    )


def _readme_with_index(body: str = "") -> str:
    """A findings README carrying the generated index marker block (initially empty)."""
    return (
        "# Findings\n"
        "\n"
        "Hand-authored preamble that build-index must preserve.\n"
        "\n"
        f"{INDEX_BEGIN_MARKER}\n"
        f"{body}"
        f"{INDEX_END_MARKER}\n"
        "\n"
        "Hand-authored trailer that build-index must preserve.\n"
    )


def _write_repo(
    root: Path,
    *,
    findings: dict[str, str],
    ledger: str,
) -> None:
    """Materialise a minimal repo: CLAUDE.md, MEMORY.md, docs/findings/*, README."""
    (root / "CLAUDE.md").write_text(_CLAUDE_MD, encoding="utf-8")
    (root / "MEMORY.md").write_text(ledger, encoding="utf-8")
    findings_dir = root / "docs" / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    for name, text in findings.items():
        (findings_dir / name).write_text(text, encoding="utf-8")
    (findings_dir / "README.md").write_text(_readme_with_index(), encoding="utf-8")


# A clean, well-formed two-row supersession ledger (DEC-0001 superseded by DEC-0002),
# whose detail-links point at the two findings the fixture writes, so the gate's
# decision<->DEC-row cross-resolution is satisfied.
_CLEAN_LEDGER_ROWS = (
    "| DEC-0001 | architectural | 2026-05-22 | superseded | DEC-0002 | VSC-User |"
    " finding-011 | gnomAD filter scoped three-way; see CLAUDE.md obs #4 |"
    " docs/findings/finding-011-gnomad-three-way-intersection.md |\n"
    "| DEC-0002 | architectural | 2026-06-21 | active | — | VSC-User |"
    " finding-035 | gnomAD filter narrowed to user_only |"
    " docs/findings/finding-035-gnomad-filter-set-consumer-audit.md |\n"
)


def _clean_findings() -> dict[str, str]:
    """A pair of findings whose frontmatter resolves the DEC supersession edge."""
    return {
        "finding-011-gnomad-three-way-intersection.md": _finding(
            number="011",
            type_="decision",
            status="superseded",
            superseded_by="[finding-035]",
            title="gnomAD three-way intersection",
        ),
        "finding-035-gnomad-filter-set-consumer-audit.md": _finding(
            number="035",
            type_="decision",
            status="active",
            supersedes="[finding-011]",
            title="gnomAD filter-set consumer audit",
        ),
    }


# ---------------------------------------------------------------------------
# Test 1 — clean tree: exit 0 + the OK verdict (substring, NOT equality:
# ~7 DEBUG `annotate.registry.register` lines precede the verdict on stdout).
# Expected RED until the config-free fix lands (the passphrase-stripped subprocess
# currently crashes in the root CLI callback before reaching a verdict).
# ---------------------------------------------------------------------------


def test_gate_passes_clean_ledger(tmp_path: Path) -> None:
    """from: plan §4 Task 2 + §5 (clean-pass) — a COMPLETE clean tree exits 0 with the OK line.

    Build a complete, gate-passing fixture (CLAUDE.md anchor, well-formed cross-resolved
    MEMORY.md ledger, findings README index marker block), bring the index into sync with
    ``build-index``, then assert ``genome docs check`` exits 0 and prints the OK verdict as a
    **substring** of stdout (the gate emits ~7 ``annotate.registry.register`` DEBUG lines to
    stdout before the verdict, so equality / ``.strip() ==`` would false-fail).
    """
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(_CLEAN_LEDGER_ROWS))
    # Bring the README index into sync first so RETRIEVAL is clean (mirrors test_docs_cli.py).
    _run_docs_build_index(tmp_path, with_passphrase=False)
    result = _run_docs_check(tmp_path, with_passphrase=False)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "docs check: OK — capture + retrieval + lifecycle all hold" in result.stdout


# ---------------------------------------------------------------------------
# Test 2 — seeded-bad tree: non-zero exit AND the SPECIFIC DUPLICATE_DEC_ID code
# (bare rc != 0 would false-pass on an unrelated failure). The duplicate check is
# git-independent, so no git init is needed in the fixture. Runs WITH a passphrase so
# the seeded-block proof is isolated from the pre-fix config crash.
# ---------------------------------------------------------------------------


def test_gate_blocks_seeded_bad_ledger(tmp_path: Path) -> None:
    """from: plan §4 Task 2 + §5 (seeded-block) — a seeded DUPLICATE_DEC_ID row blocks.

    Seed the duplicate **contiguously**: two ``DEC-0001`` rows back-to-back with NO blank line
    between them (the ledger parser ``iter_data_rows`` stops at the first non-``|`` line, so a
    row placed after a blank line would be silently unparsed and the code would never fire).
    Assert the gate exits **non-zero** AND prints the SPECIFIC ``DUPLICATE_DEC_ID`` code (a
    bare ``rc != 0`` would false-pass on any unrelated failure).
    """
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | active | — | VSC-User |"
        " finding-011 | first decision | docs/findings/finding-011.md |\n"
        "| DEC-0001 | tactical | 2026-05-23 | active | — | VSC-User |"
        " finding-035 | duplicate id, no blank line above | docs/findings/finding-035.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    result = _run_docs_check(tmp_path, with_passphrase=True)
    assert result.returncode != 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "DUPLICATE_DEC_ID" in result.stdout


# ---------------------------------------------------------------------------
# Test 3 — config-free: the gate reaches a verdict (exit 0 or 1) without a
# crash when APP_DB_PASSPHRASE is absent. Discriminating assertion: a `docs check:`
# verdict line is present and NO pydantic `ValidationError` / `app_db_passphrase`
# traceback. Expected RED until the config-free fix lands.
# ---------------------------------------------------------------------------


def test_gate_runs_config_free(tmp_path: Path) -> None:
    """from: plan §4 Task 2 + §5 (config-free) — ``genome docs check`` reaches a verdict with
    ``APP_DB_PASSPHRASE`` absent from the env.

    The contract: the gate is pure-filesystem and must run on a fresh checkout with no DB
    config. The discriminating assertion is that the combined stdout/stderr carries a
    ``docs check:`` verdict line and does NOT carry a config crash signature
    (``ValidationError`` / ``app_db_passphrase``) — a non-crash exit (0 or 1) is the pass; the
    pre-fix state crashes in the root CLI callback with a pydantic ``ValidationError`` for the
    missing ``app_db_passphrase`` before any verdict, so this is RED until the fix lands.
    """
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(_CLEAN_LEDGER_ROWS))
    result = _run_docs_check(tmp_path, with_passphrase=False)
    combined = result.stdout + result.stderr
    # Reached a verdict (not a crash): exit code is a deliberate gate verdict, 0 or 1.
    assert result.returncode in {0, 1}, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    # And did so without the config-validation crash signature.
    assert "ValidationError" not in combined, combined
    assert "app_db_passphrase" not in combined, combined
    # The positive discriminator: an actual `docs check:` verdict line was printed.
    assert "docs check:" in combined, combined

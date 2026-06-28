"""Enforcement tests for the ``genome workflows check`` reversal-gate (C2+D Phase 2 / finding-034).

Mirrors ``test_docs_gate_enforcement.py``. The gate runs clean on the real in-repo workflows,
**catches** a seeded seam-drift and a seeded schema regression (the anti-theatre falsifier — it
must do more than exit 0), and is fail-closed on a missing file or an unlocatable seam. The CLI
surface reaches a verdict config-free (no ``APP_DB_PASSPHRASE``).

The fixtures copy the *real* checked-in workflows into a tmp repo, so the clean tree genuinely
passes the gate rather than being shaped to the implementation; mutations are applied to those
copies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from genome.workflows import check
from genome.workflows.model import (
    SCHEMA_MISSING_TYPE,
    SEAM_DRIFT,
    SEAM_NOT_LOCATED,
    WORKFLOW_FILE_MISSING,
)
from genome.workflows.seam import normalize_seam

REPO_ROOT = Path(__file__).resolve().parents[2]  # backend/tests/<file> -> repo root
WORKFLOW_STEMS = ("plan-phase", "implement-review", "close")


def _seed_repo(tmp_path: Path, *, stems: tuple[str, ...] = WORKFLOW_STEMS) -> Path:
    """Build a minimal fixture repo: a CLAUDE.md marker + copies of the real workflow files."""
    (tmp_path / "CLAUDE.md").write_text("# fixture repo\n", encoding="utf-8")
    wf_dir = tmp_path / ".claude" / "workflows"
    wf_dir.mkdir(parents=True)
    for stem in stems:
        shutil.copy(REPO_ROOT / ".claude" / "workflows" / f"{stem}.js", wf_dir / f"{stem}.js")
    return tmp_path


def _genome_bin() -> str:
    """Resolve the ``genome`` console script (``[project.scripts] genome``)."""
    found = shutil.which("genome")
    if found:
        return found
    return str(Path(sys.executable).parent / "genome")


def _run_cli(cwd: Path, *, with_passphrase: bool) -> subprocess.CompletedProcess[str]:
    """Run ``genome workflows check`` with ``cwd`` inside the fixture repo.

    With ``with_passphrase`` False the env has ``APP_DB_PASSPHRASE`` removed — the gate must still
    reach a verdict (it is config-free, like ``genome docs check``).
    """
    env = {k: v for k, v in os.environ.items() if k != "APP_DB_PASSPHRASE"}
    if with_passphrase:
        env["APP_DB_PASSPHRASE"] = "test-fixture-passphrase-not-a-secret"
    env["PYTHONPATH"] = str(REPO_ROOT / "backend" / "src")
    return subprocess.run(  # noqa: S603 — fixed argv (resolved console script + literal args)
        [_genome_bin(), "workflows", "check"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# --- direct check() behaviour ---------------------------------------------------------------


def test_gate_passes_on_the_real_repo() -> None:
    """Anti-theatre: the gate runs clean on the actual in-repo workflows (green on reality)."""
    report = check(REPO_ROOT)
    assert report.ok, [f"[{v.code}] {v.location}: {v.message}" for v in report.violations]


def test_gate_passes_on_copied_workflows(tmp_path: Path) -> None:
    report = check(_seed_repo(tmp_path))
    assert report.ok, [f"[{v.code}] {v.location}: {v.message}" for v in report.violations]


def test_gate_catches_seeded_seam_drift(tmp_path: Path) -> None:
    """Mutating one file's seam logic (the retry bound) must trip SEAM_DRIFT — it CATCHES drift."""
    repo = _seed_repo(tmp_path)
    target = repo / ".claude" / "workflows" / "close.js"
    body = target.read_text(encoding="utf-8")
    mutated = body.replace("<= RETRY_LIMIT; attempt++", "<= RETRY_LIMIT + 1; attempt++", 1)
    assert mutated != body, "seam mutation anchor not found"
    target.write_text(mutated, encoding="utf-8")
    report = check(repo)
    assert not report.ok
    assert SEAM_DRIFT in {v.code for v in report.violations}


def test_gate_catches_seeded_schema_regression(tmp_path: Path) -> None:
    """A SCHEMAS entry reverted to the bare ``{required}`` shape must trip SCHEMA_MISSING_TYPE."""
    repo = _seed_repo(tmp_path)
    target = repo / ".claude" / "workflows" / "close.js"
    body = target.read_text(encoding="utf-8")
    mutated = body.replace(
        "repoSweep: { type: 'object', properties: { fruit: {} },"
        " required: ['fruit'], additionalProperties: true }",
        "repoSweep: { required: ['fruit'] }",
        1,
    )
    assert mutated != body, "schema mutation anchor not found"
    target.write_text(mutated, encoding="utf-8")
    report = check(repo)
    assert not report.ok
    assert SCHEMA_MISSING_TYPE in {v.code for v in report.violations}


def test_gate_fails_closed_on_missing_file(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path, stems=("plan-phase", "implement-review"))  # close.js absent
    report = check(repo)
    assert not report.ok
    assert WORKFLOW_FILE_MISSING in {v.code for v in report.violations}


def test_gate_fails_closed_on_missing_sentinel(tmp_path: Path) -> None:
    """An unlocatable seam (sentinel removed) is a violation, never a silent fallback."""
    repo = _seed_repo(tmp_path)
    target = repo / ".claude" / "workflows" / "plan-phase.js"
    body = target.read_text(encoding="utf-8")
    stripped = body.replace("// agent-seam:start", "// (sentinel removed)", 1)
    assert stripped != body, "start sentinel not found"
    target.write_text(stripped, encoding="utf-8")
    report = check(repo)
    assert not report.ok
    assert SEAM_NOT_LOCATED in {v.code for v in report.violations}


def test_normalize_seam_equates_label_and_linewrap_only() -> None:
    """The two legit per-file dimensions (name label, string-concat line-wrap) normalize away."""
    plan_phase = "log(`[plan-phase] x`);\n  const p = `a ` + `Plan-phase b`;"
    implement_review = "log(`[implement-review] x`);\n  const p = `a implement-review b`;"
    assert normalize_seam(plan_phase, "plan-phase") == normalize_seam(
        implement_review, "implement-review"
    )


# --- CLI surface ----------------------------------------------------------------------------


def test_cli_passes_clean_config_free(tmp_path: Path) -> None:
    """Clean fixture exits 0 with the OK line, with no APP_DB_PASSPHRASE in the env."""
    result = _run_cli(_seed_repo(tmp_path), with_passphrase=False)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "workflows check: OK" in result.stdout


def test_cli_blocks_seeded_drift(tmp_path: Path) -> None:
    """Seeded seam-drift makes the CLI exit non-zero and name the SEAM_DRIFT code."""
    repo = _seed_repo(tmp_path)
    target = repo / ".claude" / "workflows" / "close.js"
    body = target.read_text(encoding="utf-8")
    target.write_text(
        body.replace("<= RETRY_LIMIT; attempt++", "<= RETRY_LIMIT + 1; attempt++", 1),
        encoding="utf-8",
    )
    result = _run_cli(repo, with_passphrase=True)
    assert result.returncode != 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert SEAM_DRIFT in result.stdout

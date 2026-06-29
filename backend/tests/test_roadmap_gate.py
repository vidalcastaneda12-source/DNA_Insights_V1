"""Enforcement tests for the ``genome roadmap check`` source-of-truth gate (finding-042 / DEC-0125).

Mirrors ``test_workflows_gate.py``. The gate runs clean on the real in-repo ROADMAP.md, **catches**
a seeded missing-id / duplicate-id / dangling-reference (the anti-theatre falsifier — it must do
more than exit 0), exempts the machine-managed B2-SUBSCOPES region and indented sub-bullets, and is
fail-closed when ROADMAP.md is absent. The CLI surface reaches a verdict config-free (no
``APP_DB_PASSPHRASE``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from genome.roadmap import check
from genome.roadmap.model import DANGLING_REF, DUPLICATE_ID, MISSING_ID, ROADMAP_FILE_MISSING

REPO_ROOT = Path(__file__).resolve().parents[2]  # backend/tests/<file> -> repo root


def _seed(tmp_path: Path, roadmap_text: str, *, findings: dict[str, str] | None = None) -> Path:
    """Build a minimal fixture repo: CLAUDE.md marker + ROADMAP.md + empty ledger/changelog."""
    (tmp_path / "CLAUDE.md").write_text("# fixture repo\n", encoding="utf-8")
    (tmp_path / "ROADMAP.md").write_text(roadmap_text, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# ledger\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("# changelog\n", encoding="utf-8")
    fdir = tmp_path / "docs" / "findings"
    fdir.mkdir(parents=True)
    for name, text in (findings or {}).items():
        (fdir / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_real_roadmap_passes() -> None:
    """The actual checked-in ROADMAP.md satisfies the gate (the clean tree is genuinely green)."""
    report = check(REPO_ROOT)
    assert report.ok, [(v.code, v.location) for v in report.violations]


def test_missing_id_caught(tmp_path: Path) -> None:
    report = check(_seed(tmp_path, "## Phase 6\n- [ ] do a thing\n"))
    assert not report.ok
    assert any(v.code == MISSING_ID for v in report.violations)


def test_duplicate_id_caught(tmp_path: Path) -> None:
    rm = "## Phase 6\n- [ ] RM-aaaaaaa — one\n- [x] RM-aaaaaaa — two\n"
    report = check(_seed(tmp_path, rm))
    assert any(v.code == DUPLICATE_ID for v in report.violations)


def test_dangling_reference_caught(tmp_path: Path) -> None:
    rm = "## Phase 6\n- [ ] RM-aaaaaaa — one\n"
    findings = {"finding-001-x.md": "This cites RM-bbbbbbb which is not defined.\n"}
    report = check(_seed(tmp_path, rm, findings=findings))
    assert any(v.code == DANGLING_REF for v in report.violations)


def test_resolved_reference_ok(tmp_path: Path) -> None:
    rm = "## Phase 6\n- [ ] RM-aaaaaaa — one\n"
    findings = {"finding-001-x.md": "See RM-aaaaaaa for detail.\n"}
    report = check(_seed(tmp_path, rm, findings=findings))
    assert report.ok, [(v.code, v.location) for v in report.violations]


def test_managed_subscopes_region_exempt(tmp_path: Path) -> None:
    rm = (
        "## Sub Project B2\n"
        "<!-- B2-SUBSCOPES:BEGIN -->\n"
        "- [ ] origin-s1 — auto-written sub-scope (no RM- id, writer-owned)\n"
        "<!-- B2-SUBSCOPES:END -->\n"
    )
    report = check(_seed(tmp_path, rm))
    assert report.ok, [(v.code, v.location) for v in report.violations]


def test_indented_subbullet_exempt(tmp_path: Path) -> None:
    rm = "## Phase 6\n- [ ] RM-aaaaaaa — parent\n    - [ ] nested detail (no id required)\n"
    report = check(_seed(tmp_path, rm))
    assert report.ok, [(v.code, v.location) for v in report.violations]


def test_missing_roadmap_is_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# fixture repo\n", encoding="utf-8")
    report = check(tmp_path)
    assert not report.ok
    assert any(v.code == ROADMAP_FILE_MISSING for v in report.violations)


def _genome_bin() -> str:
    """Resolve the ``genome`` console script (``[project.scripts] genome``)."""
    found = shutil.which("genome")
    if found:
        return found
    return str(Path(sys.executable).parent / "genome")


def _run_cli(cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``genome roadmap check`` in ``cwd`` with APP_DB_PASSPHRASE removed (config-free)."""
    env = {k: v for k, v in os.environ.items() if k != "APP_DB_PASSPHRASE"}
    return subprocess.run(  # noqa: S603 — fixed argv (resolved console script + literal args)
        [_genome_bin(), "roadmap", "check"],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_cli_clean_exits_zero_config_free(tmp_path: Path) -> None:
    repo = _seed(tmp_path, "## Phase 6\n- [ ] RM-aaaaaaa — one\n")
    result = _run_cli(repo)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_cli_violation_exits_nonzero(tmp_path: Path) -> None:
    repo = _seed(tmp_path, "## Phase 6\n- [ ] missing an id\n")
    result = _run_cli(repo)
    assert result.returncode != 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

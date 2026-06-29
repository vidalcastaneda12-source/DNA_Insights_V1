"""No-DB-import guard — ``import genome.roadmap`` pulls in no ``genome.db`` (finding-042).

The source-of-truth gate is pure-filesystem text inspection; like ``genome docs`` /
``genome workflows`` it must run on a fresh checkout with no DuckDB / SQLCipher built. The probe
runs in a CLEAN subprocess (a fresh interpreter, not this already-DB-tainted test process where
sibling tests may have imported ``genome.db``) so it genuinely proves the ``genome.roadmap`` import
graph is DB-free.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
from pathlib import Path


def _src_root() -> str:
    """Absolute path to ``backend/src`` so the subprocess can import ``genome`` without install."""
    return str(Path(__file__).resolve().parents[1] / "src")


def _run_probe(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a clean child interpreter with ``backend/src`` on the path."""
    env = {
        "PYTHONPATH": _src_root(),
        "PATH": sysconfig.get_path("scripts") + ":/usr/bin:/bin",
    }
    return subprocess.run(  # noqa: S603 — fixed argv (this interpreter + a literal probe)
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_import_genome_roadmap_pulls_in_no_db_module() -> None:
    """``import genome.roadmap`` must not transitively import any ``genome.db`` module."""
    probe = (
        "import genome.roadmap, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db')]; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_roadmap_submodules_are_db_free() -> None:
    """The DB-free guarantee holds for the individual leaf modules."""
    probe = (
        "import genome.roadmap.cli, genome.roadmap.validator, genome.roadmap.model, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db')]; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

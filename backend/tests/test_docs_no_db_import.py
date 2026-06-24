"""No-DB-import guard — ``import genome.docs`` pulls in no ``genome.db`` (plan Task 3).

Plan-blind spec source: the approved ``decision-tracking-leak-fix`` plan §5 ("No-DB-import"
— importing ``genome.docs`` / running ``genome docs check`` pulls in no DB module and needs
no built database) + Task 3 (the lazy-import / import-time-coupling fix) + §3 constraint
(``genome docs`` carries no DB import dependency), and the frozen interface contract
(behavioural contract #10, with the exact probe). The probe is spec-given verbatim — it does
not read any implementation body.

The check runs in a CLEAN subprocess (a fresh interpreter, not this already-DB-tainted test
process, where ``genome.db`` may have been imported by sibling tests) so it actually proves
the import graph of ``genome.docs`` is DB-free, not merely that it happened to be loaded.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
from pathlib import Path


def _src_root() -> str:
    """Absolute path to ``backend/src`` so the subprocess can import ``genome`` without install."""
    # backend/tests/<this file> → parents[1] == backend/ → backend/src
    return str(Path(__file__).resolve().parents[1] / "src")


def _run_probe(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a clean child interpreter with ``backend/src`` on the path."""
    env = {
        "PYTHONPATH": _src_root(),
        # Keep PATH so the interpreter's own shared libs resolve.
        "PATH": sysconfig.get_path("scripts") + ":/usr/bin:/bin",
    }
    return subprocess.run(  # noqa: S603 — fixed argv (this interpreter + a literal probe)
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Contract #10 — the spec-given probe (verbatim from the interface contract).
# ---------------------------------------------------------------------------


def test_import_genome_docs_pulls_in_no_db_module() -> None:
    """from: plan §5 no-DB-import + Task 3 + contract #10 (verbatim probe).

    In a clean interpreter, ``import genome.docs`` must not transitively import any
    ``genome.db`` module. The probe asserts no loaded module name starts with ``genome.db``;
    a non-zero exit (the assert firing, or an ImportError requiring a DB driver) is the
    failure.
    """
    probe = (
        "import genome.docs, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db')]; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_import_genome_docs_submodules_are_db_free() -> None:
    """from: plan §5 no-DB-import + §3 ("any module it pulls in") + contract #10.

    The same DB-free guarantee holds for the individual leaf modules the gate exercises
    (``model``/``frontmatter``/``ledger``/``validator``/``index``/``cli``) — importing the
    package's CLI entrypoint must not drag in ``genome.db`` either.
    """
    probe = (
        "import genome.docs.cli, genome.docs.validator, genome.docs.index, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db')]; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

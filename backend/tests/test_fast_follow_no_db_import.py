"""No-DB-import guard — ``import genome.fast_follow`` pulls in no ``genome.db``.

Plan-blind spec source: synthesized-plan §3 constraint ("DB-free core (clean-subprocess
guard, like verify_gate)"), §4 (the seven-module set "all DB-free, one concern each, all
under the no_db_import guard"), §5 test list item 1 ("test_fast_follow_no_db_import.py —
clean-subprocess: no genome.db in fast_follow + submodules"), R1 (the DB-free guarantee is
carried by THIS package-local clean-subprocess test, which imports ``genome.fast_follow.*``
DIRECTLY, not via the already-DB-tainted ``genome.cli``), and the frozen-interface
``__init__`` docstring invariant ("Docstring asserts the package imports no genome.db").

The probe runs in a CLEAN subprocess (a fresh interpreter, not this already-DB-tainted test
process where ``genome.db`` may have been imported by sibling tests) so it actually proves the
import graph of ``genome.fast_follow`` is DB-free, not merely that it happened to be loaded.

Unlike the other fast_follow test files (RED against the ``raise NotImplementedError`` stubs),
THIS file is expected GREEN from the start: importing the modules does not call the stubbed
bodies, so the structural guard holds the moment the modules exist.
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


def test_import_genome_fast_follow_pulls_in_no_db_module() -> None:
    """from: plan §3 (DB-free core) + §5 item 1 + R1 + frozen __init__ invariant.

    In a clean interpreter, ``import genome.fast_follow`` must not transitively import any
    ``genome.db`` module. The probe asserts no loaded module name starts with ``genome.db``; a
    non-zero exit (the assert firing, or an ImportError requiring a DB driver) is the failure.
    """
    probe = (
        "import genome.fast_follow, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db')]; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_import_genome_fast_follow_submodules_are_db_free() -> None:
    """from: plan §5 item 1 ("fast_follow + submodules") + §4 (the seven-module set) + R1.

    The same DB-free guarantee holds for each leaf submodule the package exposes
    (``model`` / ``classifier`` / ``loop`` / ``persistence`` / ``formatter`` / ``cli``) —
    importing any of them, including the CLI entrypoint that eager-registers the typer sub-app
    (R1), must not drag in ``genome.db``.
    """
    probe = (
        "import genome.fast_follow.model, genome.fast_follow.classifier, "
        "genome.fast_follow.loop, genome.fast_follow.persistence, "
        "genome.fast_follow.formatter, genome.fast_follow.cli, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db')]; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

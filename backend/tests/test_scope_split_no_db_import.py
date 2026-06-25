"""No-DB-import guard — ``import genome.scope_split`` pulls in no ``genome.db``.

Plan-blind spec source: SYNTHESIZED-PLAN §3 constraint ("DB-free core (clean-subprocess
test)"), §4 step 2/9 (the seven-module set, all DB-free, under the no_db_import guard), §5
test list item 1 ("test_scope_split_no_db_import.py — clean-subprocess probe … GREEN from
freeze"), and the frozen ``__init__`` docstring invariant ("This package imports no
genome.db … must run on a fresh checkout with no DuckDB / SQLCipher built").

The probe runs in a CLEAN subprocess (a fresh interpreter, not this already-DB-tainted test
process where ``genome.db`` may have been imported by sibling tests) so it actually proves the
import graph of ``genome.scope_split`` is DB-free, not merely that it happened to be loaded.

Unlike the behavioral test files (RED against ``raise NotImplementedError`` stubs), THIS file
is GREEN from interface-freeze: importing the modules does not call the stubbed bodies, so the
structural guard holds the moment the modules exist.

test->spec provenance: enforces SYNTHESIZED-PLAN §3 "DB-free core" + the __init__ DB-free
invariant. Asserts the BEHAVIOR (no genome.db in the import graph), not a stub raise.
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


def test_import_genome_scope_split_pulls_in_no_db_module() -> None:
    """from: plan §3 (DB-free core) + §5 item 1 + frozen __init__ invariant.

    In a clean interpreter, ``import genome.scope_split`` must not transitively import any
    ``genome.db`` module. The probe asserts no loaded module name starts with ``genome.db``; a
    non-zero exit (the assert firing, or an ImportError requiring a DB driver) is the failure.
    """
    probe = (
        "import genome.scope_split, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db')]; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_import_genome_scope_split_submodules_are_db_free() -> None:
    """from: plan §5 item 1 ("each leaf submodule") + §4 (the seven-module set).

    The same DB-free guarantee holds for each leaf submodule the package exposes
    (``model`` / ``graph`` / ``splitter`` / ``formatter`` / ``roadmap_writer`` / ``cli``) —
    importing any of them, including the CLI entrypoint that eager-registers the typer sub-app,
    must not drag in ``genome.db``.
    """
    probe = (
        "import genome.scope_split.model, genome.scope_split.graph, "
        "genome.scope_split.splitter, genome.scope_split.formatter, "
        "genome.scope_split.roadmap_writer, genome.scope_split.cli, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db')]; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

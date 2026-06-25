"""No-DB-import guard — ``import genome.verify_gate`` pulls in no ``genome.db``.

Plan-blind spec source: synthesized-plan §4.1 ("No genome.db import") + §3 constraint
("DB-free core (no genome.db; clean-subprocess guard)") + §5 test list item 1
("test_verify_gate_no_db_import.py: core + submodules pull in no genome.db (clean
subprocess)"), and the frozen interface contract (the package ``__init__`` docstring's
"This package imports no genome.db" invariant). The probe is spec-given — it reads no
implementation body.

The check runs in a CLEAN subprocess (a fresh interpreter, not this already-DB-tainted
test process, where ``genome.db`` may have been imported by sibling tests) so it actually
proves the import graph of ``genome.verify_gate`` is DB-free, not merely that it happened
to be loaded. The two-row merge audit (``write_merge_audit``) deliberately lives in
``genome.privacy.external_client``, NOT here, so the core never reaches a DB — this guard
locks that boundary in place.

Unlike the other six verify-gate test files (which are RED against the
``raise NotImplementedError`` stubs), THIS file is expected GREEN from the start: it is the
structural guard, not a behaviour-of-the-stub test — importing the modules does not call
the stubbed bodies.
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


def test_import_genome_verify_gate_pulls_in_no_db_module() -> None:
    """from: plan §4.1 (no genome.db import) + §5 item 1 + frozen __init__ invariant.

    In a clean interpreter, ``import genome.verify_gate`` must not transitively import any
    ``genome.db`` module. The probe asserts no loaded module name starts with ``genome.db``;
    a non-zero exit (the assert firing, or an ImportError requiring a DB driver) is the
    failure.
    """
    probe = (
        "import genome.verify_gate, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db')]; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_import_genome_verify_gate_submodules_are_db_free() -> None:
    """from: plan §5 item 1 ("core + submodules") + frozen interface (the 4 submodules).

    The same DB-free guarantee holds for each leaf submodule the gate exposes
    (``model`` / ``verdict`` / ``formatter`` / ``cli``) — importing any of them, including
    the CLI entrypoint, must not drag in ``genome.db``.
    """
    probe = (
        "import genome.verify_gate.model, genome.verify_gate.verdict, "
        "genome.verify_gate.formatter, genome.verify_gate.cli, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db')]; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

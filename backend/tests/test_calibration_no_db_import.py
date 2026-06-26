"""No-DB / no-config import guard — ``import genome.calibration`` stays fresh-checkout-clean.

from: §5 test #1 (test_calibration_no_db_import.py) + the frozen ``__init__`` invariant
("This package imports **no** genome.db **and no** genome.config — must run on a fresh
checkout with no DuckDB / SQLCipher built and no Settings loaded"). Mirrors the
test_scope_split_no_db_import.py clean-subprocess pattern; the **no-genome.config** clause is
load-bearing (``Settings`` needs ``app_db_passphrase``, so a transitive config import would
break a fresh checkout / a passphrase-less CI).

The probe runs in a CLEAN child interpreter (not this already-DB-tainted test process where a
sibling test may have imported ``genome.db``) so it proves the *import graph* of
``genome.calibration`` is DB-free and config-free, not merely that it happened not to load.

Unlike the behavioral files (RED against ``raise NotImplementedError`` stubs), THIS file is
GREEN from interface-freeze: importing the modules never calls a stubbed body, so the
structural guarantee holds the moment the modules exist.

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
from pathlib import Path

#: The eight leaf submodules ``genome.calibration`` exposes — every one must be DB-free and
#: config-free, including the ``cli`` entrypoint the root ``genome`` app eager-registers.
_LEAF_MODULES = (
    "genome.calibration.model",
    "genome.calibration.backtest",
    "genome.calibration.ratchet",
    "genome.calibration.persistence",
    "genome.calibration.accuracy",
    "genome.calibration.commit_plan",
    "genome.calibration.formatter",
    "genome.calibration.cli",
)


def _src_root() -> str:
    """Absolute path to ``backend/src`` so the child can import ``genome`` without an install."""
    # backend/tests/<this file> -> parents[1] == backend/ -> backend/src
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


def _leaked_expr() -> str:
    """A child-side expression collecting any leaked ``genome.db`` / ``genome.config`` module."""
    return (
        "leaked = [m for m in sys.modules if m.startswith('genome.db') "
        "or m == 'genome.config' or m.startswith('genome.config.')]; "
        "assert not leaked, leaked"
    )


def test_import_genome_calibration_pulls_in_no_db_and_no_config() -> None:
    """from: §5 #1 + frozen __init__ invariant (no genome.db AND no genome.config).

    In a clean interpreter, ``import genome.calibration`` must not transitively import any
    ``genome.db`` module *or* ``genome.config`` (the pydantic Settings). A non-zero exit (the
    assert firing, or an ImportError needing a DB driver / a passphrase) is the failure.
    """
    probe = "import genome.calibration, sys; " + _leaked_expr()
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_import_genome_calibration_submodules_are_db_and_config_free() -> None:
    """from: §5 #1 ("each leaf submodule") + the frozen DB-free / config-free invariant.

    The same guarantee holds for every leaf submodule the package exposes — importing any of
    them (including the ``cli`` entrypoint that eager-registers the typer sub-app) must not drag
    in ``genome.db`` or ``genome.config``.
    """
    probe = "import " + ", ".join(_LEAF_MODULES) + ", sys; " + _leaked_expr()
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

"""Fresh-checkout guard — the ``genome`` console script imports with no SQLCipher built.

``genome docs check`` is meant to run on a fresh checkout that has NOT built pysqlcipher3
(CLAUDE.md "Environment requirements" — SQLCipher+FTS5 is a custom from-source build). The
``genome.docs`` package is already DB-free (``test_docs_no_db_import``); this locks the harder
guarantee that the **root** ``genome.cli:app`` console script also loads without pysqlcipher3, so
``genome docs ...`` is reachable on a fresh checkout (the decision-tracking-followups scope).

The probe runs in a clean child interpreter with pysqlcipher3 **stubbed absent**. pysqlcipher3 IS
installed in this environment, so the stub — executed as the probe's FIRST statement, before any
``genome.*`` import — is what reproduces a fresh checkout. The assertion target is the
**pysqlcipher3-bearing module** (``genome.db.sqlite_conn``), NOT "no ``genome.db``": ``genome.cli``
legitimately imports ``genome.db.duckdb_conn`` (a clean duckdb wheel) at module scope; only
pysqlcipher3 is the fresh-checkout blocker.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
from pathlib import Path

# The stub MUST be the first statement of every probe, before any `genome.*` import.
_STUB = "import sys; sys.modules['pysqlcipher3'] = None\n"


def _src_root() -> str:
    """Absolute path to ``backend/src`` so the child can import ``genome`` without install."""
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


def test_stub_reproduces_a_fresh_checkout() -> None:
    """Guard the guard: with the stub in place, importing the pysqlcipher3-bearing module RAISES.

    pysqlcipher3 is installed here, so without the stub the import would succeed and every probe
    below would false-pass. This asserts the stub actually disables pysqlcipher3, so a future edit
    can't silently neuter the regression lock.
    """
    probe = _STUB + (
        "import importlib\n"
        "try:\n"
        "    importlib.import_module('genome.db.sqlite_conn')\n"
        "except ModuleNotFoundError:\n"
        "    print('stub effective')\n"
        "else:\n"
        "    raise AssertionError('stub did not take effect: pysqlcipher3 still importable')\n"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "stub effective" in result.stdout


def test_console_script_imports_without_pysqlcipher3() -> None:
    """``from genome.cli import app`` loads with pysqlcipher3 absent; ``docs`` is registered.

    sqlite_conn must be absent (the pysqlcipher3-bearing module); duckdb_conn must be present (a
    clean wheel, a legitimate residual). This is the headline regression lock — RED before the
    import-relocation fix, GREEN after.
    """
    probe = _STUB + (
        "import sys\n"
        "from genome.cli import app\n"
        "assert 'genome.db.sqlite_conn' not in sys.modules, 'sqlite_conn leaked into genome.cli'\n"
        "assert 'genome.db.duckdb_conn' in sys.modules, 'duckdb_conn should load (clean wheel)'\n"
        "groups = {g.name for g in app.registered_groups if g.name}\n"
        "assert 'docs' in groups, f'docs group not registered: {sorted(groups)}'\n"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_genome_db_package_imports_without_pysqlcipher3() -> None:
    """``import genome.db`` (+ ``duckdb_connection``) works with pysqlcipher3 absent and does not
    load ``sqlite_conn`` — proves the package-init keystone (the re-export was dropped)."""
    probe = _STUB + (
        "import sys, genome.db\n"
        "assert genome.db.duckdb_connection.__name__ == 'duckdb_connection'\n"
        "assert 'genome.db.sqlite_conn' not in sys.modules, 'sqlite_conn leaked'\n"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_docs_help_via_root_app_without_pysqlcipher3() -> None:
    """``genome docs --help`` through the ROOT app loads and exits 0 with pysqlcipher3 absent —
    the real user-facing surface is reachable on a fresh checkout."""
    probe = _STUB + (
        "from typer.testing import CliRunner\n"
        "from genome.cli import app\n"
        "result = CliRunner().invoke(app, ['docs', '--help'])\n"
        "assert result.exit_code == 0, result.output\n"
        "assert 'build-index' in result.output\n"
        "assert 'check' in result.output\n"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

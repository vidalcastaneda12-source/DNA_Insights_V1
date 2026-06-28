"""No-DB-import guard — ``import genome.campaign`` pulls in no ``genome.db`` / ``genome.config``.

Spec source: SYNTHESIZED-PLAN §3 constraint ("DB-free core … imports NO genome.db AND NO
genome.config; persistence uses hard-coded Path('data/campaign/...'), never get_settings") + §5
item 1 (clean-subprocess probe over each leaf submodule) + the frozen ``__init__`` DB-free
invariant. Mirrors ``test_scope_split_no_db_import.py``.

The probe runs in a CLEAN subprocess (a fresh interpreter, not this already-DB-tainted test
process where ``genome.db`` may have been imported by sibling tests) so it actually proves the
import graph of ``genome.campaign`` is DB-free, not merely that it happened to be loaded.

GREEN from creation — importing the modules does not exercise their bodies, so the structural
guarantee holds the moment the modules exist.
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


def test_import_genome_campaign_pulls_in_no_db_or_config_module() -> None:
    """from: §3 (DB-free / no-settings core) + §5 item 1 + the frozen __init__ invariant.

    In a clean interpreter, ``import genome.campaign`` must not transitively import any
    ``genome.db`` module nor ``genome.config``; a non-zero exit (the assert firing, or an
    ImportError requiring a DB driver) is the failure.
    """
    probe = (
        "import genome.campaign, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db') or m == 'genome.config']; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_import_genome_campaign_submodules_are_db_free() -> None:
    """from: §5 ('each leaf submodule') + §4 (model/state_machine/persistence/formatter/cli).

    The same guarantee holds for each leaf submodule — including the CLI entrypoint that
    eager-registers the typer sub-app — none may drag in ``genome.db`` or ``genome.config``.
    """
    probe = (
        "import genome.campaign.model, genome.campaign.state_machine, "
        "genome.campaign.persistence, genome.campaign.formatter, genome.campaign.cli, sys; "
        "leaked = [m for m in sys.modules if m.startswith('genome.db') or m == 'genome.config']; "
        "assert not leaked, leaked"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

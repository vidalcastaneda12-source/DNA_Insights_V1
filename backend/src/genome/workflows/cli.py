"""Typer subcommands for ``genome workflows`` — the engine-primary CLI (C2+D Phase 2).

One command:

* ``workflows check`` — the fail-closed reversal-gate over the three self-contained per-scope-team
  workflows (``.claude/workflows/{plan-phase,implement-review,close}.js``): seam-drift +
  schema-validity. Prints each violation and exits non-zero on any.

**No database import.** This module (and everything it pulls in) imports no :mod:`genome.db`, so
``genome workflows check`` runs on a fresh checkout with no DuckDB / SQLCipher built — the same
DB-free guarantee as ``genome docs``.
"""

from __future__ import annotations

from pathlib import Path

import typer

from genome.workflows.validator import check

workflows_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Engine-primary workflow tooling: the fail-closed reversal-gate (seam-drift + "
        "schema-validity) over the three per-scope-team dynamic workflows."
    ),
)


def _repo_root() -> Path:
    """Locate the repo root by walking up from cwd to the first dir holding CLAUDE.md.

    ``genome workflows`` inspects tracked JS under ``.claude/workflows/``, not the runtime
    ``data/`` dir, so it anchors on the repo marker. Raises ``typer.BadParameter`` outside the repo.
    """
    start = Path.cwd().resolve()
    for candidate in (start, *start.parents):
        if (candidate / "CLAUDE.md").is_file():
            return candidate
    msg = "`genome workflows` must run inside the repo (no CLAUDE.md found walking up from cwd)"
    raise typer.BadParameter(msg)


@workflows_app.command("check")
def check_cmd() -> None:
    """Run the fail-closed reversal-gate; exit non-zero on any violation."""
    report = check(_repo_root())
    for violation in report.violations:
        typer.echo(f"[{violation.code}] {violation.location}: {violation.message}")
    if not report.ok:
        typer.echo(f"workflows check: FAIL — {len(report.violations)} violation(s)")
        raise typer.Exit(code=1)
    typer.echo(
        "workflows check: OK — seam-identity + schema-validity hold across "
        "plan-phase / implement-review / close"
    )

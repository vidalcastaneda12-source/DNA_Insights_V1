"""Typer subcommands for ``genome roadmap`` — the source-of-truth gate (finding-042 / DEC-0125).

One command:

* ``roadmap check`` — the fail-closed gate that keeps ``ROADMAP.md`` authoritative: every top-level
  checklist item has a unique ``RM-<7 hex>`` id, and no finding / ``MEMORY.md`` / ``CHANGELOG.md``
  cites an ``RM-`` id absent from ROADMAP. Prints each violation and exits non-zero on any.

**No database import.** This module (and everything it pulls in) imports no :mod:`genome.db`, so
``genome roadmap check`` runs on a fresh checkout with no DuckDB / SQLCipher built — the same
DB-free guarantee as ``genome docs`` and ``genome workflows``.
"""

from __future__ import annotations

from pathlib import Path

import typer

from genome.roadmap.validator import check

roadmap_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Source-of-truth gate: the fail-closed check that every ROADMAP.md line item carries a "
        "unique RM- id and every RM- reference resolves."
    ),
)


def _repo_root() -> Path:
    """Locate the repo root by walking up from cwd to the first dir holding CLAUDE.md.

    ``genome roadmap`` inspects tracked markdown (ROADMAP.md + findings / ledger / changelog), not
    the runtime ``data/`` dir, so it anchors on the repo marker. Raises ``typer.BadParameter``
    outside the repo.
    """
    start = Path.cwd().resolve()
    for candidate in (start, *start.parents):
        if (candidate / "CLAUDE.md").is_file():
            return candidate
    msg = "`genome roadmap` must run inside the repo (no CLAUDE.md found walking up from cwd)"
    raise typer.BadParameter(msg)


@roadmap_app.command("check")
def check_cmd() -> None:
    """Run the fail-closed source-of-truth gate; exit non-zero on any violation."""
    report = check(_repo_root())
    for violation in report.violations:
        typer.echo(f"[{violation.code}] {violation.location}: {violation.message}")
    if not report.ok:
        typer.echo(f"roadmap check: FAIL — {len(report.violations)} violation(s)")
        raise typer.Exit(code=1)
    typer.echo(
        "roadmap check: OK — every line item has a unique RM- id and all RM- references resolve"
    )

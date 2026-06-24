"""Typer subcommands for ``genome docs`` (plan Task 4).

Two commands:

* ``docs build-index`` — regenerate the findings index inside the marker block of
  ``docs/findings/README.md``, deriving ledger cross-links from frontmatter.
* ``docs check`` — the unified CAPTURE / RETRIEVAL / LIFECYCLE gate; prints each violation
  and exits non-zero on any.

**No database import.** This module (and everything it pulls in) deliberately imports no
:mod:`genome.db`, so ``genome docs check`` runs on a fresh checkout with no DuckDB /
SQLCipher built (plan Task 3). The ``genome`` root CLI registers this sub-app via a lazy
import for the same reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from genome.docs.index import build_index
from genome.docs.validator import check

docs_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Decision-tracking docs tooling: regenerate the findings index and run the "
        "CAPTURE/RETRIEVAL/LIFECYCLE gate over MEMORY.md + finding frontmatter."
    ),
)


def _repo_root() -> Path:
    """Locate the repo root by walking up from cwd to the first dir holding CLAUDE.md.

    ``genome docs`` operates on tracked markdown (``MEMORY.md``, ``docs/findings/``), not the
    runtime ``data/`` dir, so it anchors on the repo marker rather than the DB location.
    Raises ``typer.BadParameter`` when run outside the repo.
    """
    start = Path.cwd().resolve()
    for candidate in (start, *start.parents):
        if (candidate / "CLAUDE.md").is_file():
            return candidate
    msg = "`genome docs` must run inside the repo (no CLAUDE.md found walking up from cwd)"
    raise typer.BadParameter(msg)


@docs_app.command("build-index")
def build_index_cmd(
    *,
    write: Annotated[
        bool,
        typer.Option(
            help="Write the regenerated index. --no-write does a dry run and reports drift."
        ),
    ] = True,
) -> None:
    """Regenerate the findings-index marker block from finding frontmatter."""
    result = build_index(_repo_root(), write=write)
    verb = (
        "wrote" if write and result.changed else "would change" if result.changed else "no change"
    )
    typer.echo(
        f"build-index: {verb} — {result.findings_indexed} findings, "
        f"{result.cross_links_derived} cross-links derived",
    )
    if not write and result.changed:
        raise typer.Exit(code=1)


@docs_app.command("check")
def check_cmd() -> None:
    """Run the unified decision-tracking gate; exit non-zero on any violation."""
    report = check(_repo_root())
    for violation in report.violations:
        typer.echo(
            f"[{violation.dimension}/{violation.code}] {violation.location}: {violation.message}"
        )
    if not report.ok:
        typer.echo(f"docs check: FAIL — {len(report.violations)} violation(s)")
        raise typer.Exit(code=1)
    typer.echo("docs check: OK — capture + retrieval + lifecycle all hold")

"""Typer subcommands for ``genome scope-split`` (``finding-039``; plan §4).

Three commands, the serialization seam between the model-driven ``/scope-run`` skill and the
fail-closed splitter core:

* ``scope-split check`` — read a dispatcher manifest, run :func:`propose_split`, and print the
  proposal (``--json`` for the machine-readable :meth:`SplitResult.to_json` shape).
* ``scope-split dry-run`` — scan + propose only: creates nothing, writes no ROADMAP, runs no
  ``/scope-run``. Prints the literal ``would create N sub-scopes`` or ``atomic — no split``.
* ``scope-split write-roadmap`` — read / pure-transform / write-if-changed the ROADMAP managed
  block (managed-region replace, idempotent). Atomic → echoes the sentinel, writes nothing.

Every command's ``--manifest`` accepts a filesystem path **or** ``-`` (stdin), since
``/scope-run`` threads the manifest as in-prompt JSON, never a file (arch-1 seam). ``--engine``
is typed as the :data:`~genome.scope_split.graph.CouplingEngine` literal, so Typer rejects an
invalid value at the boundary with a clean non-zero exit.

**No** :mod:`genome.db` import. This module (and everything it pulls in) imports no
:mod:`genome.db`, so the splitter core stays runnable on a fresh checkout (plan §3); the
``genome`` root CLI registers this sub-app eagerly, with the DB-free guarantee carried by the
package-local clean-subprocess test, not lazy import.

All three command bodies are **implemented** (``finding-039``): they read the manifest, run
:func:`~genome.scope_split.splitter.propose_split`, and print / splice the proposal. The helper
signatures + ``--manifest`` parsing seam are the frozen contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import structlog
import typer

from genome.scope_split.formatter import (
    ATOMIC_SENTINEL,
    format_roadmap_block,
    format_split_proposal,
)
from genome.scope_split.graph import (
    CouplingEngine,
    CouplingGraphBuilder,
    make_coupling_builder,
)
from genome.scope_split.model import ScopeManifestInput
from genome.scope_split.roadmap_writer import (
    BLOCK_BEGIN,
    DEFAULT_ROADMAP_PATH,
    append_roadmap_block,
)
from genome.scope_split.splitter import propose_split

logger = structlog.get_logger(__name__)

scope_split_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Smart-cut detector: read a dispatcher scope manifest and propose a fail-closed split "
        "into separable sub-scopes (or report atomic). 'check' prints the proposal, 'dry-run' "
        "proposes without writing anything, 'write-roadmap' splices the managed ROADMAP block. "
        "Never auto-runs a sub-scope and never crosses a gate."
    ),
)


@scope_split_app.callback()
def _configure() -> None:
    """Route structlog to **stderr** so ``check --json`` keeps stdout pure JSON.

    The structured event logs are diagnostic, not the command's machine output. Sending them to
    stderr means ``--json`` consumers can ``json.loads(stdout)`` without log lines polluting it.
    """
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


#: The ``--manifest`` option type, shared by all three commands. Accepts a filesystem path or
#: the literal ``-`` (stdin) — ``/scope-run`` threads the manifest as in-prompt JSON (arch-1).
_ManifestOption = Annotated[
    str,
    typer.Option(
        "--manifest",
        help="Path to the dispatcher manifest JSON, or '-' to read it from stdin.",
    ),
]

#: The ``--engine`` option type, shared by ``check`` / ``dry-run``. Selects the coupling-graph
#: builder; ``static`` is the no-scan test seam. Typed as the :data:`CouplingEngine` literal so
#: Typer rejects an invalid value at the boundary (non-zero exit) and ``_make_builder`` needs no
#: redundant guard or ``# type: ignore`` (W1).
_EngineOption = Annotated[
    CouplingEngine,
    typer.Option(
        "--engine",
        help="Coupling-graph engine: 'auto' (default) | 'git-grep' | 'static'.",
    ),
]


def _read_manifest_text(manifest: str) -> str:
    """Read the manifest JSON text from a path or stdin (``-``) — the arch-1 seam.

    ``manifest == "-"`` reads all of stdin (the in-prompt-JSON path); otherwise it reads the
    named file. A missing / unreadable file raises a clean non-zero ``typer.BadParameter``.
    """
    if manifest == "-":
        return sys.stdin.read()
    path = Path(manifest)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read manifest file {path}: {exc}"
        raise typer.BadParameter(msg) from exc


def _load_manifest(manifest: str) -> ScopeManifestInput:
    """Read + parse + narrow a dispatcher manifest into a :class:`ScopeManifestInput`.

    A malformed file (bad JSON, missing required field) raises a clean non-zero
    ``typer.BadParameter``, never an uncaught crash.
    """
    text = _read_manifest_text(manifest)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"manifest is not valid JSON: {exc}"
        raise typer.BadParameter(msg) from exc
    if not isinstance(data, dict):
        msg = f"manifest must be a JSON object, got {type(data).__name__}"
        raise typer.BadParameter(msg)
    try:
        return ScopeManifestInput.from_json(data)
    except (ValueError, TypeError) as exc:
        msg = f"manifest is malformed: {exc}"
        raise typer.BadParameter(msg) from exc


def _make_builder(engine: CouplingEngine) -> CouplingGraphBuilder:
    """Build the coupling-graph builder for ``engine`` via :func:`make_coupling_builder`.

    ``engine`` is already narrowed to the :data:`CouplingEngine` literal by Typer at the CLI
    boundary (an invalid ``--engine`` value exits non-zero before this is reached), so no manual
    unknown-engine guard is needed here (W1) — :func:`make_coupling_builder` still raises
    :class:`ValueError` on any value outside the literal as a defense-in-depth fallback.
    """
    return make_coupling_builder(engine)


@scope_split_app.command("check")
def check_cmd(
    *,
    manifest: _ManifestOption,
    engine: _EngineOption = "auto",
    json_out: Annotated[
        bool,
        typer.Option("--json/--no-json", help="Emit the machine-readable SplitResult JSON."),
    ] = False,
) -> None:
    """Read a manifest, propose a split, and print the proposal (plan §4).

    ``--json`` emits the :meth:`SplitResult.to_json` shape (atomic → exactly
    ``{"atomic": true, "reason": ...}``); otherwise the human-readable proposal block.
    """
    parsed = _load_manifest(manifest)
    builder = _make_builder(engine)
    result = propose_split(parsed, builder)
    logger.info(
        "scope_split.cli.check",
        scope=parsed.scope_id,
        atomic=result.atomic,
        sub_scopes=len(result.sub_scopes),
    )
    if json_out:
        typer.echo(json.dumps(result.to_json(), indent=2))
        return
    typer.echo(format_split_proposal(result, origin_scope=parsed.scope_id))


@scope_split_app.command("dry-run")
def dry_run_cmd(
    *,
    manifest: _ManifestOption,
    engine: _EngineOption = "auto",
) -> None:
    """Propose a split without writing anything (plan §4 — the first-class pytest target).

    Creates nothing, writes no ROADMAP, runs no ``/scope-run``: prints the proposal then the
    literal ``would create N sub-scopes`` (split) or ``atomic — no split`` (atomic).
    """
    parsed = _load_manifest(manifest)
    builder = _make_builder(engine)
    result = propose_split(parsed, builder)
    logger.info(
        "scope_split.cli.dry_run",
        scope=parsed.scope_id,
        atomic=result.atomic,
        sub_scopes=len(result.sub_scopes),
    )
    typer.echo(format_split_proposal(result, origin_scope=parsed.scope_id))
    if result.atomic:
        typer.echo("atomic — no split")
    else:
        typer.echo(f"would create {len(result.sub_scopes)} sub-scopes")


@scope_split_app.command("write-roadmap")
def write_roadmap_cmd(
    *,
    manifest: _ManifestOption,
    engine: _EngineOption = "auto",
    roadmap: Annotated[
        Path,
        typer.Option("--roadmap", help="Path to the ROADMAP.md to splice the managed block into."),
    ] = DEFAULT_ROADMAP_PATH,
) -> None:
    """Splice the proposed sub-scopes into the ROADMAP managed block (managed-region replace; §4).

    Reads the ROADMAP, runs the pure :func:`append_roadmap_block` transform, and writes only when
    the text changed (byte-idempotent). Atomic → echoes the sentinel and writes nothing; a
    ROADMAP missing the managed block raises ``typer.BadParameter``. ``--engine`` selects the
    coupling-graph builder (``static`` is the no-scan test seam).
    """
    parsed = _load_manifest(manifest)
    builder = _make_builder(engine)
    result = propose_split(parsed, builder)

    if result.atomic:
        typer.echo(ATOMIC_SENTINEL)
        logger.info("scope_split.cli.write_roadmap.atomic", scope=parsed.scope_id)
        return

    roadmap_path = roadmap
    try:
        current = roadmap_path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read ROADMAP {roadmap_path}: {exc}"
        raise typer.BadParameter(msg) from exc

    if BLOCK_BEGIN not in current:
        msg = f"ROADMAP {roadmap_path} has no managed slot ({BLOCK_BEGIN}); add it first"
        raise typer.BadParameter(msg)

    block = format_roadmap_block(result, origin_scope=parsed.scope_id)
    try:
        updated = append_roadmap_block(current, block, origin_scope=parsed.scope_id)
    except ValueError as exc:
        msg = f"ROADMAP {roadmap_path} is missing the managed sentinels: {exc}"
        raise typer.BadParameter(msg) from exc

    if updated == current:
        typer.echo(f"ROADMAP unchanged ({roadmap_path}) — block already present")
        logger.info("scope_split.cli.write_roadmap.noop", scope=parsed.scope_id)
        return

    roadmap_path.write_text(updated, encoding="utf-8")
    typer.echo(f"wrote {len(result.sub_scopes)} sub-scope slot(s) to {roadmap_path}")
    logger.info(
        "scope_split.cli.write_roadmap.wrote",
        scope=parsed.scope_id,
        sub_scopes=len(result.sub_scopes),
    )

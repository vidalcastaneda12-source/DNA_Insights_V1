"""Typer subcommands for ``genome campaign`` (``finding-041``; B2 Phase 2).

The advisory surface over the campaign core — it SEQUENCES, TRACKS, and TEES-UP, but **never**
launches ``/scope-run`` and **never** crosses a human gate (PR 1; the live launch is PR 2):

* ``campaign start`` — read a dispatcher manifest, ``propose_split``, and (if non-atomic) seed a
  campaign ledger + tee up the deps-free head + reflect the live state into the ROADMAP managed
  block. Atomic → echo the sentinel, create nothing.
* ``campaign dry-run`` — propose + show the run order only: creates no ledger, writes no ROADMAP.
* ``campaign status`` / ``resume`` — read the persisted current view (the multi-session resume
  seam); ``resume`` points at the next ready sub-scope, advisory ("run it via /scope-run").
* ``campaign cancel`` — append terminal ejections for every active sub-scope (append-only; never
  deletes the ledger).
* ``campaign write-roadmap`` — re-reflect the current state into the ROADMAP managed block
  (read / pure-transform / write-if-changed, idempotent).

Every ``--manifest`` accepts a path **or** ``-`` (stdin), and ROADMAP reflection goes ONLY through
the reused :func:`genome.scope_split.roadmap_writer.append_roadmap_block` into the existing
B2-SUBSCOPES region — the campaign never hand-edits ROADMAP and never writes a second region.

**No** :mod:`genome.db` import; the ``genome`` root CLI registers this sub-app eagerly, the DB-free
guarantee carried by the package-local clean-subprocess test, not lazy import.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import structlog
import typer

from genome.campaign.formatter import format_campaign_roadmap_block, format_campaign_status
from genome.campaign.persistence import (
    DEFAULT_CAMPAIGN_DIR,
    _validate_campaign_id,
    append_records,
    load_campaign,
    load_history,
)
from genome.campaign.state_machine import (
    cancel_campaign,
    next_ready,
    seed_campaign,
    tee_up,
)
from genome.scope_split.formatter import ATOMIC_SENTINEL
from genome.scope_split.graph import CouplingEngine, CouplingGraphBuilder, make_coupling_builder
from genome.scope_split.model import ScopeManifestInput
from genome.scope_split.roadmap_writer import (
    BLOCK_BEGIN,
    DEFAULT_ROADMAP_PATH,
    append_roadmap_block,
)
from genome.scope_split.splitter import propose_split

if TYPE_CHECKING:
    from genome.campaign.model import CampaignState

logger = structlog.get_logger(__name__)

campaign_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Campaign orchestrator: drive a non-atomic scope-split's ordered sub-scopes through "
        "/scope-run as a persistent, resumable campaign. 'start' seeds it, 'dry-run' previews "
        "the order, 'status'/'resume' track it, 'cancel' ejects it, 'write-roadmap' reflects it. "
        "Advisory only — never launches a sub-scope and never crosses a human gate."
    ),
)


@campaign_app.callback()
def _configure() -> None:
    """Route structlog to **stderr** so command stdout stays clean for the human-readable blocks."""
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


#: ``--manifest`` (path or ``-`` stdin) — ``/scope-run`` threads the manifest as in-prompt JSON.
_ManifestOption = Annotated[
    str,
    typer.Option("--manifest", help="Path to the dispatcher manifest JSON, or '-' to read stdin."),
]

#: ``--engine`` — the coupling-graph builder; ``static`` is the no-scan test seam.
_EngineOption = Annotated[
    CouplingEngine,
    typer.Option(
        "--engine", help="Coupling-graph engine: 'auto' (default) | 'git-grep' | 'static'."
    ),
]

#: ``--campaign`` — the campaign id (the parent scope id; the persisted-ledger file stem).
_CampaignOption = Annotated[
    str,
    typer.Option("--campaign", help="The campaign id (the parent scope id)."),
]

#: ``--campaign-dir`` — the ledger home (defaults to the gitignored ``data/campaign``).
_CampaignDirOption = Annotated[
    Path,
    typer.Option("--campaign-dir", help="Directory holding the per-campaign JSONL ledgers."),
]

#: ``--roadmap`` — the ROADMAP to reflect the managed block into.
_RoadmapOption = Annotated[
    Path,
    typer.Option("--roadmap", help="Path to the ROADMAP.md to reflect the managed block into."),
]


def _read_manifest_text(manifest: str) -> str:
    """Read the manifest JSON text from a path or stdin (``-``); a bad path is a BadParameter."""
    if manifest == "-":
        return sys.stdin.read()
    path = Path(manifest)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read manifest file {path}: {exc}"
        raise typer.BadParameter(msg) from exc


def _load_manifest(manifest: str) -> ScopeManifestInput:
    """Read + parse + narrow a dispatcher manifest; malformed input is a clean BadParameter."""
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
    """Build the coupling-graph builder for ``engine`` (Typer already narrowed the value)."""
    return make_coupling_builder(engine)


def _checked_campaign_id(campaign_id: str) -> str:
    """Validate a ``--campaign`` id at the CLI boundary → a clean ``BadParameter``.

    The persistence layer rejects an unsafe file-stem id with a raw ``ValueError``; surfacing it
    here as ``typer.BadParameter`` matches how ``--manifest`` / ``--roadmap`` report bad input
    (a clean non-zero exit, not an unhandled traceback).
    """
    try:
        _validate_campaign_id(campaign_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return campaign_id


def _reflect_roadmap(state: CampaignState, roadmap_path: Path) -> None:
    """Reflect ``state`` into the ROADMAP managed block via the reused, clobber-guarded writer.

    Read / pure-transform / write-if-changed: splices the campaign's rendered block into the
    B2-SUBSCOPES region only, leaving every hand-authored line untouched. A ROADMAP missing the
    managed slot / sentinels raises a clean ``typer.BadParameter``.
    """
    block = format_campaign_roadmap_block(state)
    try:
        current = roadmap_path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read ROADMAP {roadmap_path}: {exc}"
        raise typer.BadParameter(msg) from exc
    if BLOCK_BEGIN not in current:
        msg = f"ROADMAP {roadmap_path} has no managed slot ({BLOCK_BEGIN}); add it first"
        raise typer.BadParameter(msg)
    try:
        updated = append_roadmap_block(current, block, origin_scope=state.campaign_id)
    except ValueError as exc:
        msg = f"ROADMAP {roadmap_path} is missing the managed sentinels: {exc}"
        raise typer.BadParameter(msg) from exc
    if updated == current:
        typer.echo(f"ROADMAP unchanged ({roadmap_path}) — block already current")
        return
    roadmap_path.write_text(updated, encoding="utf-8")
    typer.echo(f"reflected campaign state to {roadmap_path}")


@campaign_app.command("start")
def start_cmd(
    *,
    manifest: _ManifestOption,
    engine: _EngineOption = "auto",
    campaign_dir: _CampaignDirOption = DEFAULT_CAMPAIGN_DIR,
    roadmap: _RoadmapOption = DEFAULT_ROADMAP_PATH,
) -> None:
    """Seed a campaign from a non-atomic split and reflect it to ROADMAP (never auto-launches).

    Atomic → echo the sentinel and create nothing. Else seed the ledger (one ``PENDING`` record
    per sub-scope), tee up the deps-free head, write both in one atomic append, and reflect the
    live state into the ROADMAP managed block. Does **not** launch ``/scope-run``.
    """
    parsed = _load_manifest(manifest)
    result = propose_split(parsed, _make_builder(engine))
    if result.atomic:
        typer.echo(ATOMIC_SENTINEL)
        logger.info("campaign.cli.start.atomic", scope=parsed.scope_id)
        return

    campaign_id = _checked_campaign_id(parsed.scope_id)
    existing = load_history(campaign_id, campaign_dir=campaign_dir)
    if existing:
        # Fail closed: re-seeding would append a second 0..N-1 record_seq run onto the ledger,
        # tearing the append-only monotonic-seq invariant (locked #7). Don't silently double-seed.
        msg = (
            f"campaign {campaign_id!r} already exists ({len(existing)} ledger records) — use "
            "'status' / 'resume' to inspect it, or 'cancel' to retire it before re-seeding"
        )
        raise typer.BadParameter(msg)
    seed = seed_campaign(result, campaign_id)
    readied = tee_up(list(seed))
    append_records(campaign_id, [*seed, *readied], campaign_dir=campaign_dir)
    state = load_campaign(campaign_id, campaign_dir=campaign_dir)
    _reflect_roadmap(state, roadmap)
    typer.echo(f"started campaign {campaign_id!r} with {len(seed)} sub-scopes")
    logger.info("campaign.cli.start", scope=campaign_id, sub_scopes=len(seed))


@campaign_app.command("dry-run")
def dry_run_cmd(
    *,
    manifest: _ManifestOption,
    engine: _EngineOption = "auto",
) -> None:
    """Propose the campaign without writing anything (the first-class pytest target; §5).

    Creates no ledger, writes no ROADMAP, launches nothing: prints ``would run N sub-scopes in
    order: <id1> -> …`` (split) or the atomic sentinel (atomic).
    """
    parsed = _load_manifest(manifest)
    result = propose_split(parsed, _make_builder(engine))
    logger.info("campaign.cli.dry_run", scope=parsed.scope_id, atomic=result.atomic)
    if result.atomic:
        typer.echo(ATOMIC_SENTINEL)
        return
    order = " -> ".join(result.order)
    typer.echo(f"would run {len(result.sub_scopes)} sub-scopes in order: {order}")


@campaign_app.command("status")
def status_cmd(
    *,
    campaign: _CampaignOption,
    campaign_dir: _CampaignDirOption = DEFAULT_CAMPAIGN_DIR,
) -> None:
    """Print the campaign's current view — the latest-active record per sub-scope (§4 step 5)."""
    campaign = _checked_campaign_id(campaign)
    state = load_campaign(campaign, campaign_dir=campaign_dir)
    typer.echo(format_campaign_status(state))
    logger.info("campaign.cli.status", campaign=campaign, sub_scopes=len(state.sub_scopes))


@campaign_app.command("resume")
def resume_cmd(
    *,
    campaign: _CampaignOption,
    campaign_dir: _CampaignDirOption = DEFAULT_CAMPAIGN_DIR,
) -> None:
    """Point at the next ready sub-scope — advisory, never an auto-launch (§4 step 5).

    Picks up the persisted campaign (the multi-session resume seam) and names the next ready
    sub-scope to run manually via ``/scope-run``, or reports the campaign done / blocked.
    """
    campaign = _checked_campaign_id(campaign)
    state = load_campaign(campaign, campaign_dir=campaign_dir)
    nxt = next_ready(state)
    if nxt is not None:
        typer.echo(
            f"next ready: {nxt.sub_scope_id} — run it via /scope-run (then verify-and-merge)"
        )
    elif state.is_done():
        typer.echo(f"campaign {campaign!r} is done — every sub-scope is merged / moot / ejected")
    else:
        typer.echo(
            f"campaign {campaign!r} has no ready sub-scope (blocked on a dependency / in flight)"
        )
    logger.info(
        "campaign.cli.resume", campaign=campaign, next_ready=nxt.sub_scope_id if nxt else None
    )


@campaign_app.command("cancel")
def cancel_cmd(
    *,
    campaign: _CampaignOption,
    campaign_dir: _CampaignDirOption = DEFAULT_CAMPAIGN_DIR,
) -> None:
    """Cancel a campaign — eject every active sub-scope, append-only (refinement C; §4 step 5).

    Appends a terminal ``EJECTED`` record (operator note) for each active sub-scope via the same
    insert-then-flip; never deletes or truncates the ledger. A campaign with nothing active is a
    no-op.
    """
    campaign = _checked_campaign_id(campaign)
    history = load_history(campaign, campaign_dir=campaign_dir)
    ejections = cancel_campaign(history) if history else []
    append_records(campaign, ejections, campaign_dir=campaign_dir)
    typer.echo(f"cancelled campaign {campaign!r}: ejected {len(ejections)} active sub-scope(s)")
    logger.info("campaign.cli.cancel", campaign=campaign, ejected=len(ejections))


@campaign_app.command("write-roadmap")
def write_roadmap_cmd(
    *,
    campaign: _CampaignOption,
    campaign_dir: _CampaignDirOption = DEFAULT_CAMPAIGN_DIR,
    roadmap: _RoadmapOption = DEFAULT_ROADMAP_PATH,
) -> None:
    """Reflect the campaign's current state into the ROADMAP managed block (idempotent; §4 step 5).

    Read / pure-transform / write-if-changed via the reused ``append_roadmap_block``; a ROADMAP
    missing the managed sentinels is a clean ``typer.BadParameter``.
    """
    campaign = _checked_campaign_id(campaign)
    state = load_campaign(campaign, campaign_dir=campaign_dir)
    _reflect_roadmap(state, roadmap)
    logger.info("campaign.cli.write_roadmap", campaign=campaign, sub_scopes=len(state.sub_scopes))

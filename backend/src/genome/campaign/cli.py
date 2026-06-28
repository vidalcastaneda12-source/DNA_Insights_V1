"""Typer subcommands for ``genome campaign`` (``finding-041``; B2 Phase 2).

The live-launch surface over the campaign core — it SEQUENCES, TRACKS, TEES-UP, and **records**
each human-authorized gate crossing, but it **never** crosses a gate AUTONOMOUSLY (every
gate-recording command requires an explicit operator flag and refuses, with no ledger write,
without it) and the CLI itself **never** launches ``/scope-run`` (the ``/campaign-run`` conductor
skill drives that — PR 2 / ``finding-041``):

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
* ``campaign revalidate`` — re-validate a ``READY`` sub-scope immediately before it runs
  (``still_needed`` / ``moot`` / ``changed`` / ``grown``) and bundle the resulting tee-up; this is
  the campaign's own autonomous sequencing decision, never a human gate.
* ``campaign approve-plan`` — record **Gate 1** (plan approval; ``planning → implementing``); the
  core refuses the crossing unless ``--approved`` is given.
* ``campaign record-merge`` — record **Gate 2** (the merge ``/verify-and-merge`` performed;
  ``implementing → merged``) and tee up the next dependent; refuses unless ``--merged`` is given
  (the sole structural Gate-2 enforcer — GAP-C).
* ``campaign show`` — read-only: dump one sub-scope's active record (status / deps / origin /
  manifest snapshot); ``--json`` emits the machine record the conductor feeds ``/scope-run``.

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
from genome.campaign.model import CampaignStatus, RevalidationDecision
from genome.campaign.persistence import (
    DEFAULT_CAMPAIGN_DIR,
    _validate_campaign_id,
    append_records,
    load_campaign,
    load_history,
)
from genome.campaign.state_machine import (
    advance_on_merge,
    apply_revalidation,
    cancel_campaign,
    next_ready,
    seed_campaign,
    tee_up,
    transition,
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
    from collections.abc import Callable

    from genome.campaign.model import CampaignState, SubScopeState
    from genome.scope_split.model import SubScope

logger = structlog.get_logger(__name__)

campaign_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Campaign orchestrator: drive a non-atomic scope-split's ordered sub-scopes through "
        "/scope-run as a persistent, resumable campaign. 'start' seeds it, 'dry-run' previews "
        "the order, 'status'/'resume' track it, 'revalidate'/'approve-plan'/'record-merge' drive "
        "the live loop, 'show' inspects one sub-scope, 'cancel' ejects it, 'write-roadmap' "
        "reflects it. Records human-authorized gate events but never crosses a gate AUTONOMOUSLY "
        "(each gate-recording command requires an explicit flag) and never launches a sub-scope "
        "itself (the /campaign-run conductor skill does that)."
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

#: ``--sub-scope`` — the placeholder sub-scope id (``<origin>-sN``) a live-launch command acts on.
_SubScopeOption = Annotated[
    str,
    typer.Option("--sub-scope", help="The sub-scope id (the campaign placeholder <origin>-sN)."),
]

#: ``--decision`` — the re-validation verdict (Typer narrows the value to the enum member).
_DecisionOption = Annotated[
    RevalidationDecision,
    typer.Option(
        "--decision",
        help="Re-validation verdict: still_needed | moot | changed | grown.",
    ),
]

#: ``--manifest`` for ``revalidate`` — OPTIONAL (only ``changed`` / ``grown`` need it).
_RevalManifestOption = Annotated[
    str | None,
    typer.Option(
        "--manifest",
        help="Re-proposed manifest JSON path or '-' (required for --decision changed / grown).",
    ),
]

#: ``--approved`` — the operator's Gate-1 act; maps straight to the core's external-event guard.
_ApprovedOption = Annotated[
    bool,
    typer.Option("--approved", help="Record the operator's Gate-1 plan approval (required)."),
]

#: ``--merged`` — the operator's Gate-2 act; the SOLE structural enforcer of Gate 2 (GAP-C).
_MergedOption = Annotated[
    bool,
    typer.Option("--merged", help="Record that the Gate-2 merge (/verify-and-merge) happened."),
]

#: ``--json`` — emit a single machine-readable record (the GAP-A seam into ``/scope-run``).
_JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Emit the active record as one JSON object (machine-readable)."),
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


def _read_manifest_snapshot(manifest: str) -> dict[str, object]:
    """Read + parse a manifest to its RAW JSON object — opaque ``CHANGED`` provenance, not narrowed.

    The ``revalidate --decision changed`` path stores the re-proposed manifest verbatim as the new
    ``manifest_snapshot`` (provenance #8), so it keeps the raw dispatcher dict rather than the
    flattened :class:`ScopeManifestInput`. Malformed JSON / a non-object is a clean BadParameter
    (mirroring :func:`_load_manifest`'s ingress checks).
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
    return data


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


def _apply_event(
    campaign: str,
    campaign_dir: Path,
    roadmap: Path,
    build_records: Callable[[list[SubScopeState]], list[SubScopeState]],
) -> CampaignState:
    """Re-read the ledger, build a transition's records, append them, and reflect to ROADMAP.

    The shared shell for every live-launch event (``revalidate`` / ``approve-plan`` /
    ``record-merge``). ``build_records`` is the per-command reducer-call closure: it receives the
    freshly-loaded history (multi-session resumable — no in-memory carryover, constraint 5) and
    returns the records to append. CRUCIALLY it is invoked **before** :func:`append_records`, so a
    rejected gate crossing (a ``ValueError`` from the core's gate guard / READY precondition /
    unknown sub-scope, or the CLI's own flag / re-split guards) is surfaced as a clean
    :class:`typer.BadParameter` with the ledger byte-untouched — the no-autonomous-gate guarantee
    (constraint 3). On success the records are written in one atomic append (locked #7), then the
    reduced state is reflected into the ROADMAP managed block.
    """
    campaign = _checked_campaign_id(campaign)
    history = load_history(campaign, campaign_dir=campaign_dir)
    try:
        records = build_records(history)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    append_records(campaign, records, campaign_dir=campaign_dir)
    state = load_campaign(campaign, campaign_dir=campaign_dir)
    _reflect_roadmap(state, roadmap)
    return state


def _resolve_resplit_children(
    history: list[SubScopeState],
    manifest: str,
    engine: CouplingEngine,
) -> tuple[SubScope, ...]:
    """Re-split a GROWN manifest and apply the §4.3 CLI-boundary guard (collision / dangling dep).

    Runs :func:`propose_split` over the re-proposed manifest, then rejects — with a clean
    ``ValueError`` (surfaced by :func:`_apply_event` as a BadParameter, NO ledger write) — any
    re-split child whose id collides with an existing campaign sub-scope, or whose ``depends_on``
    names an id outside the existing campaign ids and the new sibling ids. That dangling-dep pair
    is the one footgun a live GROWN carve introduces (a seed reusing an active id tears the
    append-only view; a dangling dep blocks its dependents forever — violating fail-loud). An
    atomic / empty re-split returns ``()`` so the pure core ejects-loud rather than carving.
    """
    parsed = _load_manifest(manifest)
    result = propose_split(parsed, _make_builder(engine))
    existing_ids = {record.sub_scope_id for record in history}
    sibling_ids = {child.sub_scope_id for child in result.sub_scopes}
    for child in result.sub_scopes:
        if child.sub_scope_id in existing_ids:
            msg = (
                f"re-split child {child.sub_scope_id!r} collides with an existing campaign "
                "sub-scope — refusing the carve (it would tear the append-only ledger)"
            )
            raise ValueError(msg)
        for dep in child.depends_on:
            if dep not in existing_ids and dep not in sibling_ids:
                msg = (
                    f"re-split child {child.sub_scope_id!r} has a dangling depends_on {dep!r} "
                    "(neither an existing campaign sub-scope nor a sibling) — refusing the carve"
                )
                raise ValueError(msg)
    return result.sub_scopes


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


@campaign_app.command("revalidate")
def revalidate_cmd(  # noqa: PLR0913 — irreducible CLI surface (each option is a distinct flag)
    *,
    campaign: _CampaignOption,
    sub_scope: _SubScopeOption,
    decision: _DecisionOption,
    manifest: _RevalManifestOption = None,
    engine: _EngineOption = "auto",
    campaign_dir: _CampaignDirOption = DEFAULT_CAMPAIGN_DIR,
    roadmap: _RoadmapOption = DEFAULT_ROADMAP_PATH,
) -> None:
    """Re-validate a READY sub-scope right before it runs — autonomous, never a gate (§4.1a).

    Dispatches on ``--decision``: ``still_needed`` (READY→PLANNING) / ``moot`` (READY→MOOT) /
    ``changed`` (re-propose with a fresh ``--manifest`` snapshot, stays READY) / ``grown``
    (re-split the ``--manifest`` into children at depth+1, eject the original — past the cap or with
    no re-split it ejects-loud). The verdict is bundled with a :func:`tee_up` over
    ``[*history, *verdict]`` and appended in ONE write, so a ``moot`` unblocks its dependent and a
    ``grown`` readies the deps-free child in the same run. Re-validation is a READY-stage gate: the
    core rejects a non-READY sub-scope (clean BadParameter, no write).
    """

    def build_records(history: list[SubScopeState]) -> list[SubScopeState]:
        if decision in (RevalidationDecision.STILL_NEEDED, RevalidationDecision.MOOT):
            verdict = apply_revalidation(history, sub_scope, decision)
        elif decision is RevalidationDecision.CHANGED:
            if manifest is None:
                msg = "revalidate --decision changed requires --manifest (the re-proposed snapshot)"
                raise ValueError(msg)
            verdict = apply_revalidation(
                history,
                sub_scope,
                decision,
                updated_manifest_snapshot=_read_manifest_snapshot(manifest),
            )
        else:  # RevalidationDecision.GROWN
            if manifest is None:
                msg = "revalidate --decision grown requires --manifest (the manifest to re-split)"
                raise ValueError(msg)
            verdict = apply_revalidation(
                history,
                sub_scope,
                decision,
                resplit_children=_resolve_resplit_children(history, manifest, engine),
            )
        # Bundle the tee-up over the post-verdict history into the SAME atomic append, so a moot /
        # grown verdict unblocks its dependents in one write (apply_revalidation does not tee up).
        return [*verdict, *tee_up([*history, *verdict])]

    _apply_event(campaign, campaign_dir, roadmap, build_records)
    logger.info(
        "campaign.cli.revalidate", campaign=campaign, sub_scope=sub_scope, decision=decision.value
    )


@campaign_app.command("approve-plan")
def approve_plan_cmd(
    *,
    campaign: _CampaignOption,
    sub_scope: _SubScopeOption,
    approved: _ApprovedOption = False,
    campaign_dir: _CampaignDirOption = DEFAULT_CAMPAIGN_DIR,
    roadmap: _RoadmapOption = DEFAULT_ROADMAP_PATH,
) -> None:
    """Record Gate 1 — plan approval: PLANNING→IMPLEMENTING, only with ``--approved`` (§4.1b).

    The ``--approved`` flag IS the operator's act: it maps straight to the core's ``external_event``
    and the **core is the single enforcer** — ``PLANNING → IMPLEMENTING`` is a GATE_CROSSING, so
    without the flag the core refuses and the CLI surfaces a clean BadParameter with NO ledger
    write (the campaign never crosses a gate autonomously).
    """

    def build_records(history: list[SubScopeState]) -> list[SubScopeState]:
        return [
            transition(
                history,
                sub_scope,
                CampaignStatus.IMPLEMENTING,
                external_event=approved,
                note="Gate 1: plan approved by operator",
            )
        ]

    _apply_event(campaign, campaign_dir, roadmap, build_records)
    logger.info(
        "campaign.cli.approve_plan", campaign=campaign, sub_scope=sub_scope, approved=approved
    )


@campaign_app.command("record-merge")
def record_merge_cmd(
    *,
    campaign: _CampaignOption,
    sub_scope: _SubScopeOption,
    merged: _MergedOption = False,
    campaign_dir: _CampaignDirOption = DEFAULT_CAMPAIGN_DIR,
    roadmap: _RoadmapOption = DEFAULT_ROADMAP_PATH,
) -> None:
    """Record Gate 2 — the merge already performed: IMPLEMENTING→MERGED, only with ``--merged``.

    GAP-C asymmetry (§4.1c): :func:`advance_on_merge` hard-codes ``external_event=True`` internally,
    so unlike Gate 1 the core CANNOT refuse this crossing — the ``if not merged`` check below is the
    **sole structural enforcer** of Gate 2. With ``--merged`` it reuses ``advance_on_merge`` (the
    MERGED record + any newly-unblocked dependents, teed up in one atomic batch); a non-IMPLEMENTING
    sub-scope makes that transition reject → clean BadParameter.
    """

    def build_records(history: list[SubScopeState]) -> list[SubScopeState]:
        if not merged:
            # SOLE Gate-2 enforcer (GAP-C): advance_on_merge sets external_event=True itself, so the
            # core cannot guard this edge — this flag check is the only backstop. Raised before any
            # append → ledger byte-unchanged on rejection.
            msg = (
                "Gate 2 (record-merge) requires --merged — the campaign never crosses a gate "
                "autonomously"
            )
            raise ValueError(msg)
        return advance_on_merge(history, sub_scope)

    _apply_event(campaign, campaign_dir, roadmap, build_records)
    logger.info("campaign.cli.record_merge", campaign=campaign, sub_scope=sub_scope, merged=merged)


@campaign_app.command("show")
def show_cmd(
    *,
    campaign: _CampaignOption,
    sub_scope: _SubScopeOption,
    json_output: _JsonOption = False,
    campaign_dir: _CampaignDirOption = DEFAULT_CAMPAIGN_DIR,
) -> None:
    """Show one sub-scope's active record — read-only; ``--json`` is the GAP-A seam (§4.1d).

    ``--json`` emits the active :class:`~genome.campaign.model.SubScopeState` record as a single
    JSON object (top-level ``status`` + the nested ``manifest_snapshot`` the conductor feeds
    ``/scope-run`` as its Stage-0 manifest). Read-only: no ledger write, no ROADMAP reflection.
    """
    # --json is machine output: it must be PURE JSON on stdout. The group callback routes structlog
    # into the captured stream the test harness folds into the combined output, and load_campaign
    # logs internally, so silence structlog for this read before loading (no logger.info either).
    if json_output:
        structlog.configure(logger_factory=structlog.ReturnLoggerFactory())
    campaign = _checked_campaign_id(campaign)
    state = load_campaign(campaign, campaign_dir=campaign_dir)
    record = state.by_id(sub_scope)
    if record is None:
        msg = f"no such sub-scope {sub_scope!r} in campaign {campaign!r}"
        raise typer.BadParameter(msg)
    if json_output:
        typer.echo(json.dumps(record.to_json()))
        return
    deps = f"; depends_on: {', '.join(record.depends_on)}" if record.depends_on else ""
    note = f" — {record.note}" if record.note else ""
    typer.echo(
        f"[{record.status.value}] {record.sub_scope_id} "
        f"(origin_scope: {record.origin_scope}{deps}){note}"
    )
    logger.info(
        "campaign.cli.show", campaign=campaign, sub_scope=sub_scope, status=record.status.value
    )

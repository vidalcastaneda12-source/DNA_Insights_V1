"""Typer subcommands for ``genome calibrate`` (``finding-040``; plan §4 T7).

Six commands, the serialization seam between the model-driven dispatcher / ``/verify-and-merge``
skills and the deterministic calibration core:

* ``calibrate compute-tier`` — the **loop-closure seam (Gate-1 D1)**: read the assembled
  :class:`~genome.calibration.model.TierFields` from ``--manifest`` (a path or ``-`` for stdin),
  run :func:`~genome.calibration.model.compute_tier` against the live weights, and emit
  ``{tier, breakdown}`` JSON the dispatcher consumes as THE tier. ``--persist`` writes the
  dispatch-time predicted manifest (the write-hook feed); it is **off by default** so an ad-hoc
  run never clobbers a real dispatch store (plan v2.1 minor_2).
* ``calibrate report`` — the on-demand ``/calibrate`` report (per-knob accuracy + coverage +
  proposed disposition).
* ``calibrate ratchet`` — run the asymmetric ratchet; ``--dry-run`` (default) changes nothing,
  ``--apply`` is the explicit second gate. The Python core never runs git — the skill runs the
  emitted CommitPlan gated on this command's exit.
* ``calibrate write-outcome`` — A's close hook: read the ACTUAL gate facts from ``--actual-json``
  (path or ``-``), source the predicted block from the persisted manifest, and append one
  :class:`~genome.calibration.model.OutcomeRecord`. A missing manifest is a **visible drop**
  (stderr warn + ``exit 0`` + no append), never a corrupt row.
* ``calibrate show-weights`` — print the live tunable weights (``--json`` for machine output).
* ``calibrate apply-parked`` — one-click human approval of a parked loosen / clean-by-vacuity
  tighten, after a TOCTOU back-test + direction re-check.

**No** :mod:`genome.db` and **no** :mod:`genome.config` import. This module (and everything it
pulls in) stays runnable on a fresh checkout (plan §3); the ``genome`` root CLI registers this
sub-app eagerly, with the DB-free guarantee carried by the package-local clean-subprocess test.
``git`` is run by the skill, never here.

The command set, option names, and the ``--manifest`` / ``--actual-json`` ``path | -`` seam are
the frozen contract.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import structlog
import typer

from genome.calibration.backtest import run_backtest
from genome.calibration.commit_plan import render_commit_plan
from genome.calibration.formatter import format_calibration_report, format_ratchet_decision
from genome.calibration.model import (
    ActualBlock,
    AuditRow,
    Disposition,
    OutcomeRecord,
    PredictedBlock,
    PredictedManifest,
    TierFields,
    compute_tier,
)
from genome.calibration.persistence import (
    append_audit,
    append_outcome,
    load_audit,
    load_outcomes,
    pending_parked,
    read_manifest,
    read_weights,
    write_manifest,
    write_weights,
)
from genome.calibration.ratchet import (
    classify_direction,
    nontarget_knobs_unchanged,
    propose_ratchet,
)

if TYPE_CHECKING:
    from genome.calibration.backtest import BacktestResult
    from genome.calibration.model import Direction, RatchetDecision, RiskWeights

logger = structlog.get_logger(__name__)


def _read_json_text(source: str) -> str:
    """Read JSON text from a filesystem path or the literal ``-`` (stdin) — the CLI seam."""
    if source == "-":
        return sys.stdin.read()
    path = Path(source)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read {path}: {exc}"
        raise typer.BadParameter(msg) from exc


def _today() -> str:
    """The close date as an ISO string (tz-aware UTC) — metadata only."""
    return datetime.datetime.now(tz=datetime.UTC).date().isoformat()


calibration_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Cross-run learning: the single deterministic risk-tier source of truth "
        "(compute-tier), the on-demand /calibrate report, and the asymmetric auto-tuning "
        "ratchet (tighten auto-applies back-test-gated; loosen parks for human approval). "
        "Ships report-only — auto-tuning is dark until an auditable signoff."
    ),
)


@calibration_app.callback()
def _configure() -> None:
    """Route structlog to **stderr** so ``compute-tier`` / ``show-weights`` keep stdout pure JSON.

    The structured event logs are diagnostic, not the command's machine output. Sending them to
    stderr means ``--json`` consumers can ``json.loads(stdout)`` without log lines polluting it.
    """
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


#: The ``--manifest`` option type for ``compute-tier``. Accepts a filesystem path **or** the
#: literal ``-`` (stdin) — the dispatcher threads the assembled TierFields as in-prompt JSON.
_ManifestOption = Annotated[
    str,
    typer.Option(
        "--manifest",
        help="Path to the assembled TierFields JSON, or '-' to read it from stdin.",
    ),
]

#: The ``--actual-json`` option type for ``write-outcome``. Accepts a path or ``-`` (stdin) — the
#: close skill threads the human-confirmed ACTUAL gate facts as in-prompt JSON.
_ActualJsonOption = Annotated[
    str,
    typer.Option(
        "--actual-json",
        help="Path to the ACTUAL gate-facts JSON, or '-' to read it from stdin.",
    ),
]

#: The ``--scope-id`` option type, shared by ``compute-tier`` (for the persist filename) and
#: ``write-outcome`` (to source the persisted manifest).
_ScopeIdOption = Annotated[
    str,
    typer.Option("--scope-id", help="The dispatcher scope id (e.g. 'PR-6')."),
]

#: The ``--merges-since-last`` option type, shared by ``report`` / ``ratchet``. The skill supplies
#: the count of merges since the last ratchet pass; it drives the cadence gate.
_MergesSinceLastOption = Annotated[
    int,
    typer.Option(
        "--merges-since-last",
        min=0,
        help="Merges accumulated since the last ratchet pass (drives the cadence gate).",
    ),
]


@calibration_app.command("compute-tier")
def compute_tier_cmd(
    *,
    manifest: _ManifestOption,
    scope_id: Annotated[
        str | None,
        typer.Option(
            "--scope-id",
            help="Scope id for the --persist filename; required when --persist is set.",
        ),
    ] = None,
    persist: Annotated[
        bool,
        typer.Option(
            "--persist/--no-persist",
            help=(
                "Persist the FINAL predicted {tier,breakdown} to "
                "data/calibration/manifests/<scope_id>.json (the write-hook feed). Off by "
                "default so ad-hoc runs never clobber a dispatch store; the dispatcher passes "
                "it explicitly."
            ),
        ),
    ] = False,
) -> None:
    """Compute the deterministic risk tier for the assembled fields and emit ``{tier, breakdown}``.

    Reads :class:`~genome.calibration.model.TierFields` from ``--manifest`` (path or ``-``), runs
    :func:`~genome.calibration.model.compute_tier` against the live ``risk_weights.json``, and
    prints ``{"tier": int, "breakdown": {...}}`` to stdout — the single source of truth the
    dispatcher consumes (Gate-1 D1). With ``--persist`` it also writes the predicted manifest for
    A's close to source. A malformed manifest raises a clean non-zero ``typer.BadParameter``.
    """
    text = _read_json_text(manifest)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"manifest is not valid JSON: {exc}"
        raise typer.BadParameter(msg) from exc
    if not isinstance(data, dict):
        msg = f"manifest must be a JSON object, got {type(data).__name__}"
        raise typer.BadParameter(msg)
    try:
        fields = TierFields.from_json(data)
    except (ValueError, TypeError) as exc:
        msg = f"manifest is malformed: {exc}"
        raise typer.BadParameter(msg) from exc

    weights = read_weights()
    tier, breakdown = compute_tier(fields, weights)

    if persist:
        if scope_id is None:
            msg = "--scope-id is required when --persist is set"
            raise typer.BadParameter(msg)
        write_manifest(
            PredictedManifest(
                scope_id=scope_id,
                risk_weights_version=weights.weights_version,
                predicted=PredictedBlock(tier=tier, breakdown=breakdown),
            ),
        )

    typer.echo(json.dumps({"tier": tier, "breakdown": breakdown.to_json()}))


@calibration_app.command("report")
def report_cmd(
    *,
    merges_since_last: _MergesSinceLastOption = 0,
) -> None:
    """Print the on-demand ``/calibrate`` report (plan §4 T6).

    Reads the outcome ledger + live weights, computes per-knob accuracy + the systematic-error
    tally + each knob's coverage status, and shows the proposed ratchet disposition (with its
    PARK-by-vacuity / PARK-by-loosen reason). Below the thin-data threshold it reports
    insufficient data. Changes nothing.
    """
    weights = read_weights()
    outcomes = load_outcomes()
    decision = propose_ratchet(outcomes, weights, merges_since_last)
    typer.echo(format_calibration_report(outcomes, weights, decision))


@calibration_app.command("ratchet")
def ratchet_cmd(
    *,
    merges_since_last: _MergesSinceLastOption = 0,
    apply_changes: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help=(
                "--dry-run (default) computes the decision and changes nothing; --apply is the "
                "explicit second gate that writes an AUTO_COMMIT candidate + emits the CommitPlan "
                "for the skill to commit. The seed always NO_OPs (auto-tuning disabled)."
            ),
        ),
    ] = False,
) -> None:
    """Run the asymmetric auto-tuning ratchet over the outcome ledger (plan §4 T3 / T7).

    ``--dry-run`` (default) is inert: it prints the would-commit / would-draft diff and leaves the
    git index + weights untouched. ``--apply`` writes an ``AUTO_COMMIT`` candidate's weights and
    emits the pathspec-scoped :class:`~genome.calibration.commit_plan.CommitPlan`; the skill runs
    git gated on the exit code. Loosens and clean-by-vacuity tightens are parked, never applied.
    """
    weights = read_weights()
    outcomes = load_outcomes()
    decision = propose_ratchet(outcomes, weights, merges_since_last)
    typer.echo(format_ratchet_decision(decision))

    if not apply_changes:
        # --dry-run (default) is inert: the Python core never runs git and writes nothing.
        return

    if decision.auto_applicable and decision.candidate_weights is not None:
        # Audit BEFORE the weights mutate, so there is never an un-audited tune: if write_weights
        # raises, the (intended) audit row already records it and the CommitPlan is not emitted.
        append_audit(AuditRow(date=_today(), applied=True, decision=decision))
        write_weights(decision.candidate_weights)
        typer.echo(json.dumps(render_commit_plan(decision).to_json()))
        return

    if decision.disposition in {Disposition.PARK_FOR_APPROVAL, Disposition.SUPPRESSED}:
        append_audit(AuditRow(date=_today(), applied=False, decision=decision))
    typer.echo(f"no auto-commit: disposition is {decision.disposition.value}")


@calibration_app.command("write-outcome")
def write_outcome_cmd(
    *,
    scope_id: _ScopeIdOption,
    actual_json: _ActualJsonOption,
) -> None:
    """Append one outcome record at A's close (plan §4 T7 / FIX-3).

    Reads the ACTUAL gate facts from ``--actual-json`` (path or ``-``), sources
    ``predicted.{tier, breakdown}`` + ``risk_weights_version`` from the persisted manifest
    (:func:`~genome.calibration.persistence.read_manifest`), and appends one
    :class:`~genome.calibration.model.OutcomeRecord` to the ledger. A **missing** manifest is a
    visible drop: a stderr warning ``outcome NOT recorded: no persisted manifest for <id>`` +
    ``exit 0`` + no append. A malformed ACTUAL payload raises ``typer.BadParameter`` (non-zero, no
    append). Never touches ``verify_gate``.
    """
    text = _read_json_text(actual_json)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"--actual-json is not valid JSON: {exc}"
        raise typer.BadParameter(msg) from exc
    if not isinstance(payload, dict):
        msg = f"--actual-json must be a JSON object, got {type(payload).__name__}"
        raise typer.BadParameter(msg)

    # The payload carries the ActualBlock fields PLUS merged_sha + date (close-skill metadata).
    merged_sha = str(payload.pop("merged_sha", ""))
    date = str(payload.pop("date", "") or _today())
    try:
        actual = ActualBlock.from_json(payload)
    except (ValueError, TypeError) as exc:
        msg = f"--actual-json is malformed: {exc}"
        raise typer.BadParameter(msg) from exc

    manifest = read_manifest(scope_id)
    if manifest is None:
        # Visible drop: never block A's close, never append a corrupt/partial row.
        typer.echo(f"outcome NOT recorded: no persisted manifest for {scope_id}", err=True)
        logger.warning("calibration.write_outcome.no_manifest", scope_id=scope_id)
        return

    record = OutcomeRecord(
        scope_id=scope_id,
        merged_sha=merged_sha,
        date=date,
        risk_weights_version=manifest.risk_weights_version,
        predicted=manifest.predicted,
        actual=actual,
    )
    append_outcome(record)
    typer.echo(
        json.dumps(
            {"recorded": True, "scope_id": scope_id, "predicted_tier": record.predicted.tier},
        ),
    )


@calibration_app.command("show-weights")
def show_weights_cmd(
    *,
    json_out: Annotated[
        bool,
        typer.Option("--json/--no-json", help="Emit the machine-readable RiskWeights JSON."),
    ] = False,
) -> None:
    """Print the live tunable weights from ``risk_weights.json`` (plan §4 T7).

    ``--json`` emits the :meth:`~genome.calibration.model.RiskWeights.to_json` shape on stdout
    (logs go to stderr); otherwise a human-readable summary. Read-only.
    """
    weights = read_weights()
    if json_out:
        typer.echo(json.dumps(weights.to_json(), indent=2))
        return
    typer.echo(f"weights_version: {weights.weights_version}")
    typer.echo(f"auto_tuning_enabled: {weights.auto_tuning_enabled}")
    typer.echo(f"t1: {weights.t1}  t2: {weights.t2}")
    typer.echo(f"c_map: {dict(weights.c_map)}")
    typer.echo(f"b_buckets: {dict(weights.b_buckets)}")
    typer.echo(f"p_levels: {dict(weights.p_levels)}")


def _apply_refusal(
    live: RiskWeights,
    candidate: RiskWeights,
    decision: RatchetDecision,
    result: BacktestResult,
    current_direction: Direction,
) -> str | None:
    """The first failing apply-time guard's message for ``apply-parked``, or ``None`` if all pass.

    The fail-closed re-check order before a parked candidate may be written:

    * **FIX-3 — kill switch:** ``apply-parked`` HONORS ``auto_tuning_enabled`` (VSC-User Gate-1
      decision, 2026-06-28); off → refuse, so toggling the switch off re-freezes the human-approval
      path too (mirrors :func:`~genome.calibration.ratchet.propose_ratchet`'s first gate).
    * **back-test:** the candidate must still flip no frozen back-test row (TOCTOU).
    * **direction:** the live-vs-candidate direction must still match the parked direction.
    * **FIX-1 — lost update:** the parked candidate is a one-knob delta on the park-time live
      weights; if an intervening ``AUTO_COMMIT`` moved a DIFFERENT knob, writing the snapshot
      wholesale would revert it (a tier-neutral revert slips the two checks above). Refuse unless
      the candidate still matches current live on every non-target knob.
    """
    if not live.auto_tuning_enabled:
        return (
            "auto-tuning disabled (kill switch off); parked approval frozen — "
            "no weight write until signoff"
        )
    if not result.clean:
        return "re-check failed: candidate now flips a back-test row; NOT applied"
    if decision.direction is not None and current_direction is not decision.direction:
        return (
            f"re-check failed: direction changed since park "
            f"({decision.direction.value} → {current_direction.value}); NOT applied"
        )
    if decision.knob is not None and not nontarget_knobs_unchanged(live, candidate, decision.knob):
        return "re-check failed: live weights moved on another knob since park; NOT applied"
    return None


@calibration_app.command("apply-parked")
def apply_parked_cmd(
    *,
    apply_changes: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help=(
                "--dry-run (default) re-checks and shows the parked decision; --apply commits it "
                "after a fresh TOCTOU back-test + direction re-check still passes."
            ),
        ),
    ] = False,
) -> None:
    """One-click human approval of a parked loosen / clean-by-vacuity tighten (plan §4 T7).

    Reads the latest **un-consumed** parked decision (:func:`~genome.calibration.persistence.
    pending_parked` — FIX-2), re-runs the back-test + direction check (TOCTOU guard: the ledger may
    have moved since the park), and — only if every guard still holds — writes the candidate
    weights + emits the :class:`~genome.calibration.commit_plan.CommitPlan` for the skill. The apply
    path is fail-closed on four guards in order: the kill switch (``auto_tuning_enabled`` —
    apply-parked HONORS it, FIX-3), the back-test, the direction re-check, and a non-target-knob
    lost-update guard (FIX-1). On approval the appended ``applied=True`` row consumes the parked row
    (insert-then-supersede, FIX-2), so it is approvable exactly once. ``--dry-run`` (default)
    re-checks and reports without changing anything.
    """
    parked = pending_parked(load_audit())
    if not parked:
        typer.echo("no parked decision awaiting approval")
        return
    decision = parked[-1].decision
    candidate = decision.candidate_weights
    if candidate is None:
        typer.echo("parked decision carries no candidate weights")
        return

    # TOCTOU guard: re-run BOTH the back-test and the direction classification against the CURRENT
    # live weights (the ledger / weights may have moved since the park).
    live = read_weights()
    result = run_backtest(candidate)
    current_direction = classify_direction(live, candidate)
    parked_direction = decision.direction.value if decision.direction is not None else "n/a"
    typer.echo(format_ratchet_decision(decision))
    typer.echo(
        f"re-check: back-test {'clean' if result.clean else 'dirty'}; "
        f"direction now {current_direction.value} (parked as {parked_direction})"
    )

    if not apply_changes:
        typer.echo("--dry-run: re-checked only; nothing written")
        return
    refusal = _apply_refusal(live, candidate, decision, result, current_direction)
    if refusal is not None:
        typer.echo(refusal)
        return

    approved = dataclasses.replace(
        decision, disposition=Disposition.AUTO_COMMIT, auto_applicable=True
    )
    # Audit BEFORE the weights mutate — no un-audited tune (mirrors `ratchet --apply`). The
    # applied=True row also CONSUMES the parked row (FIX-2): pending_parked excludes it next time.
    append_audit(AuditRow(date=_today(), applied=True, decision=approved))
    write_weights(candidate)
    typer.echo(json.dumps(render_commit_plan(approved).to_json()))

"""Typer subcommands for ``genome fast-follow`` (``finding-038``; plan §4 / R2).

Three commands, the serialization seam between the model-driven skill and the fail-closed
core:

* ``fast-follow scan-assemble`` — a convenience that builds a ``candidates.json`` from
  repeatable flat ``--candidate 'k=v,...'`` tokens. Scalar fields split on ``,``; the two
  **collection** fields (``touched_paths``, ``change_class``) use ``|`` as the intra-field
  sub-delimiter so a list value's separator never collides with the field-comma — a
  mis-parsed ``touched_paths`` would be a false-DRAIN path, which the §2 safety invariant
  forbids. ``--out`` names the JSON to write.
* ``fast-follow triage`` — read a ``candidates.json`` (the **canonical** input seam, R2 —
  agents emit JSON natively, encoding lists/sets losslessly), triage it, and print the
  plan. ``--dry-run`` does scan + triage only (the first-class pytest target, OQ-2).
* ``fast-follow eject-draft`` — read a ``candidates.json``, classify it, and print the
  EJECT draft to stdout for the human to paste into ``/scope-run`` (never writes ROADMAP).

**No database import.** This module (and everything it pulls in) imports no :mod:`genome.db`,
so the loop core stays runnable on a fresh checkout (plan §3 / A4). The ``genome`` root CLI
registers this sub-app eagerly; the DB-free guarantee is carried by the package-local
``test_fast_follow_no_db_import.py`` clean-subprocess test, not by lazy import. ``gh`` / ``rm``
/ the merge itself live in the **skill**, never here.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import structlog
import typer

from genome.fast_follow.formatter import format_eject_draft, format_triage_plan
from genome.fast_follow.loop import plan_next_batch
from genome.fast_follow.model import Candidate, Classification

logger = structlog.get_logger(__name__)

#: The two collection fields whose values are split on the ``|`` intra-field sub-delimiter
#: (so a list value's separator never collides with the field-comma — a mis-parsed
#: ``touched_paths`` would be a false-DRAIN path the §2 safety invariant forbids).
_COLLECTION_FIELDS: frozenset[str] = frozenset({"touched_paths", "change_class"})

#: The optional-integer fields that accept a literal ``none`` (case-insensitive) → ``None``
#: (the fail-closed undecidable signal), or an integer string.
_OPT_INT_FIELDS: frozenset[str] = frozenset({"blast_radius", "applicable_anchors"})

fast_follow_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Fast-follow drain loop: scan-assemble a candidates.json, triage it into a "
        "fail-closed DRAIN/EJECT/DISCARD plan, and draft the EJECT block for /scope-run. "
        "The CLI never merges and never writes ROADMAP — drains go through /verify-and-merge."
    ),
)


def _parse_opt_int(field: str, value: str, token: str) -> int | None:
    """Parse an optional-integer field value: literal ``none`` → ``None``, else an int."""
    if value.lower() == "none" or value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        msg = (
            f"--candidate field {field!r} must be an integer or 'none', got {value!r} in {token!r}"
        )
        raise typer.BadParameter(msg) from exc


def _tokenize_candidate_fields(token: str) -> dict[str, str]:
    """Split a flat ``--candidate 'k=v,...'`` token into its ``{key: value}`` fields.

    Each chunk must be ``key=value`` (raises otherwise), and a duplicate key is rejected rather
    than silently last-winning — for ``touched_paths`` / ``change_class`` a silent overwrite
    would drop a guarded path/label, a false-DRAIN the §2 safety invariant forbids.
    """
    fields: dict[str, str] = {}
    for chunk in token.split(","):
        part = chunk.strip()
        if not part:
            continue
        if "=" not in part:
            msg = f"--candidate field must be 'key=value', got {part!r} in {token!r}"
            raise typer.BadParameter(msg)
        key, _, value = part.partition("=")
        key_clean = key.strip()
        if key_clean in fields:
            msg = f"--candidate has duplicate field {key_clean!r} in {token!r}"
            raise typer.BadParameter(msg)
        fields[key_clean] = value.strip()
    return fields


def _parse_candidate_token(token: str) -> Candidate:
    """Parse one flat ``--candidate 'k=v,...'`` token into a :class:`Candidate`.

    Scalar fields split on ``,``; the two collection fields (``touched_paths`` /
    ``change_class``) carry their own values split on the ``|`` intra-field sub-delimiter so a
    list value's separator never collides with the field-comma (a mis-parsed ``touched_paths``
    is a false-DRAIN path the §2 safety invariant forbids). A malformed token — no ``=`` in a
    chunk, a duplicate / unknown field, or a missing required field — raises ``typer.BadParameter``.
    """
    fields = _tokenize_candidate_fields(token)

    allowed = {
        "candidate_id",
        "source",
        "kind",
        "tier",
        "is_stale",
        *_COLLECTION_FIELDS,
        *_OPT_INT_FIELDS,
    }
    unknown = sorted(set(fields) - allowed)
    if unknown:
        msg = f"--candidate has unknown field(s) {unknown}; valid fields are {sorted(allowed)}"
        raise typer.BadParameter(msg)

    for required in ("candidate_id", "source", "kind"):
        if not fields.get(required):
            msg = f"--candidate is missing required field {required!r}, got {token!r}"
            raise typer.BadParameter(msg)

    def _collection(field: str) -> tuple[str, ...]:
        raw = fields.get(field, "")
        if raw == "":
            return ()
        parts = raw.split("|")
        # An empty '|'-segment means a mis-delimited value (e.g. a stray '||' or wrong
        # separator) that would silently shrink the collection — for touched_paths that can
        # disarm the path guard (false-DRAIN). Reject rather than silently drop.
        if any(not part for part in parts):
            msg = f"--candidate field {field!r} has an empty '|'-segment in {raw!r}"
            raise typer.BadParameter(msg)
        return tuple(parts)

    is_stale_raw = fields.get("is_stale", "false").lower()
    # Strict boolean parse — a malformed is_stale (typo, 'maybe') must raise, not silently
    # coerce to False (which would re-enter the drain lane), matching the JSON seam's _as_bool.
    if is_stale_raw not in {"true", "1", "yes", "false", "0", "no"}:
        msg = f"--candidate is_stale must be a boolean, got {fields.get('is_stale')!r} in {token!r}"
        raise typer.BadParameter(msg)
    tier_raw = fields.get("tier")
    return Candidate(
        candidate_id=fields["candidate_id"],
        source=fields["source"],
        kind=fields["kind"],
        change_class=frozenset(_collection("change_class")),
        blast_radius=_parse_opt_int("blast_radius", fields.get("blast_radius", "none"), token),
        applicable_anchors=_parse_opt_int(
            "applicable_anchors", fields.get("applicable_anchors", "none"), token
        ),
        tier=None if tier_raw in (None, "", "none") else tier_raw,
        touched_paths=_collection("touched_paths"),
        is_stale=is_stale_raw in {"true", "1", "yes"},  # falsey values validated above
    )


def _load_candidates(path: Path) -> list[Candidate]:
    """Read a ``candidates.json`` and reconstruct the :class:`Candidate` list (the seam, R2).

    A missing / unreadable / malformed file raises ``typer.BadParameter`` (a clean non-zero
    exit), never an uncaught crash.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read candidates file {path}: {exc}"
        raise typer.BadParameter(msg) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"candidates file {path} is not valid JSON: {exc}"
        raise typer.BadParameter(msg) from exc
    if not isinstance(data, list):
        msg = f"candidates file {path} must be a JSON array, got {type(data).__name__}"
        raise typer.BadParameter(msg)
    out: list[Candidate] = []
    for item in data:
        if not isinstance(item, dict):
            msg = f"each candidate must be a JSON object, got {type(item).__name__} in {path}"
            raise typer.BadParameter(msg)
        try:
            out.append(Candidate.from_json(item))
        except (ValueError, TypeError) as exc:
            msg = f"candidates file {path} has a malformed candidate: {exc}"
            raise typer.BadParameter(msg) from exc
    return out


@fast_follow_app.command("scan-assemble")
def scan_assemble_cmd(
    *,
    candidate: Annotated[
        list[str],
        typer.Option(
            "--candidate",
            help=(
                "One candidate as flat 'k=v,...' tokens. Scalar fields split on ','; the "
                "collection fields touched_paths and change_class use '|' as the intra-field "
                "sub-delimiter (e.g. "
                "'touched_paths=docs/schemas/x.md|ddl/y.sql,change_class=schema|pipeline'). "
                "Repeatable."
            ),
        ),
    ],
    out: Annotated[
        Path,
        typer.Option("--out", help="Path to write the assembled candidates.json."),
    ] = Path("candidates.json"),
) -> None:
    """Assemble flat ``--candidate`` tokens into a ``candidates.json``.

    A convenience over the canonical JSON seam (R2): the collection fields use ``|`` as the
    intra-field sub-delimiter so a ``docs/schemas/**`` path survives the round-trip and still
    EJECTs (the touched_paths guard is not defeated by parsing). Malformed tokens raise a
    clean non-zero ``typer.BadParameter``.
    """
    candidates = [_parse_candidate_token(token) for token in candidate]
    payload = [c.to_json() for c in candidates]
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("fast_follow.scan_assemble.wrote", out=str(out), count=len(candidates))
    typer.echo(f"wrote {len(candidates)} candidate(s): {out}")


@fast_follow_app.command("triage")
def triage_cmd(
    *,
    candidates: Annotated[
        Path,
        typer.Option(
            "--candidates",
            exists=False,
            help="Path to the candidates.json (the canonical input seam, R2).",
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Scan + triage only, no drain — the first-class pytest target (OQ-2).",
        ),
    ] = False,
) -> None:
    """Triage a ``candidates.json`` and print the fail-closed plan.

    ``--dry-run`` runs scan + triage only (no drain, no gate) and is the deterministic test
    seam. A missing / malformed candidates file raises a clean non-zero ``typer.BadParameter``.
    """
    parsed = _load_candidates(candidates)
    # The CLI triages from a clean slate (no persisted seen-set, no prior batches) — the
    # cross-invocation dedup is the skill's job around this pure step.
    plan = plan_next_batch(parsed, seen=set(), batches_done=0)
    if dry_run:
        plan = replace(plan, dry=True)
    typer.echo(format_triage_plan(plan))
    if dry_run:
        counts = plan.counts()
        typer.echo(
            f"would drain {counts['drain']} / eject {counts['eject']} / discard {counts['discard']}"
        )


@fast_follow_app.command("eject-draft")
def eject_draft_cmd(
    *,
    candidates: Annotated[
        Path,
        typer.Option(
            "--candidates",
            exists=False,
            help="Path to the candidates.json (the canonical input seam, R2).",
        ),
    ],
) -> None:
    """Print the EJECT draft for a ``candidates.json`` to stdout (plan OQ-3).

    Drafts-to-stdout only — the human pastes the block into ``/scope-run``; this never writes
    ROADMAP.md. A missing / malformed candidates file raises a clean non-zero
    ``typer.BadParameter``.
    """
    parsed = _load_candidates(candidates)
    plan = plan_next_batch(parsed, seen=set(), batches_done=0)
    # The eject draft covers every EJECT verdict across the planned + overflow partitions.
    ejects = [
        triage
        for triage in (*plan.triaged, *plan.overflow)
        if triage.classification is Classification.EJECT
    ]
    typer.echo(format_eject_draft(ejects))

"""Typer subcommands for ``genome verify-gate`` (plan §4.5 / R1).

Three commands, the serialization seam between the bash skill and the fail-closed core:

* ``verify-gate assemble`` — build an :class:`~genome.verify_gate.model.EvidencePackage`
  from **flat primitive** args (the skill passes only flat strings; bash never assembles
  nested JSON), construct the frozen dataclasses Python-side, and emit ``evidence.json``.
* ``verify-gate verdict`` — read an ``evidence.json``, reduce it, print the verdict, and
  **exit non-zero on ``BLOCKED`` or ``UNKNOWN``** (the skill's whole gate: a non-zero exit
  here stops the merge).
* ``verify-gate format`` — read an ``evidence.json`` and print the raw evidence block.

**No database import.** This module (and everything it pulls in) imports no :mod:`genome.db`,
so the gate core stays runnable on a fresh checkout (plan §4.1). The ``genome`` root CLI
registers this sub-app via a lazy import for the same reason. ``gh`` / ``rm`` / the merge
itself live in the **skill**, never here — the CLI's only job is data → verdict → exit code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from genome.verify_gate.formatter import format_evidence
from genome.verify_gate.model import (
    AnchorCheck,
    EvidencePackage,
    IntegrityFlags,
    StepStatus,
    Verdict,
)
from genome.verify_gate.verdict import reduce_verdict

verify_gate_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Agentic verify-and-merge gate: assemble an evidence package, reduce it to a "
        "three-valued verdict, and render the raw evidence block. The CLI never merges — "
        "a non-zero `verdict` exit is the signal the skill stops on."
    ),
)


def _parse_step(raw: str) -> tuple[str, StepStatus]:
    """Parse one ``--step`` flat token ``name:exit_code`` into ``(name, StepStatus)``.

    Split on the LAST ``:`` so step names containing a colon survive. An empty exit-code
    token (``pytest:``) is the documented UNKNOWN case; a non-numeric token
    (``pytest:notanumber``) is malformed and raises — the seam never silently coerces.
    """
    if ":" not in raw:
        msg = f"--step must be 'name:exit_code', got {raw!r}"
        raise typer.BadParameter(msg)
    name, _, code = raw.rpartition(":")
    if not name:
        msg = f"--step is missing a step name before ':', got {raw!r}"
        raise typer.BadParameter(msg)
    code = code.strip()
    if code == "":
        return name, StepStatus.UNKNOWN
    try:
        exit_code = int(code)
    except ValueError as exc:
        msg = f"--step exit code must be an integer or empty (UNKNOWN), got {raw!r}"
        raise typer.BadParameter(msg) from exc
    return name, (StepStatus.PASS if exit_code == 0 else StepStatus.FAIL)


def _parse_anchor(raw: str) -> AnchorCheck:
    """Parse one ``--anchor`` flat token into an :class:`AnchorCheck`.

    Shape: ``name=<col>,expected=<v>,actual=<v>[,deferred=true]``. A token missing the
    ``key=value`` structure, or one without a ``name=``, is malformed and raises — a fabricated
    or silently-dropped anchor is exactly the false-GREEN risk the gate exists to prevent.
    A literal ``none`` value (case-insensitive) maps to ``None`` (a not-captured side).
    """
    fields: dict[str, str] = {}
    for part in raw.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            msg = f"--anchor field must be 'key=value', got {chunk!r} in {raw!r}"
            raise typer.BadParameter(msg)
        key, _, value = chunk.partition("=")
        fields[key.strip()] = value.strip()
    if "name" not in fields or not fields["name"]:
        msg = f"--anchor must include a non-empty 'name=', got {raw!r}"
        raise typer.BadParameter(msg)

    def _opt(value: str | None) -> str | None:
        if value is None or value.lower() == "none":
            return None
        return value

    return AnchorCheck(
        name=fields["name"],
        expected=_opt(fields.get("expected")),
        actual=_opt(fields.get("actual")),
        deferred=fields.get("deferred", "").lower() in {"true", "1", "yes"},
    )


def _load_package(package: Path) -> EvidencePackage:
    """Read an ``evidence.json`` and reconstruct the :class:`EvidencePackage`.

    A missing / unreadable / malformed file raises ``typer.BadParameter`` (a clean non-zero
    exit), never an uncaught crash.
    """
    try:
        text = package.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read evidence package {package}: {exc}"
        raise typer.BadParameter(msg) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"evidence package {package} is not valid JSON: {exc}"
        raise typer.BadParameter(msg) from exc
    if not isinstance(data, dict):
        msg = f"evidence package {package} must be a JSON object, got {type(data).__name__}"
        raise typer.BadParameter(msg)
    try:
        return EvidencePackage.from_json(data)
    except (ValueError, TypeError) as exc:
        msg = f"evidence package {package} is malformed: {exc}"
        raise typer.BadParameter(msg) from exc


@verify_gate_app.command("assemble")
def assemble_cmd(  # noqa: PLR0913 — one flat CLI flag per primitive evidence input
    *,
    change_class: Annotated[
        list[str],
        typer.Option(
            "--change-class",
            help=(
                "Change-class label(s): core | schema | pipeline | annotation. Repeatable; "
                "a change may carry several (e.g. schema + pipeline)."
            ),
        ),
    ],
    step: Annotated[
        list[str],
        typer.Option(
            "--step",
            help=(
                "A verification step as 'name:exit_code' (e.g. 'pytest:0'). Repeatable; an "
                "empty exit code (e.g. 'pytest:') is UNKNOWN."
            ),
        ),
    ],
    anchor: Annotated[
        list[str] | None,
        typer.Option(
            "--anchor",
            help=(
                "A real-data anchor as 'name=<col>,expected=<v>,actual=<v>[,deferred=true]'. "
                "Repeatable. Omit entirely for the N/A path (no anchors apply)."
            ),
        ),
    ] = None,
    changelog_present: Annotated[
        bool,
        typer.Option(
            "--changelog-present/--no-changelog-present",
            help="A [Unreleased] CHANGELOG entry was added. Fail-closed default: absent.",
        ),
    ] = False,
    docs_check_clean: Annotated[
        bool,
        typer.Option(
            "--docs-check-clean/--no-docs-check-clean",
            help="`genome docs check` exited 0. Fail-closed default: not clean.",
        ),
    ] = False,
    weakened_or_removed_test: Annotated[
        bool,
        typer.Option(
            "--weakened-or-removed-test/--no-weakened-or-removed-test",
            help="A test was weakened/removed. Fail-closed default: assume weakened.",
        ),
    ] = True,
    gate_fill_survivor: Annotated[
        bool,
        typer.Option(
            "--gate-fill-survivor/--no-gate-fill-survivor",
            help="A GATE-FILL sentinel survived the diff. Fail-closed default: assume survived.",
        ),
    ] = True,
    test_count_before: Annotated[
        int | None,
        typer.Option("--test-count-before", help="Collected test count before the change."),
    ] = None,
    test_count_after: Annotated[
        int | None,
        typer.Option("--test-count-after", help="Collected test count after the change."),
    ] = None,
    rebuild_pending: Annotated[
        bool,
        typer.Option(
            "--rebuild-pending/--no-rebuild-pending",
            help="A schema DB rebuild is still owed. Fail-closed default: pending.",
        ),
    ] = True,
    out: Annotated[
        Path,
        typer.Option("--out", help="Path to write the assembled evidence.json."),
    ] = Path("evidence.json"),
) -> None:
    """Build an EvidencePackage from flat args and write it to ``evidence.json``.

    The skill passes only flat primitive strings; the frozen dataclasses are constructed
    here (mypy-checked) so bash never assembles nested JSON.
    """
    steps = tuple(_parse_step(s) for s in step)
    anchors = tuple(_parse_anchor(a) for a in (anchor or []))
    pkg = EvidencePackage(
        change_class=frozenset(change_class),
        steps=steps,
        anchors=anchors,
        integrity=IntegrityFlags(
            changelog_present=changelog_present,
            docs_check_clean=docs_check_clean,
            weakened_or_removed_test=weakened_or_removed_test,
            gate_fill_survivor=gate_fill_survivor,
            test_count_before=test_count_before,
            test_count_after=test_count_after,
        ),
        rebuild_pending=rebuild_pending,
    )
    out.write_text(json.dumps(pkg.to_json(), indent=2, sort_keys=True), encoding="utf-8")
    typer.echo(f"wrote evidence package: {out}")


@verify_gate_app.command("verdict")
def verdict_cmd(
    *,
    package: Annotated[
        Path,
        typer.Option(
            "--package",
            exists=False,
            help="Path to the evidence.json produced by `verify-gate assemble`.",
        ),
    ],
) -> None:
    """Reduce an evidence package and exit non-zero on BLOCKED or UNKNOWN.

    A clean ``GREEN`` exits 0 and prints the ``merge`` affordance; any other verdict prints
    the blocking reason and exits non-zero — the signal the skill stops on (no merge).
    """
    pkg = _load_package(package)
    verdict = reduce_verdict(pkg)
    if verdict is Verdict.GREEN:
        typer.echo("verdict: GREEN")
        typer.echo("All decidable checks passed. Type `merge` to squash-merge and close.")
        return
    # BLOCKED or UNKNOWN: print the stop reason, offer NO merge affordance, exit non-zero.
    typer.echo(f"verdict: {verdict.value.upper()}", err=True)
    typer.echo(
        "The gate did not clear — stop. Resolve the flagged signals and re-run; "
        "no squash/close happens.",
        err=True,
    )
    raise typer.Exit(code=1)


@verify_gate_app.command("format")
def format_cmd(
    *,
    package: Annotated[
        Path,
        typer.Option(
            "--package",
            exists=False,
            help="Path to the evidence.json produced by `verify-gate assemble`.",
        ),
    ],
) -> None:
    """Print the raw, human-readable evidence block for one evidence package."""
    pkg = _load_package(package)
    typer.echo(format_evidence(pkg))

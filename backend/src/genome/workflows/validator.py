"""The unified ``genome workflows check`` reversal-gate (C2+D Phase 2 / finding-034 / DEC-0122).

Runs two fail-closed checks over the three self-contained per-scope-team workflows:

* **seam-drift** — the duplicated ``agent()``/retry seam (GT-1 forbids a shared import) must stay
  logically identical across ``plan-phase`` / ``implement-review`` / ``close``;
* **schema-validity** — every ``SCHEMAS`` entry must declare ``type: 'object'`` (locks the PR 1
  400-fix against regression).

This module — and everything it imports — pulls in **no** :mod:`genome.db`; the gate is pure
filesystem text inspection and runs on a fresh checkout (mirrors :mod:`genome.docs`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genome.workflows.model import (
    SCHEMA_MISSING_TYPE,
    SCHEMAS_NOT_LOCATED,
    SEAM_DRIFT,
    SEAM_NOT_LOCATED,
    WORKFLOW_FILE_MISSING,
    GateReport,
    GateViolation,
)
from genome.workflows.schemas import extract_schemas_block, schema_entry_keys_without_type
from genome.workflows.seam import extract_seam, normalize_seam

if TYPE_CHECKING:
    from pathlib import Path

# The three engine-dialect orchestrators the gate guards (the stem is also the per-file
# normalization label — see :func:`genome.workflows.seam.normalize_seam`).
WORKFLOW_STEMS: tuple[str, ...] = ("plan-phase", "implement-review", "close")


def _load_bodies(repo_root: Path) -> tuple[dict[str, str], list[GateViolation]]:
    """Read the three workflow files; a missing file is a fail-closed violation."""
    wf_dir = repo_root / ".claude" / "workflows"
    bodies: dict[str, str] = {}
    violations: list[GateViolation] = []
    for stem in WORKFLOW_STEMS:
        path = wf_dir / f"{stem}.js"
        if not path.is_file():
            violations.append(
                GateViolation(
                    WORKFLOW_FILE_MISSING, f"{stem}.js", f"workflow file not found: {path}"
                )
            )
            continue
        bodies[stem] = path.read_text(encoding="utf-8")
    return bodies, violations


def _check_seam_drift(bodies: dict[str, str]) -> list[GateViolation]:
    """Each seam must be locatable (fail-closed) and normalize-identical across the files."""
    violations: list[GateViolation] = []
    norms: dict[str, str] = {}
    for stem, body in bodies.items():
        seam = extract_seam(body)
        if seam is None:
            violations.append(
                GateViolation(
                    SEAM_NOT_LOCATED,
                    f"{stem}.js",
                    f"could not locate the agent()/retry seam in {stem}.js — "
                    f"delimit it with // agent-seam:start / // agent-seam:end",
                )
            )
            continue
        norms[stem] = normalize_seam(seam, stem)
    if len(norms) > 1 and len(set(norms.values())) > 1:
        baseline_stem = next(iter(norms))
        baseline = norms[baseline_stem]
        violations.extend(
            GateViolation(
                SEAM_DRIFT,
                f"{stem}.js",
                f"agent()/retry seam LOGIC drifted between {baseline_stem}.js and "
                f"{stem}.js (GT-1 requires the inlined copies to stay logically identical)",
            )
            for stem, norm in norms.items()
            if norm != baseline
        )
    return violations


def _check_schema_validity(bodies: dict[str, str]) -> list[GateViolation]:
    """Flag any ``SCHEMAS`` entry lacking ``type: 'object'`` (block unlocatable = fail-closed)."""
    violations: list[GateViolation] = []
    for stem, body in bodies.items():
        block = extract_schemas_block(body)
        if block is None:
            violations.append(
                GateViolation(
                    SCHEMAS_NOT_LOCATED,
                    f"{stem}.js",
                    f"could not locate `const SCHEMAS = {{ ... }}` in {stem}.js",
                )
            )
            continue
        violations.extend(
            GateViolation(
                SCHEMA_MISSING_TYPE,
                f"{stem}.js:{key}",
                f"SCHEMAS.{key} lacks a top-level `type: 'object'` — the engine rejects an "
                f"input_schema without `type` (400 input_schema.type: Field required)",
            )
            for key in schema_entry_keys_without_type(block)
        )
    return violations


def check(repo_root: Path) -> GateReport:
    """Run the fail-closed reversal-gate over the three team workflows.

    Returns a :class:`GateReport`; ``report.ok`` is the CLI's exit signal. Pure filesystem read;
    never imports :mod:`genome.db`.
    """
    bodies, violations = _load_bodies(repo_root)
    violations.extend(_check_seam_drift(bodies))
    violations.extend(_check_schema_validity(bodies))
    return GateReport(violations=tuple(violations))

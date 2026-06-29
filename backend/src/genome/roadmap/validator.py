"""The ``genome roadmap check`` source-of-truth gate (finding-042 / DEC-0125).

Runs three fail-closed checks that keep ``ROADMAP.md`` the authoritative scope ledger:

* **id presence** — every top-level (column-0) ``- [ ]`` / ``- [x]`` checklist item carries a
  well-formed ``RM-<7 hex>`` id right after the checkbox;
* **id uniqueness** — no ``RM-`` id is defined on more than one checklist line;
* **referential integrity** — every ``RM-<7 hex>`` token cited in ``docs/findings/*.md`` /
  ``MEMORY.md`` / ``CHANGELOG.md`` resolves to an id defined in ``ROADMAP.md`` (dangling-ref catch).

The machine-managed ``<!-- B2-SUBSCOPES:BEGIN -->``…``<!-- B2-SUBSCOPES:END -->`` region is exempt
(transient, writer-owned), as are indented sub-bullets and prose. This module — and everything it
imports — pulls in **no** :mod:`genome.db`; the gate is pure filesystem text inspection and runs on
a fresh checkout (mirrors :mod:`genome.docs` and :mod:`genome.workflows`).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from genome.roadmap.model import (
    DANGLING_REF,
    DUPLICATE_ID,
    MISSING_ID,
    ROADMAP_FILE_MISSING,
    GateReport,
    GateViolation,
)

if TYPE_CHECKING:
    from pathlib import Path

# An ``RM-`` id is exactly 7 lowercase-hex chars; the negative lookahead rejects an 8th hex digit
# so a malformed longer token never silently matches on a 7-char prefix.
RM_ID = re.compile(r"RM-[0-9a-f]{7}(?![0-9a-f])")
# A top-level checklist item (column 0, no leading whitespace — indented sub-bullets are exempt).
_CHECKLIST = re.compile(r"^- \[[ x]\] ")
# A *well-formed* checklist item: the id sits immediately after the checkbox.
_CHECKLIST_WITH_ID = re.compile(r"^- \[[ x]\] (RM-[0-9a-f]{7})(?![0-9a-f])")

_MANAGED_BEGIN = "<!-- B2-SUBSCOPES:BEGIN -->"
_MANAGED_END = "<!-- B2-SUBSCOPES:END -->"

# Files that may cite an RM- id and must not dangle (finding-042). ROADMAP.md itself is the
# definition home and is excluded from the reference scan.
_REFERRING_FILES = ("MEMORY.md", "CHANGELOG.md")
_REFERRING_GLOB = "docs/findings/*.md"


def _extract_definitions(text: str) -> tuple[list[tuple[str, int]], list[GateViolation]]:
    """Return ``(definitions, missing)``.

    ``definitions`` is each defined ``(id, line_no)``; ``missing`` is a MISSING_ID violation per
    column-0 checklist item (outside the managed region) lacking a well-formed id.
    """
    definitions: list[tuple[str, int]] = []
    missing: list[GateViolation] = []
    in_managed = False
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped == _MANAGED_BEGIN:
            in_managed = True
            continue
        if stripped == _MANAGED_END:
            in_managed = False
            continue
        if in_managed or not _CHECKLIST.match(line):
            continue
        matched = _CHECKLIST_WITH_ID.match(line)
        if matched is None:
            missing.append(
                GateViolation(
                    MISSING_ID,
                    f"ROADMAP.md:{idx}",
                    "top-level checklist item lacks a well-formed `RM-<7 hex>` id right after "
                    f"the checkbox: {line[:80]!r}",
                )
            )
            continue
        definitions.append((matched.group(1), idx))
    return definitions, missing


def _check_uniqueness(definitions: list[tuple[str, int]]) -> list[GateViolation]:
    """Flag any ``RM-`` id defined on more than one checklist line."""
    seen: dict[str, int] = {}
    violations: list[GateViolation] = []
    for rid, line_no in definitions:
        if rid in seen:
            violations.append(
                GateViolation(
                    DUPLICATE_ID,
                    f"ROADMAP.md:{line_no}",
                    f"id {rid} is already defined at line {seen[rid]} — RM- ids must be unique",
                )
            )
        else:
            seen[rid] = line_no
    return violations


def _check_references(repo_root: Path, defined: set[str]) -> list[GateViolation]:
    """Flag any ``RM-<7 hex>`` token in a referring doc not defined in ROADMAP.md."""
    violations: list[GateViolation] = []
    paths = [repo_root / name for name in _REFERRING_FILES]
    paths.extend(sorted(repo_root.glob(_REFERRING_GLOB)))
    for path in paths:
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        tokens = sorted(set(RM_ID.findall(path.read_text(encoding="utf-8"))))
        violations.extend(
            GateViolation(
                DANGLING_REF,
                str(rel),
                f"cites {rid}, which is not defined in ROADMAP.md (use the non-hex "
                "placeholder `RM-xxxxxxx` for illustrative examples)",
            )
            for rid in tokens
            if rid not in defined
        )
    return violations


def check(repo_root: Path) -> GateReport:
    """Run the fail-closed source-of-truth gate over ROADMAP.md + its referring docs.

    Returns a :class:`GateReport`; ``report.ok`` is the CLI's exit signal. Pure filesystem read;
    never imports :mod:`genome.db`.
    """
    roadmap = repo_root / "ROADMAP.md"
    if not roadmap.is_file():
        return GateReport(
            violations=(
                GateViolation(
                    ROADMAP_FILE_MISSING, "ROADMAP.md", f"ROADMAP.md not found at {roadmap}"
                ),
            )
        )
    text = roadmap.read_text(encoding="utf-8")
    definitions, violations = _extract_definitions(text)
    violations.extend(_check_uniqueness(definitions))
    violations.extend(_check_references(repo_root, {rid for rid, _ in definitions}))
    return GateReport(violations=tuple(violations))

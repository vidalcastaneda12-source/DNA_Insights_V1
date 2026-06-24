"""``genome docs build-index`` — the single-index retrieval surface (plan Task 4).

Regenerates the findings-index table inside the marker block of ``docs/findings/README.md``,
deriving each finding's status and supersession cross-links from its frontmatter — frontmatter
is authoritative, the ledger's cross-links are *derived* (plan §0 resolution #4). Imports no
:mod:`genome.db`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from genome.docs.frontmatter import parse_frontmatter
from genome.docs.model import (
    INDEX_BEGIN_MARKER,
    INDEX_END_MARKER,
    Frontmatter,
    IndexResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_FINDING_ID_RE = re.compile(r"^(finding-\d+)")
_INDEX_HEADER = "| finding | type | status | supersedes | superseded_by |"
_INDEX_SEPARATOR = "|---|---|---|---|---|"


def _finding_id(filename: str) -> str | None:
    """Extract the ``finding-NNN`` stable id from a finding filename, or ``None``."""
    match = _FINDING_ID_RE.match(filename)
    return match.group(1) if match else None


def _normalize(text: str) -> str:
    """Strip trailing whitespace per line — the normalize half of normalize-then-compare."""
    return "\n".join(line.rstrip() for line in text.splitlines())


def render_index_table(findings: Sequence[tuple[str, Frontmatter]]) -> str:
    """Render the findings-index markdown table (header + rows), ending with a newline.

    ``findings`` is a sequence of ``(finding_id, frontmatter)``; rows are emitted in
    finding-id order so the render is deterministic (the normalize-then-compare idempotence
    unit). Each row carries the id, type, status, and the derived supersedes/superseded_by
    cross-links.
    """
    lines = [_INDEX_HEADER, _INDEX_SEPARATOR]
    for fid, frontmatter in sorted(findings, key=lambda item: item[0]):
        supersedes = ", ".join(frontmatter.supersedes)
        superseded_by = ", ".join(frontmatter.superseded_by)
        lines.append(
            f"| {fid} | {frontmatter.type} | {frontmatter.status} "
            f"| {supersedes} | {superseded_by} |",
        )
    return "\n".join(lines) + "\n"


def _read_findings(findings_dir: Path) -> list[tuple[str, Frontmatter]]:
    """Read every ``finding-*.md`` frontmatter (skipping unparseable ones)."""
    out: list[tuple[str, Frontmatter]] = []
    for path in sorted(findings_dir.glob("finding-*.md")):
        fid = _finding_id(path.name)
        if fid is None:
            continue
        try:
            frontmatter = parse_frontmatter(path.read_text(encoding="utf-8"))
        except ValueError:
            continue  # malformed frontmatter is a CAPTURE concern of `check`, not the index
        if frontmatter is not None:
            out.append((fid, frontmatter))
    return out


def _count_cross_links(findings: Sequence[tuple[str, Frontmatter]]) -> int:
    """Count distinct directed supersession edges derived from frontmatter."""
    edges: set[tuple[str, str]] = set()
    for fid, frontmatter in findings:
        for target in frontmatter.supersedes:
            edges.add((fid, target))
        for target in frontmatter.superseded_by:
            edges.add((target, fid))
    return len(edges)


def _splice_index(readme_text: str, table: str) -> str:
    """Replace the span between the index markers with ``table``, preserving all prose."""
    begin = readme_text.index(INDEX_BEGIN_MARKER)
    end = readme_text.index(INDEX_END_MARKER)
    prefix = readme_text[: begin + len(INDEX_BEGIN_MARKER)]
    suffix = readme_text[end:]
    return f"{prefix}\n{table}{suffix}"


def build_index(repo_root: Path, *, write: bool = True) -> IndexResult:
    """Regenerate the findings-index marker block in the findings README.

    Reads every ``docs/findings/finding-*.md`` frontmatter, renders the index table, and
    splices it between the markers, preserving all hand-authored README prose outside them.
    When ``write`` is False, computes the render and reports ``changed`` without touching disk
    (the dry-run the Task-5 bulk-apply gates on). Idempotence is **normalize-then-compare**:
    table padding and trailing whitespace are normalised, so a second run is a no-op even if a
    formatter reflows the table.
    """
    findings_dir = repo_root / "docs" / "findings"
    findings = _read_findings(findings_dir)
    table = render_index_table(findings)

    readme_path = findings_dir / "README.md"
    current = readme_path.read_text(encoding="utf-8")
    rendered = _splice_index(current, table)
    changed = _normalize(rendered) != _normalize(current)

    if write and changed:
        readme_path.write_text(rendered, encoding="utf-8")

    return IndexResult(
        changed=changed,
        rendered=rendered,
        findings_indexed=len(findings),
        cross_links_derived=_count_cross_links(findings),
    )

"""The unified ``genome docs check`` gate — where locked decision #7 lives (plan Task 3).

A markdown ledger has no transaction and no reader-isolation, so the no-torn-state /
"never UPDATE active content" invariant cannot be enforced by the substrate. The plan
**relocates** that invariant here: ``check`` hard-fails on any violation across three
dimensions (CAPTURE / RETRIEVAL / LIFECYCLE), and the CLI turns a non-empty report into a
non-zero exit.

This module imports **no** :mod:`genome.db` — it is pure filesystem (plus an optional ``git``
baseline read for the content-immutability rule). ``genome docs check`` must run on a fresh
checkout with no DuckDB / SQLCipher built.

The violation-code constants are re-exported from :mod:`genome.docs.model` so callers can
refer to them as ``validator.DUPLICATE_DEC_ID`` etc.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import structlog

from genome.docs.frontmatter import FrontmatterError, parse_frontmatter
from genome.docs.index import build_index
from genome.docs.ledger import LedgerError, iter_data_rows, parse_ledger, row_from_cells
from genome.docs.model import (
    BAD_KIND_VOCAB,
    BAD_STATUS_VOCAB,
    BAD_TYPE_VOCAB,
    CODE_DIMENSION,
    COPIED_ANCHOR_NUMBER,
    DECISION_WITHOUT_DEC_ROW,
    DUPLICATE_DEC_ID,
    INDEX_BEGIN_MARKER,
    INDEX_END_MARKER,
    INPLACE_CONTENT_EDIT,
    LIFECYCLE,
    MALFORMED_FRONTMATTER,
    MALFORMED_LEDGER_ROW,
    MISSING_FRONTMATTER,
    MISSING_INDEX_MARKER,
    MISSING_PROVENANCE,
    MULTIPLE_SUPERSEDERS,
    NON_CANONICAL_ACTOR,
    NON_MONOTONIC_DEC_ID,
    ORPHAN_SUPERSESSION,
    STALE_INDEX,
    SUPERSEDED_WITHOUT_POINTER,
    UNRESOLVED_CROSS_REF,
    CheckReport,
    CheckViolation,
    Frontmatter,
    LedgerRow,
)

_log = structlog.get_logger("genome.docs.validator")

__all__ = [
    "BAD_KIND_VOCAB",
    "BAD_STATUS_VOCAB",
    "BAD_TYPE_VOCAB",
    "COPIED_ANCHOR_NUMBER",
    "DECISION_WITHOUT_DEC_ROW",
    "DUPLICATE_DEC_ID",
    "INPLACE_CONTENT_EDIT",
    "MALFORMED_FRONTMATTER",
    "MISSING_FRONTMATTER",
    "MISSING_INDEX_MARKER",
    "MISSING_PROVENANCE",
    "MULTIPLE_SUPERSEDERS",
    "NON_CANONICAL_ACTOR",
    "NON_MONOTONIC_DEC_ID",
    "ORPHAN_SUPERSESSION",
    "STALE_INDEX",
    "SUPERSEDED_WITHOUT_POINTER",
    "UNRESOLVED_CROSS_REF",
    "anchor_numbers",
    "check",
]

_FINDING_ID_RE = re.compile(r"^(finding-\d+)")
_DEC_NUM_RE = re.compile(r"\d+")
# Comma-grouped magnitudes (≥ 4 digits) are the anchor shape; small ungrouped numbers
# (dates, PR numbers, fold counts) are not anchors.
_ANCHOR_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+")
# A number sitting next to one of these is a tolerance-banded anchor — never frozen as a
# scalar (CLAUDE.md "tolerance-banded, not exact"), so it is excluded from the guard set.
_TOLERANCE_RE = re.compile(r"tolerance-banded|±|~")
_REAL_DATA_HEADING = "## Real-data observations"
_TERMINAL_STATUSES: frozenset[str] = frozenset({"superseded", "reversed"})


@dataclass(frozen=True, slots=True)
class _FindingInfo:
    """One finding's parse outcome — the frontmatter, or the code its parse failed with."""

    fid: str
    path: Path
    frontmatter: Frontmatter | None
    error_code: str | None


def _violation(code: str, location: str, message: str) -> CheckViolation:
    return CheckViolation(
        dimension=CODE_DIMENSION.get(code, LIFECYCLE),
        code=code,
        location=location,
        message=message,
    )


def _finding_id(name: str) -> str | None:
    match = _FINDING_ID_RE.match(name)
    return match.group(1) if match else None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _real_data_section(claude_md_text: str) -> str:
    start = claude_md_text.find(_REAL_DATA_HEADING)
    if start == -1:
        return ""
    rest = claude_md_text[start + len(_REAL_DATA_HEADING) :]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


def anchor_numbers(claude_md_text: str) -> frozenset[str]:
    """Extract the real-data anchor numbers from CLAUDE.md's "Real-data observations".

    These are the imputation / index / consensus digits the ledger must **reference**, never
    transcribe (plan §3 anchor-drift blocker). A comma-grouped number whose immediate context
    carries a tolerance marker (``tolerance-banded`` / ``±`` / ``~``) is excluded — those are
    never frozen as scalars — so the guard set is exact scalars only.
    """
    section = _real_data_section(claude_md_text)
    matches = list(_ANCHOR_NUM_RE.finditer(section))
    out: set[str] = set()
    for idx, match in enumerate(matches):
        window_end = (
            matches[idx + 1].start()
            if idx + 1 < len(matches)
            else min(len(section), match.end() + 80)
        )
        if not _TOLERANCE_RE.search(section[match.end() : window_end]):
            out.add(match.group(0))
    return frozenset(out)


def _load_findings(findings_dir: Path) -> list[_FindingInfo]:
    out: list[_FindingInfo] = []
    if not findings_dir.is_dir():
        return out
    for path in sorted(findings_dir.glob("finding-*.md")):
        fid = _finding_id(path.name)
        if fid is None:
            continue
        try:
            frontmatter = parse_frontmatter(path.read_text(encoding="utf-8"))
        except FrontmatterError as err:
            out.append(_FindingInfo(fid, path, None, err.code))
        else:
            out.append(_FindingInfo(fid, path, frontmatter, None))
    return out


def _load_ledger(path: Path) -> tuple[list[LedgerRow], list[CheckViolation]]:
    """Parse ``MEMORY.md`` leniently: collect a violation per malformed row **and** keep the
    parseable rows so the structural checks still run on the good subset.

    Uses the lenient ``row_from_cells`` core (which returns ``(None, code)`` instead of
    raising) — built for exactly this. ``parse_ledger``'s raise-on-first is for the render /
    round-trip path, not the gate, which must report every violation in one pass.
    """
    text = _read_text(path)
    rows: list[LedgerRow] = []
    violations: list[CheckViolation] = []
    for line_no, cells in iter_data_rows(text):
        row, code = row_from_cells(cells)
        if row is None:
            violations.append(
                _violation(
                    code or MALFORMED_LEDGER_ROW,
                    f"MEMORY.md:{line_no}",
                    "ledger row failed to parse",
                ),
            )
        else:
            rows.append(row)
    return rows, violations


def _finding_violations(findings: list[_FindingInfo]) -> list[CheckViolation]:
    out: list[CheckViolation] = []
    for info in findings:
        if info.error_code is not None:
            out.append(_violation(info.error_code, str(info.path), "malformed frontmatter"))
        elif info.frontmatter is None:
            out.append(_violation(MISSING_FRONTMATTER, str(info.path), "no frontmatter block"))
    return out


def _dec_num(dec: str) -> int | None:
    match = _DEC_NUM_RE.search(dec)
    return int(match.group()) if match else None


def _dec_id_violations(rows: list[LedgerRow]) -> list[CheckViolation]:
    out: list[CheckViolation] = []
    seen: set[str] = set()
    for row in rows:
        if row.dec in seen:
            out.append(_violation(DUPLICATE_DEC_ID, row.dec, "duplicate DEC id"))
        seen.add(row.dec)
    nums = [_dec_num(row.dec) for row in rows]
    for prev, cur in pairwise(nums):
        if prev is not None and cur is not None and cur <= prev:
            out.append(
                _violation(NON_MONOTONIC_DEC_ID, "MEMORY.md", "DEC ids are not strictly increasing")
            )
            break
    return out


def _supersession_violations(rows: list[LedgerRow]) -> list[CheckViolation]:
    out: list[CheckViolation] = []
    dec_ids = {row.dec for row in rows}
    targets = [row.superseded_by for row in rows if row.superseded_by is not None]
    for row in rows:
        if row.status in _TERMINAL_STATUSES and row.superseded_by is None:
            out.append(
                _violation(
                    SUPERSEDED_WITHOUT_POINTER, row.dec, f"{row.status} row has no superseded_by"
                )
            )
        if row.superseded_by is not None and row.superseded_by not in dec_ids:
            out.append(
                _violation(
                    ORPHAN_SUPERSESSION, row.dec, f"superseded_by {row.superseded_by} not in ledger"
                )
            )
    for target, count in Counter(targets).items():
        if count > 1:
            out.append(_violation(MULTIPLE_SUPERSEDERS, target, f"{count} rows supersede {target}"))
    return out


def _row_value_violations(rows: list[LedgerRow], anchors: frozenset[str]) -> list[CheckViolation]:
    out: list[CheckViolation] = []
    for row in rows:
        if not row.provenance.strip():
            out.append(
                _violation(
                    MISSING_PROVENANCE, row.dec, "empty provenance (use 'unknown' if unrecoverable)"
                )
            )
        copied = next((a for a in anchors if a in row.decision), None)
        if copied is not None:
            out.append(
                _violation(
                    COPIED_ANCHOR_NUMBER,
                    row.dec,
                    f"anchor {copied} copied verbatim; reference it instead",
                )
            )
    return out


def _crossref_violations(
    findings: list[_FindingInfo], rows: list[LedgerRow]
) -> list[CheckViolation]:
    valid = {info.fid for info in findings} | {row.dec for row in rows}
    out: list[CheckViolation] = []
    for info in findings:
        if info.frontmatter is None:
            continue
        pointers = (*info.frontmatter.supersedes, *info.frontmatter.superseded_by)
        out.extend(
            _violation(UNRESOLVED_CROSS_REF, str(info.path), f"pointer {pointer} does not resolve")
            for pointer in pointers
            if pointer not in valid
        )
    return out


def _decision_row_violations(
    findings: list[_FindingInfo], rows: list[LedgerRow]
) -> list[CheckViolation]:
    referenced = {
        fid for row in rows if (fid := _finding_id(Path(row.detail_link).name)) is not None
    }
    out: list[CheckViolation] = []
    for info in findings:
        fm = info.frontmatter
        if fm is not None and fm.type in {"decision", "both"} and info.fid not in referenced:
            out.append(
                _violation(
                    DECISION_WITHOUT_DEC_ROW,
                    str(info.path),
                    f"{info.fid} is a decision with no DEC row",
                )
            )
    return out


def _content_columns(row: LedgerRow) -> tuple[str, str, tuple[str, ...], str, str, str]:
    """The immutable content columns — everything except ``status`` / ``superseded_by``
    (matches MEMORY.md's documented immutable set, ``date`` included)."""
    return row.kind, row.date, row.actors, row.provenance, row.decision, row.detail_link


def _git_run(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run ``git -C repo_root <args>``; ``None`` if git is unavailable / not a repo."""
    try:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_root), *args],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _git_toplevel(repo_root: Path) -> Path | None:
    """The git work-tree root for ``repo_root``, or ``None`` if git is absent / not a repo."""
    result = _git_run(repo_root, "rev-parse", "--show-toplevel")
    if result is None or result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _git_baseline(repo_root: Path, rel: str) -> str | None:
    result = _git_run(repo_root, "show", f"HEAD:{rel}")
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def _inplace_violations(repo_root: Path, rows: list[LedgerRow]) -> list[CheckViolation]:
    if not rows:
        return []
    # Diff against the git baseline only when repo_root IS the git work-tree root — otherwise
    # `git show HEAD:MEMORY.md` resolves an ancestor's MEMORY.md, not this one. A missing git
    # or a nested checkout skips the immutability check; log it so the skip is observable
    # (fail-open is substrate-honest here, but it must not be silent).
    toplevel = _git_toplevel(repo_root)
    if toplevel is None or toplevel != repo_root.resolve():
        _log.debug(
            "docs.check.inplace_skipped", repo_root=str(repo_root), git_toplevel=str(toplevel)
        )
        return []
    baseline = _git_baseline(repo_root, "MEMORY.md")
    if baseline is None:
        return []  # MEMORY.md not yet in HEAD (the PR introducing it) — no baseline to diff
    try:
        base_by_id = {row.dec: row for row in parse_ledger(baseline)}
    except LedgerError:
        return []
    out: list[CheckViolation] = []
    for row in rows:
        prior = base_by_id.get(row.dec)
        if prior is not None and _content_columns(prior) != _content_columns(row):
            out.append(
                _violation(
                    INPLACE_CONTENT_EDIT,
                    row.dec,
                    "content column edited in place; supersede instead",
                )
            )
    return out


def _retrieval_violations(repo_root: Path) -> list[CheckViolation]:
    # A missing README or absent index markers is a RETRIEVAL failure, NOT a no-op: the
    # retrieval surface is broken exactly when it most needs flagging. Detect it explicitly
    # rather than swallowing the ValueError build_index would raise on absent markers.
    readme = "docs/findings/README.md"
    text = _read_text(repo_root / "docs" / "findings" / "README.md")
    if not text:
        return [_violation(MISSING_INDEX_MARKER, readme, "findings README missing or unreadable")]
    if INDEX_BEGIN_MARKER not in text or INDEX_END_MARKER not in text:
        return [
            _violation(
                MISSING_INDEX_MARKER,
                readme,
                "findings-index markers absent; add the BEGIN/END block then run build-index",
            ),
        ]
    try:
        result = build_index(repo_root, write=False)
    except (OSError, ValueError) as err:
        _log.warning("docs.check.index_unbuildable", error=str(err))
        return [
            _violation(
                MISSING_INDEX_MARKER, readme, f"findings index could not be regenerated: {err}"
            )
        ]
    if result.changed:
        return [
            _violation(
                STALE_INDEX, readme, "findings index is stale; run `genome docs build-index`"
            )
        ]
    return []


def check(repo_root: Path) -> CheckReport:
    """Run the unified CAPTURE / RETRIEVAL / LIFECYCLE gate over a repo tree.

    Returns a :class:`CheckReport`; ``report.ok`` is the CLI's exit signal. See the module
    docstring and :data:`genome.docs.model.CODE_DIMENSION` for the full code set. Pure
    filesystem read plus an optional ``git`` baseline; never imports :mod:`genome.db`.
    """
    findings = _load_findings(repo_root / "docs" / "findings")
    rows, ledger_violations = _load_ledger(repo_root / "MEMORY.md")
    claude_text = _read_text(repo_root / "CLAUDE.md")
    if not claude_text:
        # An unreadable anchor source silently empties the COPIED_ANCHOR_NUMBER guard — log so
        # the disabled guard is observable rather than a silent pass.
        _log.debug("docs.check.anchor_source_unreadable", path=str(repo_root / "CLAUDE.md"))
    anchors = anchor_numbers(claude_text)

    violations: list[CheckViolation] = []
    violations += _finding_violations(findings)
    violations += ledger_violations
    violations += _dec_id_violations(rows)
    violations += _supersession_violations(rows)
    violations += _row_value_violations(rows, anchors)
    violations += _crossref_violations(findings, rows)
    violations += _decision_row_violations(findings, rows)
    violations += _inplace_violations(repo_root, rows)
    violations += _retrieval_violations(repo_root)
    return CheckReport(violations=tuple(violations))

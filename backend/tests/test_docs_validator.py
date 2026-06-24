"""Validator gate — ``genome.docs.validator.check`` (the enforcement core).

Plan-blind spec source: the approved ``decision-tracking-leak-fix`` plan §5 (Lifecycle,
Integrity, Anchor-reference guard, Negative tests, Provenance) + §6 (the single gate;
the extended negative control), and the frozen interface contract (validator violation
codes + behavioural contracts #2, #3, #4, #5, #7). Expected values come from that spec —
never from the stubbed ``validator.py`` body (``raise NotImplementedError`` by design;
RED is correct).

This module builds a **fixture repo on disk** (``CLAUDE.md`` + ``MEMORY.md`` + a couple of
``docs/findings/`` files + a README index marker block) modelled on the real repo shapes
(finding-013 realism: the ``MEMORY.md`` worked example, the on-disk frontmatter block, a
real CLAUDE.md anchor digit). The clean baseline is then mutated one axis at a time so
each negative test pins exactly one violation code.

``check(repo_root)`` is pure filesystem + an optional ``git`` baseline; it imports no
``genome.db`` (the no-DB-import property is asserted separately in
``test_docs_no_db_import.py``).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from genome.docs import validator
from genome.docs.model import INDEX_BEGIN_MARKER, INDEX_END_MARKER
from genome.docs.validator import anchor_numbers, check

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixture-repo building blocks (real on-disk shapes; finding-013 realism).
# ---------------------------------------------------------------------------

# A real exact-scalar anchor from CLAUDE.md "Real-data observations" #3 (variants_master
# total = 3,160,364). Deterministic, NOT tolerance-banded — a legitimate COPIED_ANCHOR
# target. A separate tolerance-banded number (chrX yield ±~100) is deliberately NOT used
# as a guard target (the contract excludes those from ``anchor_numbers``).
_EXACT_ANCHOR = "3,160,364"

_CLAUDE_MD = (
    "# Project Context — DNA Insights App\n"
    "\n"
    "## Real-data observations\n"
    "\n"
    "3. Phase 4 Beagle imputation. Post-chrX re-lock: `consensus_total` = "
    f"`variants_master` total **{_EXACT_ANCHOR}**; chrX yield is **92,832** total kept "
    "(tolerance-banded ±~100, never frozen as a scalar).\n"
)


def _finding(  # noqa: PLR0913 — a fixture builder; each kwarg is one frontmatter key
    *,
    number: str,
    type_: str = "observation",
    status: str = "active",
    actors: str = "[VSC-User]",
    date: str = "2026-06-23",
    supersedes: str = "[]",
    superseded_by: str = "[]",
    title: str = "a finding",
) -> str:
    """Render a finding file: a frontmatter block prepended above its ``# Finding`` H1."""
    return (
        "---\n"
        f"type: {type_}\n"
        f"status: {status}\n"
        f"actors: {actors}\n"
        f"date: {date}\n"
        f"supersedes: {supersedes}\n"
        f"superseded_by: {superseded_by}\n"
        "---\n"
        f"# Finding {number} — {title}\n"
        "\n"
        "Body text below the fence.\n"
    )


def _ledger(rows: str) -> str:
    """Render a ``MEMORY.md`` with header + separator + the supplied data ``rows``."""
    columns = (
        "| DEC | kind | date | status | superseded_by | actors | provenance |"
        " decision | detail-link |\n"
    )
    separator = "|---|---|---|---|---|---|---|---|---|\n"
    return (
        "# MEMORY — decision ledger\n"
        "\n"
        "Insert-then-flip is the only sanctioned transition.\n"
        "\n"
        "<!-- BEGIN decision-ledger -->\n"
        "\n" + columns + separator + rows + "\n"
        "<!-- END decision-ledger -->\n"
    )


def _readme_with_index(body: str = "") -> str:
    """A findings README carrying the generated index marker block (initially empty)."""
    return (
        "# Findings\n"
        "\n"
        "Hand-authored preamble that build-index must preserve.\n"
        "\n"
        f"{INDEX_BEGIN_MARKER}\n"
        f"{body}"
        f"{INDEX_END_MARKER}\n"
        "\n"
        "Hand-authored trailer that build-index must preserve.\n"
    )


def _write_repo(
    root: Path,
    *,
    findings: dict[str, str],
    ledger: str,
    readme: str | None = None,
    claude_md: str = _CLAUDE_MD,
) -> None:
    """Materialise a minimal repo: CLAUDE.md, MEMORY.md, docs/findings/*, README."""
    (root / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    (root / "MEMORY.md").write_text(ledger, encoding="utf-8")
    findings_dir = root / "docs" / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    for name, text in findings.items():
        (findings_dir / name).write_text(text, encoding="utf-8")
    (findings_dir / "README.md").write_text(
        readme if readme is not None else _readme_with_index(),
        encoding="utf-8",
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed argv, no shell, test-controlled git baseline
        ["git", *args],  # noqa: S607 — `git` resolved from PATH is fine in tests
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_commit_all(root: Path) -> None:
    """Init a git repo at ``root`` and commit everything (the validator baseline)."""
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")


def _codes(report: object) -> set[str]:
    """The set of violation codes present in a ``CheckReport``."""
    return {v.code for v in report.violations}  # type: ignore[attr-defined]


# A clean, well-formed two-row supersession ledger (DEC-0001 superseded by DEC-0002).
_CLEAN_LEDGER_ROWS = (
    "| DEC-0001 | architectural | 2026-05-22 | superseded | DEC-0002 | VSC-User |"
    " finding-011 | gnomAD filter scoped three-way; see CLAUDE.md obs #4 |"
    " docs/findings/finding-011-gnomad-three-way-intersection.md |\n"
    "| DEC-0002 | architectural | 2026-06-21 | active | — | VSC-User |"
    " finding-035 | gnomAD filter narrowed to user_only |"
    " docs/findings/finding-035-gnomad-filter-set-consumer-audit.md |\n"
)


def _clean_findings() -> dict[str, str]:
    """A pair of findings whose frontmatter resolves the DEC supersession edge."""
    return {
        "finding-011-gnomad-three-way-intersection.md": _finding(
            number="011",
            type_="decision",
            status="superseded",
            superseded_by="[finding-035]",
            title="gnomAD three-way intersection",
        ),
        "finding-035-gnomad-filter-set-consumer-audit.md": _finding(
            number="035",
            type_="decision",
            status="active",
            supersedes="[finding-011]",
            title="gnomAD filter-set consumer audit",
        ),
    }


# ---------------------------------------------------------------------------
# anchor_numbers — the digits to guard against (contract: tolerance-banded excluded).
# ---------------------------------------------------------------------------


def test_anchor_numbers_includes_exact_scalar() -> None:
    """from: plan §3 anchors / contract ``anchor_numbers`` (exact scalars guarded).

    An exact-scalar anchor (``variants_master`` total = 3,160,364) is in the guarded set.
    """
    assert _EXACT_ANCHOR in anchor_numbers(_CLAUDE_MD)


def test_anchor_numbers_excludes_tolerance_banded() -> None:
    """from: plan §3 (tolerance-banded anchors never frozen as scalars) / contract.

    A tolerance-banded number (the chrX yield 92,832, documented ±~100) is deliberately
    NOT frozen as a guard scalar.
    """
    assert "92,832" not in anchor_numbers(_CLAUDE_MD)


# ---------------------------------------------------------------------------
# Contract #3/#5 — clean baseline is OK; positive control for the gate.
# ---------------------------------------------------------------------------


def test_clean_repo_passes_lifecycle_and_capture(tmp_path: Path) -> None:
    """from: plan §6 single gate (positive control) + contract #3.

    A repo with parseable frontmatter on every finding and a well-formed, cross-resolved
    supersession ledger has NO lifecycle or capture violations. (RETRIEVAL/STALE_INDEX is
    exercised in ``test_docs_index.py``; here we assert the absence of every LIFECYCLE and
    CAPTURE code so a stale-index difference cannot mask a lifecycle pass.)
    """
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(_CLEAN_LEDGER_ROWS))
    report = check(tmp_path)
    lifecycle_codes = {
        validator.DUPLICATE_DEC_ID,
        validator.NON_MONOTONIC_DEC_ID,
        validator.BAD_STATUS_VOCAB,
        validator.BAD_KIND_VOCAB,
        validator.BAD_TYPE_VOCAB,
        validator.SUPERSEDED_WITHOUT_POINTER,
        validator.MULTIPLE_SUPERSEDERS,
        validator.ORPHAN_SUPERSESSION,
        validator.UNRESOLVED_CROSS_REF,
        validator.NON_CANONICAL_ACTOR,
        validator.MISSING_PROVENANCE,
        validator.COPIED_ANCHOR_NUMBER,
        validator.INPLACE_CONTENT_EDIT,
    }
    present = _codes(report)
    assert present.isdisjoint(lifecycle_codes), present
    assert validator.MISSING_FRONTMATTER not in present, present


# ---------------------------------------------------------------------------
# Contract #3 — CAPTURE: a finding with no parseable frontmatter.
# ---------------------------------------------------------------------------


def test_missing_frontmatter_is_capture_violation(tmp_path: Path) -> None:
    """from: plan §6 CAPTURE + contract code ``MISSING_FRONTMATTER``.

    A ``finding-*.md`` with no leading frontmatter fence (a real bare-body finding) is a
    CAPTURE violation and makes the report not-ok.
    """
    findings = _clean_findings()
    findings["finding-001-no-frontmatter.md"] = (
        "# Finding 001 — initial schema sketch\n\nNo frontmatter fence here at all.\n"
    )
    _write_repo(tmp_path, findings=findings, ledger=_ledger(_CLEAN_LEDGER_ROWS))
    report = check(tmp_path)
    assert validator.MISSING_FRONTMATTER in _codes(report)
    assert report.ok is False


# ---------------------------------------------------------------------------
# Contract #3 — LIFECYCLE integrity codes.
# ---------------------------------------------------------------------------


def test_duplicate_dec_id_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: plan §5 integrity (DEC ids unique) + §5 negative (duplicate-active DEC).

    Two rows sharing ``DEC-0001`` → ``DUPLICATE_DEC_ID``; report not-ok.
    """
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | active | — | VSC-User |"
        " PR #1 | first | docs/findings/finding-001.md |\n"
        "| DEC-0001 | tactical | 2026-05-23 | active | — | VSC-User |"
        " PR #2 | duplicate id | docs/findings/finding-002.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.DUPLICATE_DEC_ID in _codes(report)
    assert report.ok is False


def test_non_monotonic_dec_id_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: plan §5 integrity (DEC ids monotonic) + contract ``NON_MONOTONIC_DEC_ID``.

    A ledger that jumps backwards (DEC-0005 then DEC-0002) violates monotonicity.
    """
    rows = (
        "| DEC-0005 | architectural | 2026-05-22 | active | — | VSC-User |"
        " PR #1 | fifth | docs/findings/finding-001.md |\n"
        "| DEC-0002 | tactical | 2026-05-23 | active | — | VSC-User |"
        " PR #2 | out of order | docs/findings/finding-002.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.NON_MONOTONIC_DEC_ID in _codes(report)
    assert report.ok is False


def test_bad_status_vocab_in_ledger_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: plan §5 integrity (closed-vocab status) + contract ``BAD_STATUS_VOCAB``."""
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | retired | — | VSC-User |"
        " PR #1 | bad status | docs/findings/finding-001.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.BAD_STATUS_VOCAB in _codes(report)
    assert report.ok is False


def test_bad_kind_vocab_in_ledger_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: plan §5 integrity (closed-vocab kind) + contract ``BAD_KIND_VOCAB``."""
    rows = (
        "| DEC-0001 | strategic | 2026-05-22 | active | — | VSC-User |"
        " PR #1 | bad kind | docs/findings/finding-001.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.BAD_KIND_VOCAB in _codes(report)
    assert report.ok is False


def test_bad_type_vocab_in_frontmatter_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: plan §5 integrity (closed-vocab type) + contract ``BAD_TYPE_VOCAB``.

    A finding frontmatter ``type`` outside ``FINDING_TYPE_VOCAB`` surfaces as
    ``BAD_TYPE_VOCAB`` from the gate.
    """
    findings = _clean_findings()
    findings["finding-050-bad-type.md"] = _finding(
        number="050",
        type_="opinion",  # not observation|decision|both
        status="active",
        title="a finding with a bad type",
    )
    _write_repo(tmp_path, findings=findings, ledger=_ledger(_CLEAN_LEDGER_ROWS))
    report = check(tmp_path)
    assert validator.BAD_TYPE_VOCAB in _codes(report)
    assert report.ok is False


def test_superseded_without_pointer_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: plan §5 integrity (a superseded row lacking a pointer).

    Code ``SUPERSEDED_WITHOUT_POINTER``: a ``superseded`` row with ``—`` in
    ``superseded_by`` is an orphaned status — the pointer is required.
    """
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | superseded | — | VSC-User |"
        " finding-011 | superseded but no pointer | docs/findings/finding-011.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.SUPERSEDED_WITHOUT_POINTER in _codes(report)
    assert report.ok is False


def test_multiple_superseders_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: plan §5 integrity (EXACTLY ONE superseder) + contract ``MULTIPLE_SUPERSEDERS``.

    Two distinct active rows both claiming to supersede ``DEC-0001`` (each pointing back at
    it) means more than one superseder for one superseded row.
    """
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | superseded | DEC-0002 | VSC-User |"
        " finding-011 | first decision | docs/findings/finding-011.md |\n"
        "| DEC-0002 | architectural | 2026-05-23 | superseded | DEC-0001 | VSC-User |"
        " finding-012 | superseder A pointing back | docs/findings/finding-012.md |\n"
        "| DEC-0003 | architectural | 2026-05-24 | superseded | DEC-0001 | VSC-User |"
        " finding-013 | superseder B pointing back | docs/findings/finding-013.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.MULTIPLE_SUPERSEDERS in _codes(report)
    assert report.ok is False


def test_orphan_supersession_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: plan §5 integrity (NO orphan supersession) + contract ``ORPHAN_SUPERSESSION``.

    ``DEC-0001`` points ``superseded_by`` at ``DEC-0099`` which does not exist in the
    ledger → a dangling supersession edge.
    """
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | superseded | DEC-0099 | VSC-User |"
        " finding-011 | points at a missing superseder | docs/findings/finding-011.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.ORPHAN_SUPERSESSION in _codes(report)
    assert report.ok is False


def test_non_canonical_actor_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: plan §5 negative (non-canonical actor) + contract ``NON_CANONICAL_ACTOR``.

    A ledger row whose actor is neither canonical nor in the legacy map fails the gate.
    """
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | active | — | NovelUnmappedActor |"
        " PR #1 | a decision | docs/findings/finding-001.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.NON_CANONICAL_ACTOR in _codes(report)
    assert report.ok is False


def test_legacy_actor_in_ledger_passes_via_map(tmp_path: Path) -> None:
    """from: plan §5 actor legacy map (existing CHANGELOG names validate via the map).

    A ledger row authored with the real legacy spelling ``VSC-Claude`` does NOT raise a
    ``NON_CANONICAL_ACTOR`` (the legacy map covers it).
    """
    rows = (
        "| DEC-0001 | tactical | 2026-05-22 | active | — | VSC-Claude |"
        " PR #1 | mapped legacy actor | docs/findings/finding-001.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.NON_CANONICAL_ACTOR not in _codes(report)


# ---------------------------------------------------------------------------
# Contract #3/#5/#7 — cross-resolution + provenance.
# ---------------------------------------------------------------------------


def test_unresolved_cross_ref_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: plan §5 negative (dangling finding↔DEC cross-ref) + ``UNRESOLVED_CROSS_REF``.

    A finding frontmatter that declares ``superseded_by: [finding-999]`` where no such
    finding exists is a cross-space pointer that does not resolve — the finding-id ⇄ DEC-id
    cross-resolution fails.
    """
    findings = _clean_findings()
    findings["finding-060-dangling-xref.md"] = _finding(
        number="060",
        type_="decision",
        status="superseded",
        superseded_by="[finding-999]",  # finding-999 does not exist
        title="dangling cross-ref",
    )
    _write_repo(tmp_path, findings=findings, ledger=_ledger(_CLEAN_LEDGER_ROWS))
    report = check(tmp_path)
    assert validator.UNRESOLVED_CROSS_REF in _codes(report)
    assert report.ok is False


def test_missing_provenance_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: plan §5 provenance (never empty/guessed) + contract ``MISSING_PROVENANCE``.

    A backfill row with an EMPTY provenance cell (no ``unknown``, no real source) fails:
    ``unknown`` is the only sanctioned value for an unrecoverable source.
    """
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | active | — | VSC-User |"
        "  | a decision with no provenance | docs/findings/finding-001.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.MISSING_PROVENANCE in _codes(report)
    assert report.ok is False


def test_provenance_unknown_is_accepted(tmp_path: Path) -> None:
    """from: plan §5 provenance (``unknown`` accepted) + §3 decision #8.

    An unrecoverable backfill row carrying the literal ``unknown`` provenance is accepted —
    no ``MISSING_PROVENANCE`` for it.
    """
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | active | — | VSC-User |"
        " unknown | an unrecoverable backfill decision | docs/findings/finding-001.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.MISSING_PROVENANCE not in _codes(report)


# ---------------------------------------------------------------------------
# Contract #4 — anchor-reference guard (COPIED_ANCHOR_NUMBER).
# ---------------------------------------------------------------------------


def test_copied_anchor_number_in_decision_is_violation(tmp_path: Path) -> None:
    """from: plan §5 anchor-reference guard + §6 extended negative control.

    Code ``COPIED_ANCHOR_NUMBER``: a DEC ``decision`` cell that TRANSCRIBES a CLAUDE.md
    anchor digit (3,160,364) verbatim — rather than referencing its home — makes the gate
    fail. CLAUDE.md stays the single source of truth for every anchor.
    """
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | active | — | VSC-User |"
        f" finding-035 | variants_master is now {_EXACT_ANCHOR} rows after canonicalize |"
        " docs/findings/finding-020-canonical-refalt-backfill.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.COPIED_ANCHOR_NUMBER in _codes(report)
    assert report.ok is False


def test_anchor_referenced_not_copied_is_clean(tmp_path: Path) -> None:
    """from: plan §3 (anchors referenced, never copied) + contract #4 (clean side).

    The SAME decision phrased as a reference (``see CLAUDE.md obs #3``) carries no anchor
    digit and so raises no ``COPIED_ANCHOR_NUMBER``.
    """
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | active | — | VSC-User |"
        " finding-035 | variants_master total is re-locked post-chrX; see CLAUDE.md obs #3 |"
        " docs/findings/finding-020-canonical-refalt-backfill.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.COPIED_ANCHOR_NUMBER not in _codes(report)


def test_tolerance_banded_number_in_decision_is_clean(tmp_path: Path) -> None:
    """from: plan §3 (tolerance-banded anchors never frozen as scalars) + contract #4.

    A decision cell mentioning a tolerance-banded number (chrX yield 92,832) is NOT a
    copied-anchor violation — those numbers are deliberately excluded from the guarded set.
    """
    rows = (
        "| DEC-0001 | tactical | 2026-05-22 | active | — | VSC-User |"
        " finding-029 | chrX recovery yields ~92,832 kept calls (tolerance-banded) |"
        " docs/findings/finding-029-chrx-imputation-m1.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.COPIED_ANCHOR_NUMBER not in _codes(report)


# ---------------------------------------------------------------------------
# Contract #2 — LIFECYCLE: insert-then-flip vs in-place content edit (git baseline).
# ---------------------------------------------------------------------------


def test_inplace_content_edit_of_superseded_row_is_rejected(tmp_path: Path) -> None:
    """from: plan §5 Lifecycle (load-bearing) + §5 negative + contract #2.

    Code ``INPLACE_CONTENT_EDIT``.
    Commit a baseline ``MEMORY.md``; then in the work tree REWRITE the immutable
    ``decision`` content column of the already-superseded ``DEC-0001`` row (not its
    status/pointer). The gate compares against the git baseline and rejects the in-place
    content edit — the only sanctioned change is insert-then-flip.
    """
    edited_rows = (
        "| DEC-0001 | architectural | 2026-05-22 | superseded | DEC-0002 | VSC-User |"
        " finding-011 | EDITED IN PLACE — content column rewritten |"
        " docs/findings/finding-011-gnomad-three-way-intersection.md |\n"
        "| DEC-0002 | architectural | 2026-06-21 | active | — | VSC-User |"
        " finding-035 | gnomAD filter narrowed to user_only |"
        " docs/findings/finding-035-gnomad-filter-set-consumer-audit.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(_CLEAN_LEDGER_ROWS))
    _git_commit_all(tmp_path)
    # Mutate the immutable content column of the superseded row in the work tree.
    (tmp_path / "MEMORY.md").write_text(_ledger(edited_rows), encoding="utf-8")

    report = check(tmp_path)
    assert validator.INPLACE_CONTENT_EDIT in _codes(report)
    assert report.ok is False


def test_insert_then_flip_is_accepted_against_baseline(tmp_path: Path) -> None:
    """from: plan §5 Lifecycle (only accepted transition) + contract #2.

    Commit a baseline where ``DEC-0001`` is ``active``; then in the work tree FLIP it to
    ``superseded``/``DEC-0002`` and INSERT the new ``DEC-0002`` row (status+pointer only,
    content columns untouched). This is the sanctioned insert-then-flip → no
    ``INPLACE_CONTENT_EDIT``.
    """
    baseline_rows = (
        "| DEC-0001 | architectural | 2026-05-22 | active | — | VSC-User |"
        " finding-011 | gnomAD filter scoped three-way; see CLAUDE.md obs #4 |"
        " docs/findings/finding-011-gnomad-three-way-intersection.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(baseline_rows))
    _git_commit_all(tmp_path)
    # Flip DEC-0001 (status+pointer only) and insert DEC-0002 — content columns unchanged.
    (tmp_path / "MEMORY.md").write_text(_ledger(_CLEAN_LEDGER_ROWS), encoding="utf-8")

    report = check(tmp_path)
    assert validator.INPLACE_CONTENT_EDIT not in _codes(report)


def test_inplace_rule_skipped_gracefully_without_git_baseline(tmp_path: Path) -> None:
    """from: contract code ``INPLACE_CONTENT_EDIT`` (skipped gracefully w/ no committed baseline).

    With NO git repo at ``repo_root`` (no committed baseline to diff against), the in-place
    rule is silently satisfied — ``check`` runs and emits no ``INPLACE_CONTENT_EDIT``.
    """
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(_CLEAN_LEDGER_ROWS))
    # Deliberately do NOT init git here.
    report = check(tmp_path)
    assert validator.INPLACE_CONTENT_EDIT not in _codes(report)


# ---------------------------------------------------------------------------
# Stage-3 fix-first coverage — review-identified gaps (silent-failure blocker,
# convention date-immutability, test-adequacy untested codes).
# ---------------------------------------------------------------------------


def test_missing_index_markers_is_retrieval_violation(tmp_path: Path) -> None:
    """from: Stage-3 silent-failure review (blocker) — a README with no index markers must
    FAIL the gate, not silently pass. RETRIEVAL is not a no-op when the index surface is
    broken."""
    no_markers = "# Findings\n\nA findings README with no index marker block at all.\n"
    _write_repo(
        tmp_path,
        findings=_clean_findings(),
        ledger=_ledger(_CLEAN_LEDGER_ROWS),
        readme=no_markers,
    )
    report = check(tmp_path)
    assert validator.MISSING_INDEX_MARKER in _codes(report)
    assert report.ok is False


def test_stale_index_is_retrieval_violation_via_check(tmp_path: Path) -> None:
    """from: Stage-3 test-adequacy — STALE_INDEX surfaces through check() (markers present but
    an empty/stale body vs the findings), not only via build_index() directly."""
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(_CLEAN_LEDGER_ROWS))
    report = check(tmp_path)
    assert validator.STALE_INDEX in _codes(report)


def test_malformed_frontmatter_is_capture_violation_via_check(tmp_path: Path) -> None:
    """from: Stage-3 test-adequacy — a present-but-structurally-broken frontmatter block (a
    line with no colon) surfaces as MALFORMED_FRONTMATTER through check()."""
    findings = _clean_findings()
    findings["finding-070-malformed.md"] = (
        "---\nno_colon_here\n---\n# Finding 070 — malformed frontmatter\n\nBody.\n"
    )
    _write_repo(tmp_path, findings=findings, ledger=_ledger(_CLEAN_LEDGER_ROWS))
    report = check(tmp_path)
    assert validator.MALFORMED_FRONTMATTER in _codes(report)
    assert report.ok is False


def test_reversed_without_pointer_is_lifecycle_violation(tmp_path: Path) -> None:
    """from: Stage-3 test-adequacy — the 'reversed' terminal status (not only 'superseded')
    requires a superseded_by pointer."""
    rows = (
        "| DEC-0001 | architectural | 2026-05-22 | reversed | — | VSC-User |"
        " finding-011 | reversed but no pointer | docs/findings/finding-011.md |\n"
    )
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(rows))
    report = check(tmp_path)
    assert validator.SUPERSEDED_WITHOUT_POINTER in _codes(report)
    assert report.ok is False


def test_type_both_finding_without_dec_row_is_violation(tmp_path: Path) -> None:
    """from: Stage-3 test-adequacy — a type='both' finding (not only 'decision') with no DEC
    row is a DECISION_WITHOUT_DEC_ROW violation."""
    findings = _clean_findings()
    findings["finding-071-both.md"] = _finding(
        number="071",
        type_="both",
        status="active",
        title="observation plus a decision",
    )
    _write_repo(tmp_path, findings=findings, ledger=_ledger(_CLEAN_LEDGER_ROWS))
    report = check(tmp_path)
    locs = [v.location for v in report.violations if v.code == validator.DECISION_WITHOUT_DEC_ROW]
    assert any("finding-071" in loc for loc in locs), locs


def test_inplace_date_edit_is_rejected(tmp_path: Path) -> None:
    """from: Stage-3 convention review — the `date` content column is immutable (MEMORY.md
    legend + LedgerRow docstring); editing ONLY the date of an existing DEC row against the git
    baseline trips INPLACE_CONTENT_EDIT (date is in the enforced content set)."""
    _write_repo(tmp_path, findings=_clean_findings(), ledger=_ledger(_CLEAN_LEDGER_ROWS))
    _git_commit_all(tmp_path)
    edited = _CLEAN_LEDGER_ROWS.replace("2026-05-22", "2026-05-23")  # DEC-0001 date only
    (tmp_path / "MEMORY.md").write_text(_ledger(edited), encoding="utf-8")
    report = check(tmp_path)
    assert validator.INPLACE_CONTENT_EDIT in _codes(report)

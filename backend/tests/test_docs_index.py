"""Findings-index generation + retrieval idempotence — ``genome.docs.index``.

Plan-blind spec source: the approved ``decision-tracking-leak-fix`` plan §5 ("Retrieval
idempotence" — ``build-index`` is normalize-then-compare stable; a second run is a no-op)
+ §6 (RETRIEVAL dimension: regenerated index matches committed under normalize-then-compare),
and the frozen interface contract (``genome.docs.index`` surface, ``IndexResult``, behavioural
contract #8). Expected values come from that spec — never from the stubbed ``index.py`` body
(``raise NotImplementedError`` by design; RED is correct).

Fixtures build a minimal repo on disk (finding-013 realism: real on-disk frontmatter blocks
+ the README index marker block) and drive ``build_index`` against it; idempotence is asserted
behaviourally (``changed`` flips false on the second run; hand-authored prose outside the
markers is preserved) rather than against a byte-exact golden render we cannot compute without
the implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from genome.docs.index import build_index
from genome.docs.model import (
    INDEX_BEGIN_MARKER,
    INDEX_END_MARKER,
    IndexResult,
)

if TYPE_CHECKING:
    from pathlib import Path

_PREAMBLE = "Hand-authored preamble that build-index MUST preserve verbatim.\n"
_TRAILER = "Hand-authored trailer that build-index MUST preserve verbatim.\n"


def _finding(  # noqa: PLR0913 — a fixture builder; each kwarg is one frontmatter key
    *,
    number: str,
    type_: str = "decision",
    status: str = "active",
    supersedes: str = "[]",
    superseded_by: str = "[]",
    title: str = "a finding",
) -> str:
    return (
        "---\n"
        f"type: {type_}\n"
        f"status: {status}\n"
        "actors: [VSC-User]\n"
        "date: 2026-06-23\n"
        f"supersedes: {supersedes}\n"
        f"superseded_by: {superseded_by}\n"
        "---\n"
        f"# Finding {number} — {title}\n"
        "\n"
        "Body text.\n"
    )


def _readme(index_body: str = "") -> str:
    """A findings README with the generated marker block (initially empty between markers)."""
    return (
        "# Findings\n"
        "\n"
        f"{_PREAMBLE}"
        "\n"
        f"{INDEX_BEGIN_MARKER}\n"
        f"{index_body}"
        f"{INDEX_END_MARKER}\n"
        "\n"
        f"{_TRAILER}"
    )


def _build_repo(root: Path, *, readme: str | None = None) -> None:
    """A repo with a supersession pair (011 → 035) so cross-links can be derived."""
    (root / "CLAUDE.md").write_text("# Project\n\n## Real-data observations\n", encoding="utf-8")
    (root / "MEMORY.md").write_text("# MEMORY — decision ledger\n", encoding="utf-8")
    findings_dir = root / "docs" / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    findings_dir.joinpath("finding-011-gnomad-three-way-intersection.md").write_text(
        _finding(
            number="011",
            status="superseded",
            superseded_by="[finding-035]",
            title="gnomAD three-way intersection",
        ),
        encoding="utf-8",
    )
    findings_dir.joinpath("finding-035-gnomad-filter-set-consumer-audit.md").write_text(
        _finding(
            number="035",
            status="active",
            supersedes="[finding-011]",
            title="gnomAD filter-set consumer audit",
        ),
        encoding="utf-8",
    )
    findings_dir.joinpath("README.md").write_text(
        readme if readme is not None else _readme(),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Contract #8 — retrieval idempotence (normalize-then-compare, second run no-op).
# ---------------------------------------------------------------------------


def test_build_index_first_run_changes_then_writes(tmp_path: Path) -> None:
    """from: plan §5 retrieval idempotence (first run) + contract ``build_index``.

    Against a README with an EMPTY marker block, the first ``build_index(write=True)``
    reports ``changed=True``, indexes the two findings, and derives the one supersession
    cross-link (011 → 035).
    """
    _build_repo(tmp_path)
    result = build_index(tmp_path, write=True)
    assert isinstance(result, IndexResult)
    assert result.changed is True
    assert result.findings_indexed == 2
    assert result.cross_links_derived == 1


def test_build_index_second_run_is_noop(tmp_path: Path) -> None:
    """from: plan §5 retrieval idempotence (second run is a no-op) + §6 RETRIEVAL + contract #8.

    After the first write, a second ``build_index`` over the now-current README reports
    ``changed=False`` (normalize-then-compare stable: padding/trailing whitespace are
    normalised, so a re-render of the same findings is a no-op).
    """
    _build_repo(tmp_path)
    build_index(tmp_path, write=True)
    second = build_index(tmp_path, write=True)
    assert second.changed is False


def test_build_index_normalize_then_compare_ignores_padding(tmp_path: Path) -> None:
    """from: plan §5 (normalize-then-compare, NOT raw byte-identical) + contract #8.

    Idempotence dodges table-padding / trailing-whitespace fragility: take the rendered
    README, perturb it with trailing spaces on the marker-block lines, write it back, and
    ``build_index`` still reports ``changed=False`` — the comparison normalises first.
    """
    _build_repo(tmp_path)
    first = build_index(tmp_path, write=True)
    readme_path = tmp_path / "docs" / "findings" / "README.md"
    # Perturb purely cosmetically: append trailing spaces to non-empty lines.
    perturbed = "\n".join(
        (line + "   ") if line.strip() else line for line in first.rendered.splitlines()
    )
    readme_path.write_text(perturbed + "\n", encoding="utf-8")
    again = build_index(tmp_path, write=True)
    assert again.changed is False


def test_build_index_dry_run_does_not_touch_disk(tmp_path: Path) -> None:
    """from: contract ``build_index(write=False)`` (dry run reports ``changed`` w/o writing).

    With an empty marker block, ``build_index(write=False)`` reports ``changed=True`` but
    leaves the README on disk byte-unchanged (the CI drift-guard mode).
    """
    _build_repo(tmp_path)
    readme_path = tmp_path / "docs" / "findings" / "README.md"
    before = readme_path.read_text(encoding="utf-8")
    result = build_index(tmp_path, write=False)
    assert result.changed is True
    assert readme_path.read_text(encoding="utf-8") == before


def test_build_index_preserves_prose_outside_markers(tmp_path: Path) -> None:
    """from: plan Task 4 (regenerate only the marker block; preserve all prose outside).

    The hand-authored preamble and trailer outside the markers survive verbatim in the
    rendered README; only the span between the markers is rewritten.
    """
    _build_repo(tmp_path)
    result = build_index(tmp_path, write=True)
    assert _PREAMBLE in result.rendered
    assert _TRAILER in result.rendered
    assert INDEX_BEGIN_MARKER in result.rendered
    assert INDEX_END_MARKER in result.rendered

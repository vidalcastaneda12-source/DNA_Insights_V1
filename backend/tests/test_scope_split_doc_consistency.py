"""Cross-doc consistency — the B2-Phase1 doc + ledger deliverables exist and are well-formed.

Plan-blind spec source: SYNTHESIZED-PLAN §4 step 11 (the Stage-0.5 hook in
.claude/commands/scope-run.md: "Specific sentence for doc-consistency regex") + step 12
(.claude/commands/scope-split.md mirror; finding-039 with CAPTURE/RETRIEVAL/LIFECYCLE
frontmatter); §5 ("doc_consistency (Stage-0.5 specific sentence regex; B2-Phase1 slot;
scope-split.md sentinel-free; finding-039 frontmatter)"); IMPL-CONTRACT mech #10 ("finding-039
frontmatter copied verbatim from finding-038; ROADMAP <!-- B2-SUBSCOPES --> sentinels live ONLY
in ROADMAP, not in scope-split.md (avoid doc-consistency regex collision)").

STATUS: the ROADMAP B2-Phase1 slot + managed markers are bootstrapped FIRST (plan §4 step 1), so
those two assertions are GREEN from freeze. The Stage-0.5 sentence in scope-run.md,
scope-split.md, and finding-039 are created by the implementer concurrently — those assertions
are RED until the docs land (expected; noted). All assert the POSITIVE invariant, never a naive
grep.

test->spec provenance noted per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import re
from pathlib import Path


def _repo_root() -> Path:
    """Walk up from this file to the first directory holding ``CLAUDE.md`` (the repo root)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "CLAUDE.md").is_file():
            return parent
    msg = "could not locate repo root (no CLAUDE.md found walking up from the test file)"
    raise AssertionError(msg)


def _frontmatter_block(text: str) -> str:
    """Return the YAML frontmatter block (between the first two ``---`` fences)."""
    assert text.startswith("---"), "doc has no leading '---' frontmatter fence"
    end = text.find("\n---", 3)
    assert end != -1, "doc frontmatter is not closed by a '---' fence"
    return text[3:end]


# ── ROADMAP B2-Phase1 slot + managed markers (GREEN — bootstrapped first) ──────


def test_roadmap_has_b2_phase1_slot() -> None:
    """from: SYNTHESIZED-PLAN §4 step 1 (bootstrap the B2-Phase1 ROADMAP slot FIRST) + §5
    ("B2-Phase1 slot").

    ROADMAP.md carries the "## Sub Project B2 — scope-split (Phase 1)" section and the
    B2-Phase1 slot line.
    """
    text = (_repo_root() / "ROADMAP.md").read_text(encoding="utf-8")
    assert "Sub Project B2 — scope-split (Phase 1)" in text
    assert "B2-Phase1" in text


def test_roadmap_has_managed_subscope_markers() -> None:
    """from: SYNTHESIZED-PLAN §4 step 1 (empty managed delimiters in ROADMAP) + mech #10 (the
    sentinels live ONLY in ROADMAP).

    The managed <!-- B2-SUBSCOPES:BEGIN --> / <!-- B2-SUBSCOPES:END --> sentinels exist in
    ROADMAP.md so write-roadmap has a slot to manage.
    """
    text = (_repo_root() / "ROADMAP.md").read_text(encoding="utf-8")
    assert "<!-- B2-SUBSCOPES:BEGIN -->" in text
    assert "<!-- B2-SUBSCOPES:END -->" in text


# ── scope-run.md Stage-0.5 split-check sentence (RED until landed) ─────────────


def test_scope_run_has_stage_0_5_split_check_sentence() -> None:
    """from: SYNTHESIZED-PLAN §4 step 11 ("Specific sentence for doc-consistency regex") + §5
    ("Stage-0.5 specific sentence regex").

    scope-run.md gains a Stage-0.5 split-check step. The assertion keys on the SPECIFIC pairing
    of "scope-split" with "split check" within a small window (a regex, NOT a naive grep), so a
    pre-existing unrelated "scope-split" mention does not false-pass. RED until the doc lands.
    """
    text = (_repo_root() / ".claude" / "commands" / "scope-run.md").read_text(encoding="utf-8")
    lowered = text.lower()
    pattern = re.compile(
        r"scope-split[^.\n]{0,80}split[\s-]?check"
        r"|split[\s-]?check[^.\n]{0,80}scope-split",
        re.IGNORECASE,
    )
    assert pattern.search(lowered) is not None, (
        "scope-run.md is missing the specific Stage-0.5 split-check sentence pairing "
        "'scope-split' with 'split check'"
    )


# ── scope-split.md skill doc (RED until landed) ───────────────────────────────


def test_scope_split_skill_doc_exists_and_is_sentinel_free() -> None:
    """from: SYNTHESIZED-PLAN §4 step 12 (.claude/commands/scope-split.md mirror fast-follow.md)
    + §5 ("scope-split.md sentinel-free") + mech #10 (no <!-- B2-SUBSCOPES --> sentinel here).

    The /scope-split skill doc exists and carries no GATE-FILL / TODO survivor (born durable).
    RED until the doc lands.
    """
    skill = _repo_root() / ".claude" / "commands" / "scope-split.md"
    assert skill.is_file(), f"scope-split skill doc not created yet: {skill}"
    body = skill.read_text(encoding="utf-8")
    assert "GATE-FILL" not in body, "GATE-FILL survivor in scope-split.md"
    assert "TODO" not in body, "TODO survivor in scope-split.md"


# ── finding-039 frontmatter (RED until landed) ────────────────────────────────


def test_finding_039_exists_and_frontmatter_parses() -> None:
    """from: SYNTHESIZED-PLAN §4 step 12 (finding-039 with CAPTURE/RETRIEVAL/LIFECYCLE
    frontmatter) + §5 ("finding-039 frontmatter") + mech #10 (frontmatter copied verbatim from
    finding-038).

    docs/findings/finding-039-*.md exists; its YAML frontmatter carries type / status / actors /
    date, and the body carries the CAPTURE / RETRIEVAL / LIFECYCLE sections (the genome docs
    check gate's required structure). RED until the finding lands.
    """
    findings = sorted((_repo_root() / "docs" / "findings").glob("finding-039-*.md"))
    assert findings, "finding-039-*.md not created yet"
    text = findings[0].read_text(encoding="utf-8")
    fm = _frontmatter_block(text)
    for key in ("type:", "status:", "actors:", "date:"):
        assert key in fm, f"finding-039 frontmatter missing {key!r}"
    for section in ("CAPTURE", "RETRIEVAL", "LIFECYCLE"):
        assert section in text, f"finding-039 body missing {section} section"

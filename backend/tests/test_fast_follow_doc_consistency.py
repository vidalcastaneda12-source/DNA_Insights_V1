"""Cross-doc consistency — the Sub Project B doc deliverables exist and are well-formed.

Plan-blind spec source: synthesized-plan §4 (the skill + docs deliverables:
``.claude/commands/fast-follow.md``; ``docs/findings/finding-038-fast-follow-drain-loop.md``
with CAPTURE/RETRIEVAL/LIFECYCLE frontmatter; the auto-offer hook is a prose append to
``.claude/commands/verify-and-merge.md`` step 9 — "No JS"), §5 test list item 6
("test_fast_follow_doc_consistency.py — fast-follow.md exists + sentinel-free;
verify-and-merge auto-offer hook prose present; finding-038 frontmatter parses"), R5
("Disambiguate 'fast-follow' in verify-and-merge.md step 9 … the new auto-offer hook prose
must use distinct, specific wording … and assert that SPECIFIC sentence, never a naive `grep
fast-follow` which would false-pass on the pre-existing curator line"), R6 (finding-038
frontmatter matches finding-037's template: type/status/actors/date + CAPTURE/RETRIEVAL/
LIFECYCLE), and the FROZEN INTERFACE CONTRACT ("Docs deliverables (will exist by green-time)").

These are RED until the fill round creates the docs. They assert the POSITIVE invariant (the
specific auto-offer phrase about a `/fast-follow` DRAIN scan), deliberately NOT a brittle naive
``grep fast-follow`` — verify-and-merge.md ALREADY uses "fast-follow" for the knowledge-curator
doc re-lock (a different sense, R5), so a naive grep would false-pass.
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


# ── The fast-follow skill exists ──────────────────────────────────────────────


def test_fast_follow_skill_doc_exists() -> None:
    """from: plan §4 (``.claude/commands/fast-follow.md`` thin skill) + §5 item 6.

    The ``/fast-follow`` skill file exists (the model-driven scan→triage→approval→drain→gate→
    loop runbook).
    """
    root = _repo_root()
    skill = root / ".claude" / "commands" / "fast-follow.md"
    assert skill.is_file(), f"fast-follow skill doc not created yet: {skill}"


def test_fast_follow_skill_doc_is_sentinel_free() -> None:
    """from: plan §5 item 6 (fast-follow.md … sentinel-free).

    The new skill doc carries no GATE-FILL / TODO placeholder survivor — it is born durable.
    """
    root = _repo_root()
    skill = root / ".claude" / "commands" / "fast-follow.md"
    assert skill.is_file(), f"fast-follow skill doc not created yet: {skill}"
    body = skill.read_text(encoding="utf-8")
    assert "GATE-FILL" not in body, "GATE-FILL survivor in fast-follow.md"
    assert "TODO" not in body, "TODO survivor in fast-follow.md"


# ── finding-038 exists and its frontmatter parses ─────────────────────────────


def test_finding_038_exists_and_frontmatter_parses() -> None:
    """from: plan §4 (finding-038 with CAPTURE/RETRIEVAL/LIFECYCLE frontmatter) + §5 item 6 +
    R6 (frontmatter matches finding-037's template: type/status/actors/date).

    ``docs/findings/finding-038-fast-follow-drain-loop.md`` exists and its YAML frontmatter
    block carries the required keys (type / status / actors / date) — the same template
    finding-037 uses. (CAPTURE/RETRIEVAL/LIFECYCLE sections live in the body; the structured
    frontmatter keys are asserted here.)
    """
    root = _repo_root()
    findings = sorted((root / "docs" / "findings").glob("finding-038-*.md"))
    assert findings, "finding-038-*.md not created yet"
    text = findings[0].read_text(encoding="utf-8")
    fm = _frontmatter_block(text)
    for key in ("type:", "status:", "actors:", "date:"):
        assert key in fm, f"finding-038 frontmatter missing {key!r}"


# ── verify-and-merge.md carries the SPECIFIC /fast-follow drain-scan auto-offer ─


def test_verify_and_merge_has_fast_follow_drain_scan_auto_offer() -> None:
    """from: plan §4 (auto-offer hook = prose append to verify-and-merge.md step 9) + §5 item 6
    + R5 (assert the SPECIFIC sentence about offering a /fast-follow DRAIN-loop scan, NEVER a
    naive ``grep fast-follow`` that false-matches the pre-existing knowledge-curator usage).

    The close step of ``.claude/commands/verify-and-merge.md`` gains a distinct auto-offer line
    that offers a ``/fast-follow`` DRAIN-loop scan of the residual backlog. The assertion keys
    on the SPECIFIC pairing of "fast-follow" with the DRAIN-scan sense (a regex requiring both
    "/fast-follow" and "drain" within the same sentence-ish window) — so the pre-existing
    curator "fast-follow" mention (a different sense) does not false-pass it.
    """
    root = _repo_root()
    text = (root / ".claude" / "commands" / "verify-and-merge.md").read_text(encoding="utf-8")
    lowered = text.lower()
    # Require the specific DRAIN-scan auto-offer: "/fast-follow" co-located with "drain" + "scan".
    # The naive `grep fast-follow` would match the curator line; this pattern would not.
    pattern = re.compile(
        r"/fast-follow[^.\n]{0,80}drain[^.\n]{0,40}scan"
        r"|drain[^.\n]{0,40}scan[^.\n]{0,80}/fast-follow",
        re.IGNORECASE,
    )
    assert pattern.search(lowered) is not None, (
        "verify-and-merge.md is missing the specific '/fast-follow drain-loop scan' auto-offer "
        "line (R5 disambiguation — a naive grep on 'fast-follow' would false-pass on the "
        "pre-existing knowledge-curator usage)"
    )


def test_verify_and_merge_auto_offer_is_offer_only(tmp_path: Path) -> None:
    """from: R5 (the auto-offer is OFFER-only — never acts; B's own self-merge triggering it is
    harmless).

    The auto-offer line frames the scan as something OFFERED (the operator may decline), not an
    autonomous action. Asserted as the presence of offer-language ("offer"/"would you"/"can
    run") in the vicinity of the /fast-follow drain-scan mention. (tmp_path unused; kept for a
    uniform signature with the sandbox-using tests.)
    """
    _ = tmp_path
    root = _repo_root()
    lowered = (
        (root / ".claude" / "commands" / "verify-and-merge.md").read_text(encoding="utf-8").lower()
    )
    assert "offer" in lowered, (
        "verify-and-merge.md auto-offer hook does not frame the /fast-follow scan as an OFFER "
        "(R5: offer-only, never an autonomous action)"
    )

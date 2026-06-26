"""Cross-doc consistency — the finding-040 / write-hook / precedent-read deliverables are wired in.

from: §5 test #17 (test_calibration_doc_consistency.py) + §6:
  * ``.claude/commands/verify-and-merge.md`` Step 9 cites ``genome calibrate write-outcome``;
  * the dispatcher step-7 precedent read cites ``data/calibration/outcomes.jsonl`` with an explicit
    absent -> fall-back-to-grep note;
  * ``docs/findings/finding-040-*.md`` exists with valid frontmatter (type/status/actors/date +
    CAPTURE/RETRIEVAL/LIFECYCLE).

A fourth check (``sub-project-C1-cross-run-learning.md`` status flipped) was retired when that
plan was pruned in the 2026-06-26 repo sweep — finding-040 is the durable record. The three
remaining were RED until the implementer's doc work (T10/T11) landed; each asserts the POSITIVE
invariant (a specific pairing / a real frontmatter), never a naive grep. test->spec provenance is
stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import re
from pathlib import Path


def _repo_root() -> Path:
    """Walk up from this file to the first directory holding ``CLAUDE.md`` (the repo root)."""
    for parent in Path(__file__).resolve().parents:
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


def test_verify_and_merge_step_9_cites_write_outcome() -> None:
    """from: §6 (verify-and-merge.md Step 9 cites ``genome calibrate write-outcome``).

    A's close hook (Step 9, "Close") runs the outcome write-hook. The assertion scopes to the
    Step-9 region (between "9. **Close.**" and the next heading) so a stray mention elsewhere does
    not false-pass. RED until the doc edit lands.
    """
    path = _repo_root() / ".claude" / "commands" / "verify-and-merge.md"
    text = path.read_text(encoding="utf-8")
    start = text.find("9. **Close.**")
    assert start != -1, "verify-and-merge.md has no Step 9 'Close' marker"
    rest = text[start:]
    end = rest.find("\n## ")
    step9 = rest if end == -1 else rest[:end]
    assert "genome calibrate write-outcome" in step9


def test_dispatcher_precedent_read_cites_outcomes_ledger_with_grep_fallback() -> None:
    """from: §6 (dispatcher step-7 precedent read cites outcomes.jsonl + absent -> grep fallback).

    The precedent read now consults the systematic outcome ledger, with an explicit fall-back to
    the finding/git grep when the ledger is absent (first run). The assertion pairs the ledger
    path with an absent/fallback/grep token within a window. RED until the doc edit lands.
    """
    path = _repo_root() / ".claude" / "agents" / "scope-dispatcher.md"
    text = path.read_text(encoding="utf-8")
    assert "data/calibration/outcomes.jsonl" in text
    pattern = re.compile(
        r"outcomes\.jsonl[\s\S]{0,300}(absent|fall[\s-]?back|grep|first run)"
        r"|(absent|fall[\s-]?back|grep)[\s\S]{0,300}outcomes\.jsonl",
        re.IGNORECASE,
    )
    assert pattern.search(text) is not None, "no absent -> grep fallback note beside outcomes.jsonl"


def test_finding_040_exists_and_frontmatter_parses() -> None:
    """from: §6 (docs/findings/finding-040-*.md exists with valid frontmatter).

    The cross-run-learning finding exists; its YAML frontmatter carries type / status / actors /
    date, and the body carries the CAPTURE / RETRIEVAL / LIFECYCLE sections the docs-check gate
    requires. RED until the finding lands.
    """
    findings = sorted((_repo_root() / "docs" / "findings").glob("finding-040-*.md"))
    assert findings, "finding-040-*.md not created yet"
    text = findings[0].read_text(encoding="utf-8")
    fm = _frontmatter_block(text)
    for key in ("type:", "status:", "actors:", "date:"):
        assert key in fm, f"finding-040 frontmatter missing {key!r}"
    for section in ("CAPTURE", "RETRIEVAL", "LIFECYCLE"):
        assert section in text, f"finding-040 body missing {section} section"

"""roadmap_writer units — PURE-STRING append/clobber suite (no filesystem).

Plan-blind spec source: FROZEN-INTERFACE roadmap_writer.py (BLOCK_BEGIN/BLOCK_END/
DEFAULT_ROADMAP_PATH "real"; append_roadmap_block "STUBBED"; Contract: "requires parent slot +
markers present → else raise (ValueError); idempotent (same origin_scope → byte-unchanged,
newline-normalized); replace ONLY content between markers; reversible; bytes OUTSIDE markers
byte-identical"); IMPL-CONTRACT mech #9 ("normalizes the inter-sentinel region … byte-idempotent
regardless of parent trailing newline; test BOTH trailing-newline and no-trailing-newline parent
fixtures"); SYNTHESIZED-PLAN §5 ("roadmap_writer (PURE-STRING clobber suite: appends under
parent; idempotent byte-unchanged; raises absent parent/markers; reversible; …)").

BLOCK_BEGIN / BLOCK_END / DEFAULT_ROADMAP_PATH assertions are GREEN from freeze. The transform
tests are RED-until-filled: they assert the BEHAVIOR (the contract above) so they go RED on
NotImplementedError now and GREEN when append_roadmap_block lands — never
pytest.raises(NotImplementedError).

test->spec provenance noted per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from genome.scope_split.roadmap_writer import (
    BLOCK_BEGIN,
    BLOCK_END,
    DEFAULT_ROADMAP_PATH,
    append_roadmap_block,
)

_HEADER = "## Sub Project B2 — scope-split (Phase 1)\n"
_PROSE = "Some prose describing the slot.\n\n"
_TAIL = "\n## Next section (must stay byte-identical)\n"

_BLOCK = "- sub-scope PR-7-s1 (schema)\n- sub-scope PR-7-s2 (cli)\n"


def _parent_with_markers(*, trailing_newline: bool) -> str:
    """A ROADMAP-shaped parent that already carries the empty managed markers under the slot."""
    body = _HEADER + _PROSE + f"{BLOCK_BEGIN}\n{BLOCK_END}" + _TAIL
    return body + "\n" if trailing_newline else body


# ── real module constants (GREEN from freeze) ─────────────────────────────────


def test_marker_constants_are_the_frozen_sentinels() -> None:
    """from: FROZEN-INTERFACE roadmap_writer ("BLOCK_BEGIN = '<!-- B2-SUBSCOPES:BEGIN -->';
    BLOCK_END = '<!-- B2-SUBSCOPES:END -->'"). GREEN from freeze.
    """
    assert BLOCK_BEGIN == "<!-- B2-SUBSCOPES:BEGIN -->"
    assert BLOCK_END == "<!-- B2-SUBSCOPES:END -->"


def test_default_roadmap_path_is_roadmap_md() -> None:
    """from: FROZEN-INTERFACE ("DEFAULT_ROADMAP_PATH = Path('ROADMAP.md')"). GREEN from freeze."""
    assert Path("ROADMAP.md") == DEFAULT_ROADMAP_PATH


# ── append between markers (RED until filled) ─────────────────────────────────


def test_append_inserts_block_between_markers() -> None:
    """from: roadmap_writer contract ("replace ONLY content between markers") + SYNTHESIZED-PLAN
    §5 ("appends under parent slot").

    The rendered block lands between BLOCK_BEGIN and BLOCK_END. (RED-until-filled.)
    """
    parent = _parent_with_markers(trailing_newline=True)
    result = append_roadmap_block(parent, _BLOCK, origin_scope="PR-7")
    begin = result.index(BLOCK_BEGIN)
    end = result.index(BLOCK_END)
    between = result[begin + len(BLOCK_BEGIN) : end]
    assert "PR-7-s1" in between
    assert "PR-7-s2" in between


def test_append_leaves_bytes_outside_markers_identical() -> None:
    """from: roadmap_writer contract ("bytes OUTSIDE markers byte-identical").

    Everything before BLOCK_BEGIN and after BLOCK_END is byte-identical to the parent — the
    write touches only the managed region. (RED-until-filled.)
    """
    parent = _parent_with_markers(trailing_newline=True)
    result = append_roadmap_block(parent, _BLOCK, origin_scope="PR-7")

    p_begin = parent.index(BLOCK_BEGIN)
    r_begin = result.index(BLOCK_BEGIN)
    assert result[:r_begin] == parent[:p_begin]

    p_after = parent.index(BLOCK_END) + len(BLOCK_END)
    r_after = result.index(BLOCK_END) + len(BLOCK_END)
    assert result[r_after:] == parent[p_after:]


def test_append_is_byte_idempotent_on_same_origin_scope() -> None:
    """from: roadmap_writer contract ("idempotent (same origin_scope → byte-unchanged,
    newline-normalized)") + SYNTHESIZED-PLAN §6 ("write-roadmap twice byte-identical 2nd").

    Re-appending the same block for the same origin_scope is byte-unchanged on the second pass.
    (RED-until-filled.)
    """
    parent = _parent_with_markers(trailing_newline=True)
    once = append_roadmap_block(parent, _BLOCK, origin_scope="PR-7")
    twice = append_roadmap_block(once, _BLOCK, origin_scope="PR-7")
    assert twice == once


def test_append_idempotent_regardless_of_parent_trailing_newline() -> None:
    """from: mech #9 ("byte-idempotent regardless of parent trailing newline; test BOTH
    trailing-newline and no-trailing-newline parent fixtures").

    Both a trailing-newline parent and a no-trailing-newline parent normalize to the same
    managed region, and re-application is byte-idempotent for each. (RED-until-filled.)
    """
    for trailing in (True, False):
        parent = _parent_with_markers(trailing_newline=trailing)
        once = append_roadmap_block(parent, _BLOCK, origin_scope="PR-7")
        twice = append_roadmap_block(once, _BLOCK, origin_scope="PR-7")
        assert twice == once, f"not idempotent with trailing_newline={trailing}"


def test_append_raises_when_markers_absent() -> None:
    """from: roadmap_writer contract ("requires parent slot + BLOCK_BEGIN/BLOCK_END markers
    present in roadmap_text → else raise (ValueError)") + SYNTHESIZED-PLAN §5 ("raises absent
    parent/markers").

    A parent with no managed markers fails closed with ValueError rather than appending
    blindly (failure-ordering (b): ROADMAP clobbering must be impossible). (RED-until-filled.)
    """
    parent = _HEADER + _PROSE + _TAIL  # no markers
    with pytest.raises(ValueError):  # noqa: PT011 — contract is "raise (ValueError)"
        append_roadmap_block(parent, _BLOCK, origin_scope="PR-7")


def test_append_is_reversible_to_empty_block() -> None:
    """from: roadmap_writer contract ("reversible") + SYNTHESIZED-PLAN §5 ("reversible").

    The managed region between the markers can be returned to its empty state — appending an
    empty block restores the bytes outside the markers and leaves the managed region empty (no
    residue). (RED-until-filled.)
    """
    parent = _parent_with_markers(trailing_newline=True)
    filled = append_roadmap_block(parent, _BLOCK, origin_scope="PR-7")
    emptied = append_roadmap_block(filled, "", origin_scope="PR-7")

    begin = emptied.index(BLOCK_BEGIN)
    end = emptied.index(BLOCK_END)
    between = emptied[begin + len(BLOCK_BEGIN) : end]
    assert between.strip() == ""
    # bytes outside the markers survive the round-trip
    assert emptied[:begin] == parent[: parent.index(BLOCK_BEGIN)]

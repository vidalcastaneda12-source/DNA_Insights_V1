"""Frontmatter parser tolerance + round-trip — ``genome.docs.frontmatter`` / ``model``.

Plan-blind spec source: the approved ``decision-tracking-leak-fix`` plan §5 ("Parser
tolerance") + §6, and the frozen interface contract (``genome.docs`` public surface,
behavioural contracts #1, #6, #7). Every expected value here is derived from that spec —
never from the stubbed implementation bodies (``frontmatter.py`` / ``model.py`` currently
``raise NotImplementedError`` by design; these tests are RED until the implementer fills
them, which is the point: a test authored from the *spec* is an independent oracle).

Fixtures are built from the **real** finding ``Status`` shapes catalogued in the plan
§1 / the test-author brief (finding-013 realism), used verbatim as templates — the five
live shapes, the no-``Status`` case (finding-001/020), a ``|---|``-separator-heavy body
(finding-020/034), and the finding-024 H1-contains-"status" false-positive.

House style mirrors ``test_config.py`` (plain pytest functions, ``from __future__`` import,
type-annotated) — no DB fixture is needed: this module touches no database.
"""

from __future__ import annotations

import pytest

from genome.docs.frontmatter import (
    FrontmatterError,
    parse_frontmatter,
    render_frontmatter,
    split_frontmatter,
)
from genome.docs.model import (
    CANONICAL_ACTORS,
    FINDING_TYPE_VOCAB,
    KIND_VOCAB,
    LEGACY_ACTOR_MAP,
    STATUS_VOCAB,
    Frontmatter,
    canonicalize_actor,
)

# ---------------------------------------------------------------------------
# Real finding ``Status`` shapes (finding-013: verbatim, not invented).
# Each is a finding BODY that carries NO leading ``---`` fence — so the parser
# must report a CAPTURE-miss (``split_frontmatter`` → (None, body);
# ``parse_frontmatter`` → None) and must NOT mistake any in-body construct
# (``## Status``, ``|---|---|``, a deeper ``---`` thematic break) for a fence.
# ---------------------------------------------------------------------------

# finding-008 — blockquote-bold Status line.
_BODY_BLOCKQUOTE_BOLD = (
    "# Finding 008 — chrX prepare drops hom-only positions\n"
    "\n"
    "> **Status: closed by PR #74** (M3-physical region split).\n"
    "\n"
    "Context paragraph describing the observation.\n"
)

# finding-011 / finding-035 — bold-prose Status line (no blockquote).
_BODY_BOLD_PROSE = (
    "# Finding 011 — gnomAD three-way intersection\n"
    "\n"
    "**Status: superseded by [finding-035](finding-035-gnomad-filter-set-consumer-audit.md)"
    " — user_only narrowing.**\n"
    "\n"
    "The three-way filter is retained as the documented revert baseline.\n"
)

# finding-016 — blockquote-bold *note* with a parenthesised date.
_BODY_BLOCKQUOTE_NOTE = (
    "# Finding 016 — loader version-label decoupling\n"
    "\n"
    "> **Status note (2026-06-21):** the loader label and the on-disk data can diverge.\n"
    "\n"
    "Details follow.\n"
)

# finding-030..034 — a ``## Status`` heading SECTION deeper in the body.
_BODY_HEADING_SECTION = (
    "# Finding 032 — chrX LOO concordance\n"
    "\n"
    "Some context first.\n"
    "\n"
    "## Status\n"
    "\n"
    "Active; the LOO concordance is ~0.9856.\n"
)

# finding-005 — inline italic ``*Status:*``.
_BODY_INLINE = (
    "# Finding 005 — imputation prepare filters\n"
    "\n"
    "*Status:* active.\n"
    "\n"
    "Hom-only positions are filtered at prepare.\n"
)

# finding-001 / finding-020 — NO Status section at all.
_BODY_NO_STATUS = (
    "# Finding 001 — initial schema sketch\n"
    "\n"
    "A plain finding with no Status section anywhere in the body.\n"
)

# finding-020 / finding-034 — pipe-table-heavy body with many ``|---|---|`` rows.
# The separator rows and free-text ``|``/backtick/``:`` cells are the parser hazard:
# none of them may be mistaken for the frontmatter fence.
_BODY_PIPE_TABLE_HEAVY = (
    "# Finding 020 — canonical REF/ALT backfill\n"
    "\n"
    "Bedrock anchor table:\n"
    "\n"
    "| metric | before | after |\n"
    "|---|---|---|\n"
    "| gnomad_matches | 101,501 | 2,796,952 |\n"
    "| clinvar_matches | 2,559 | 61,458 |\n"
    "\n"
    "| stage | command | note |\n"
    "|---|---|---|\n"
    "| 1 | `canonicalize-variants` | re-orients swap victims |\n"
    "| 2 | `merge` | re-runs consensus |\n"
)

# finding-024 — H1 CONTAINS the word "status" but there is NO Status field.
# (The finding-024 false-positive: a title-substring match must not be read as a
# Status line, and the absence of a leading fence still means CAPTURE-miss.)
_BODY_H1_CONTAINS_STATUS = (
    "# Finding 024 — `genome status` reports stale active-version labels\n"
    "\n"
    "The `genome status` command's output is what this finding is about.\n"
)

_NO_FENCE_BODIES: dict[str, str] = {
    "blockquote_bold": _BODY_BLOCKQUOTE_BOLD,
    "bold_prose": _BODY_BOLD_PROSE,
    "blockquote_note": _BODY_BLOCKQUOTE_NOTE,
    "heading_section": _BODY_HEADING_SECTION,
    "inline": _BODY_INLINE,
    "no_status": _BODY_NO_STATUS,
    "pipe_table_heavy": _BODY_PIPE_TABLE_HEAVY,
    "h1_contains_status": _BODY_H1_CONTAINS_STATUS,
}

# A valid prepended frontmatter block (the on-disk shape from the contract example),
# placed atop a finding H1. Uses a real legacy actor spelling to exercise the legacy map.
_VALID_FRONTMATTER_DOC = (
    "---\n"
    "type: decision\n"
    "status: superseded\n"
    "kind: architectural\n"
    "date: 2026-05-22\n"
    "actors: [VSC-User, VSC-Claude]\n"
    "supersedes: []\n"
    "superseded_by: [finding-035]\n"
    "---\n"
    "# Finding 011 — gnomAD three-way intersection\n"
    "\n"
    "Body text below the fence.\n"
)


# ---------------------------------------------------------------------------
# Contract #1 — parser tolerance: every real Status shape is a CAPTURE-miss
# (no leading fence) and NONE mis-fences off the body.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(_NO_FENCE_BODIES))
def test_split_frontmatter_no_leading_fence_returns_none_and_full_body(shape: str) -> None:
    """from: plan §5 parser tolerance / contract #1.

    A finding body with no leading ``---`` (any of the 5 real Status shapes, the
    no-Status case, the pipe-table-heavy body, the H1-contains-"status" case) →
    ``split_frontmatter`` returns ``(None, <full body unchanged>)``. The body is
    NEVER re-scanned for a fence.
    """
    body = _NO_FENCE_BODIES[shape]
    block, returned_body = split_frontmatter(body)
    assert block is None
    assert returned_body == body


@pytest.mark.parametrize("shape", sorted(_NO_FENCE_BODIES))
def test_parse_frontmatter_no_leading_fence_returns_none(shape: str) -> None:
    """from: plan §5 parser tolerance / contract #1 (CAPTURE-miss = ``None``).

    ``parse_frontmatter`` returns ``None`` (not an error) when there is no leading
    fence — this is the signal the validator turns into ``MISSING_FRONTMATTER``.
    """
    assert parse_frontmatter(_NO_FENCE_BODIES[shape]) is None


def test_pipe_table_separator_not_mistaken_for_fence() -> None:
    """from: plan §5 ``|---|``-separator-heavy body / contract #1.

    The 12 pipe-table findings have many ``|---|---|`` separator rows. None may be
    read as a frontmatter fence: a body that is all tables still parses to
    ``(None, body)`` and the body round-trips byte-identically.
    """
    block, body = split_frontmatter(_BODY_PIPE_TABLE_HEAVY)
    assert block is None
    assert "|---|---|" in body  # the hazard survived untouched in the returned body
    assert body == _BODY_PIPE_TABLE_HEAVY


def test_deeper_thematic_break_not_mistaken_for_fence() -> None:
    """from: plan §5 / contract #1 — a ``---`` thematic break BELOW line 1 is body.

    Only the very first line may open a fence. A ``---`` rule deeper in the body must
    not start (or close) a frontmatter block.
    """
    body = (
        "# Finding 099 — a finding with a thematic break\n"
        "\n"
        "Intro paragraph.\n"
        "\n"
        "---\n"
        "\n"
        "A section after a horizontal rule.\n"
    )
    block, returned_body = split_frontmatter(body)
    assert block is None
    assert returned_body == body


def test_h1_contains_status_word_is_still_capture_miss() -> None:
    """from: plan §5 / contract #1 — the finding-024 false positive.

    An H1 containing the literal word "status" (``# Finding 024 — `genome status`
    reports ...``) is NOT a Status field and carries no leading fence, so it is a
    plain CAPTURE-miss — ``parse_frontmatter`` → ``None``.
    """
    assert parse_frontmatter(_BODY_H1_CONTAINS_STATUS) is None


# ---------------------------------------------------------------------------
# Frontmatter happy path + round-trip (contract: render_frontmatter ↔ parse).
# ---------------------------------------------------------------------------


def test_parse_valid_frontmatter_block_fields() -> None:
    """from: plan Task 2/3 + contract frontmatter on-disk shape.

    A well-formed leading block parses into a ``Frontmatter`` with the declared
    closed-vocab values; ``supersedes`` defaults to an empty tuple; ``superseded_by``
    carries the finding-id; and a legacy actor (``VSC-Claude``) is canonicalised
    through ``LEGACY_ACTOR_MAP`` (→ ``ClaudeCodeDevelopment``).
    """
    fm = parse_frontmatter(_VALID_FRONTMATTER_DOC)
    assert isinstance(fm, Frontmatter)
    assert fm.type == "decision"
    assert fm.status == "superseded"
    assert fm.date == "2026-05-22"
    assert fm.supersedes == ()
    assert fm.superseded_by == ("finding-035",)
    # VSC-User is canonical; VSC-Claude maps to ClaudeCodeDevelopment.
    assert fm.actors == ("VSC-User", "ClaudeCodeDevelopment")


def test_render_frontmatter_round_trips_with_parse() -> None:
    """from: plan Task 2 + contract (``render_frontmatter`` round-trips with parse).

    Rendering a ``Frontmatter`` then re-parsing yields an equal ``Frontmatter``; the
    render ends with a trailing newline and is safe to prepend above a ``# Finding`` H1
    (parsing ``<render> + "# Finding ...\\n"`` recovers the same block).
    """
    fm = Frontmatter(
        type="decision",
        status="active",
        actors=("VSC-User", "ClaudeCodeDevelopment"),
        date="2026-06-23",
        supersedes=(),
        superseded_by=(),
    )
    rendered = render_frontmatter(fm)
    assert rendered.endswith("\n")
    # Round-trips on its own.
    assert parse_frontmatter(rendered) == fm
    # Safe to prepend above an H1.
    assert parse_frontmatter(rendered + "# Finding 036 — decision ledger\n") == fm


# ---------------------------------------------------------------------------
# Frontmatter negative — a present-but-malformed block RAISES FrontmatterError.
# ---------------------------------------------------------------------------


def test_unknown_key_raises_frontmatter_error() -> None:
    """from: plan §5 / contract (unknown key → ``FrontmatterError``)."""
    doc = (
        "---\n"
        "type: decision\n"
        "status: active\n"
        "actors: [VSC-User]\n"
        "date: 2026-06-23\n"
        "bogus_key: nope\n"
        "---\n"
        "# Finding 100\n"
    )
    with pytest.raises(FrontmatterError):
        parse_frontmatter(doc)


def test_status_vocab_violation_raises_frontmatter_error() -> None:
    """from: plan §5 integrity (closed-vocab status) — frontmatter leg.

    A ``status`` value outside ``STATUS_VOCAB`` in a present block is a hard parse error.
    """
    doc = (
        "---\n"
        "type: decision\n"
        "status: tactical\n"  # 'tactical' is a KIND, never a STATUS (plan §0 res #2)
        "actors: [VSC-User]\n"
        "date: 2026-06-23\n"
        "---\n"
        "# Finding 101\n"
    )
    with pytest.raises(FrontmatterError):
        parse_frontmatter(doc)


def test_type_vocab_violation_raises_frontmatter_error() -> None:
    """from: plan §5 integrity (closed-vocab type) — frontmatter leg."""
    doc = (
        "---\n"
        "type: opinion\n"  # not in FINDING_TYPE_VOCAB
        "status: active\n"
        "actors: [VSC-User]\n"
        "date: 2026-06-23\n"
        "---\n"
        "# Finding 102\n"
    )
    with pytest.raises(FrontmatterError):
        parse_frontmatter(doc)


def test_unmapped_actor_raises_frontmatter_error() -> None:
    """from: plan §5 actor legacy map (unmapped novel name fails) — frontmatter leg."""
    doc = (
        "---\n"
        "type: decision\n"
        "status: active\n"
        "actors: [SomeRandomBot]\n"  # neither canonical nor in the legacy map
        "date: 2026-06-23\n"
        "---\n"
        "# Finding 103\n"
    )
    with pytest.raises(FrontmatterError):
        parse_frontmatter(doc)


def test_missing_required_key_raises_frontmatter_error() -> None:
    """from: plan §5 / contract (missing required key → ``FrontmatterError``).

    Required keys are ``type``, ``status``, ``actors``, ``date``. Omitting ``date``
    is a hard parse error (the block is present, so this is malformed, not a miss).
    """
    doc = "---\ntype: decision\nstatus: active\nactors: [VSC-User]\n---\n# Finding 104\n"
    with pytest.raises(FrontmatterError):
        parse_frontmatter(doc)


def test_bad_date_raises_frontmatter_error() -> None:
    """from: plan §5 / contract (bad date → ``FrontmatterError``)."""
    doc = (
        "---\n"
        "type: decision\n"
        "status: active\n"
        "actors: [VSC-User]\n"
        "date: 22-05-2026\n"  # not ISO-8601 YYYY-MM-DD
        "---\n"
        "# Finding 105\n"
    )
    with pytest.raises(FrontmatterError):
        parse_frontmatter(doc)


# ---------------------------------------------------------------------------
# Contract #6 — actor legacy map (``canonicalize_actor``) + closed-vocab constants.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    sorted(LEGACY_ACTOR_MAP.items()),
)
def test_canonicalize_actor_maps_real_legacy_names(legacy: str, canonical: str) -> None:
    """from: plan §5 actor legacy map / contract #6.

    Every real legacy spelling that lives in tracked history (``VSC-Claude``,
    ``VSC-ClaudeCodeDevelopment``, ``VSC-ClaudeCode``, ``VSC-ClaudeCodePlanning``,
    ``AI-Claude``) canonicalises to its mapped name.
    """
    assert canonicalize_actor(legacy) == canonical


@pytest.mark.parametrize("actor", sorted(CANONICAL_ACTORS))
def test_canonicalize_actor_passthrough_for_canonical(actor: str) -> None:
    """from: plan §5 / contract #6 — a canonical name is returned unchanged."""
    assert canonicalize_actor(actor) == actor


def test_canonicalize_actor_unmapped_returns_none() -> None:
    """from: plan §5 actor legacy map / contract #6 — an unmapped novel name → ``None``.

    ``None`` is the signal the validator turns into ``NON_CANONICAL_ACTOR``.
    """
    assert canonicalize_actor("NovelUnmappedActor") is None


def test_closed_vocab_constants_match_spec() -> None:
    """from: plan §0 resolution #2 + contract closed-vocab section.

    Guards the frozen vocab against silent drift — these four sets are the contract
    surface the validator enforces.
    """
    # Compare as plain sets (constant on the left reads naturally; set equality is exact).
    assert set(STATUS_VOCAB) == {"active", "superseded", "reversed", "deferred"}
    assert set(KIND_VOCAB) == {"architectural", "tactical"}
    assert set(FINDING_TYPE_VOCAB) == {"observation", "decision", "both"}
    assert set(CANONICAL_ACTORS) == {
        "ClaudeCodeVerification",
        "ClaudeCodeTestingBugs",
        "ClaudeCodePlanning",
        "ClaudeCodeDevelopment",
        "VSC-User",
    }
    # 'tactical' is a KIND, deliberately NOT a status (plan §0 resolution #2).
    assert "tactical" not in STATUS_VOCAB

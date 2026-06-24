"""Ledger table parser + free-text safety — ``genome.docs.ledger`` / ``model``.

Plan-blind spec source: the approved ``decision-tracking-leak-fix`` plan §5 (parser
tolerance — "free-text ``|`` in a ledger cell"; Task 2 ledger grammar) and the frozen
interface contract (``genome.docs.ledger`` surface, ``LedgerRow``). Expected values are
derived from that spec and from the committed ``MEMORY.md`` worked example (the dogfooded
``DEC-0001``/``DEC-0002`` pair) — never from the stubbed bodies (``ledger.py`` currently
``raise NotImplementedError``; RED is correct here).

The ``MEMORY.md`` worked-example rows are the realism anchor (finding-013): the parser
must round-trip the exact rows the repo ships, including the prose + fenced example text
that surrounds the table.
"""

from __future__ import annotations

import pytest

from genome.docs.ledger import (
    LEDGER_COLUMNS,
    LedgerError,
    escape_cell,
    parse_ledger,
    render_row,
    split_row,
)
from genome.docs.model import LedgerRow

# ---------------------------------------------------------------------------
# A ledger document modelled on the committed repo-root ``MEMORY.md`` worked
# example (finding-013 realism) — prose above, a fenced/markered table, prose
# below. The table is the real DEC-0001/DEC-0002 gnomAD-filter supersession pair.
# ---------------------------------------------------------------------------

_HEADER = (
    "| DEC | kind | date | status | superseded_by | actors | provenance |"
    " decision | detail-link |\n"
)
_SEPARATOR = "|---|---|---|---|---|---|---|---|---|\n"
_ROW_DEC_0001 = (
    "| DEC-0001 | architectural | 2026-05-22 | superseded | DEC-0002 |"
    " VSC-User, ClaudeCodeDevelopment | finding-011 |"
    " gnomAD frequency filter scoped three-way (union of user, ClinVar, GWAS, PGS);"
    " retained as the documented revert baseline |"
    " docs/findings/finding-011-gnomad-three-way-intersection.md |\n"
)
_ROW_DEC_0002 = (
    "| DEC-0002 | architectural | 2026-06-21 | active | — | VSC-User | finding-035 |"
    " gnomAD filter narrowed to `user_only` — the consumed subset |"
    " docs/findings/finding-035-gnomad-filter-set-consumer-audit.md |\n"
)
_LEDGER_DOC = (
    "# MEMORY — decision ledger\n"
    "\n"
    "Prose preamble describing the ledger. A stray pipe in prose | like this | is ignored.\n"
    "\n" + _HEADER + _SEPARATOR + _ROW_DEC_0001 + _ROW_DEC_0002 + "\n"
    "_Trailing prose below the table is also ignored by the parser._\n"
)


# ---------------------------------------------------------------------------
# Column contract + split/escape primitives.
# ---------------------------------------------------------------------------


def test_ledger_columns_match_spec() -> None:
    """from: plan Task 2 ledger grammar + contract ``LEDGER_COLUMNS``.

    The nine columns, in order, are the frozen table grammar.
    """
    assert LEDGER_COLUMNS == (
        "DEC",
        "kind",
        "date",
        "status",
        "superseded_by",
        "actors",
        "provenance",
        "decision",
        "detail-link",
    )


def test_split_row_drops_border_pipes_and_strips() -> None:
    """from: plan Task 2 (free-text safety) + contract ``split_row``.

    ``split_row`` drops the leading/trailing border pipes and strips each cell.
    """
    assert split_row("| a | b | c |") == ["a", "b", "c"]


def test_split_row_unescapes_free_text_pipe() -> None:
    """from: plan §5 free-text ``|`` in a ledger cell / contract ``split_row``.

    A ``\\|`` inside a cell is an ESCAPED pipe and must survive as a literal ``|`` in
    that one cell — it must NOT split the row into an extra column.
    """
    cells = split_row(r"| DEC-0007 | a \| b | c |")
    assert cells == ["DEC-0007", "a | b", "c"]


def test_escape_cell_is_inverse_of_split() -> None:
    """from: plan §5 free-text safety / contract ``escape_cell`` (inverse of split).

    ``escape_cell`` turns a literal ``|`` into ``\\|`` so a free-text ``decision`` cell
    can carry a pipe; reconstructing a single-cell row and splitting recovers the
    original value (dogfood round-trip).
    """
    raw = "either a | b approach"
    escaped = escape_cell(raw)
    assert escaped == r"either a \| b approach"
    assert split_row(f"| {escaped} |") == [raw]


# ---------------------------------------------------------------------------
# parse_ledger — locate the table, skip the separator, ignore surrounding prose.
# ---------------------------------------------------------------------------


def test_parse_ledger_finds_only_table_rows() -> None:
    """from: plan Task 2/3 (ledger parser ignores prose) + contract ``parse_ledger``.

    The worked-example document has prose above and below and a ``|---|`` separator;
    ``parse_ledger`` returns exactly the two data rows, in order, and ignores
    everything else (including a stray pipe in the prose preamble).
    """
    rows = parse_ledger(_LEDGER_DOC)
    assert [r.dec for r in rows] == ["DEC-0001", "DEC-0002"]


def test_parse_ledger_row_fields_round_trip_worked_example() -> None:
    """from: plan Task 2 worked example + contract ``LedgerRow``.

    Field-level parse of the real ``DEC-0001`` row: kind/date/status/actors/provenance/
    detail-link and the back-pointer all populate; ``superseded_by`` is the DEC id.
    Actors split on the comma into a tuple.
    """
    rows = parse_ledger(_LEDGER_DOC)
    dec1 = next(r for r in rows if r.dec == "DEC-0001")
    assert dec1.kind == "architectural"
    assert dec1.date == "2026-05-22"
    assert dec1.status == "superseded"
    assert dec1.superseded_by == "DEC-0002"
    assert dec1.actors == ("VSC-User", "ClaudeCodeDevelopment")
    assert dec1.provenance == "finding-011"
    assert dec1.detail_link == "docs/findings/finding-011-gnomad-three-way-intersection.md"


def test_parse_ledger_emdash_superseded_by_becomes_none() -> None:
    """from: contract ``parse_ledger`` (``—``/empty ``superseded_by`` → ``None``).

    The active ``DEC-0002`` row carries ``—`` in ``superseded_by``; that normalises to
    ``None`` (no back-pointer), distinct from the superseded row's DEC id.
    """
    rows = parse_ledger(_LEDGER_DOC)
    dec2 = next(r for r in rows if r.dec == "DEC-0002")
    assert dec2.status == "active"
    assert dec2.superseded_by is None


# ---------------------------------------------------------------------------
# render_row ↔ parse_ledger round-trip, incl. a free-text pipe (dogfood).
# ---------------------------------------------------------------------------


def test_render_row_round_trips_with_free_text_pipe() -> None:
    """from: plan §5 free-text ``|`` + Task 2 "worked example round-trips" (dogfood).

    A ``LedgerRow`` whose ``decision`` carries a raw ``|`` renders with the pipe escaped,
    and re-parsing the rendered row (wrapped in a minimal header+separator table)
    recovers an equal ``LedgerRow`` — content survives, column count is unchanged.
    """
    row = LedgerRow(
        dec="DEC-0042",
        kind="tactical",
        date="2026-06-23",
        status="active",
        actors=("VSC-User",),
        provenance="PR #93",
        decision="chose stdlib parser | not PyYAML for the flat block",
        detail_link="docs/findings/finding-036-decision-tracking-ledger.md",
        superseded_by=None,
    )
    rendered = render_row(row)
    # The raw pipe in the free-text cell is escaped in the rendered row.
    assert r"\|" in rendered
    header = "| " + " | ".join(LEDGER_COLUMNS) + " |\n"
    separator = "|" + "---|" * len(LEDGER_COLUMNS) + "\n"
    reparsed = parse_ledger(header + separator + rendered + "\n")
    assert reparsed == [row]


# ---------------------------------------------------------------------------
# parse_ledger negative — wrong column count / vocab / unmapped actor raise.
# ---------------------------------------------------------------------------


def test_parse_ledger_wrong_column_count_raises() -> None:
    """from: contract ``parse_ledger`` (wrong column count → ``LedgerError``).

    A data row with fewer cells than ``LEDGER_COLUMNS`` is a hard parse error — the
    table grammar is fixed-width.
    """
    doc = (
        "| " + " | ".join(LEDGER_COLUMNS) + " |\n"
        "|" + "---|" * len(LEDGER_COLUMNS) + "\n"
        "| DEC-0001 | architectural | 2026-05-22 |\n"  # truncated row
    )
    with pytest.raises(LedgerError):
        parse_ledger(doc)


def test_parse_ledger_bad_status_vocab_raises() -> None:
    """from: plan §5 integrity (closed-vocab status) — ledger leg.

    A ``status`` outside ``STATUS_VOCAB`` in a ledger row is a hard parse error.
    """
    doc = (
        "| " + " | ".join(LEDGER_COLUMNS) + " |\n"
        "|" + "---|" * len(LEDGER_COLUMNS) + "\n"
        "| DEC-0001 | architectural | 2026-05-22 | retired | — | VSC-User |"
        " PR #1 | a decision | docs/findings/finding-001.md |\n"
    )
    with pytest.raises(LedgerError):
        parse_ledger(doc)


def test_parse_ledger_bad_kind_vocab_raises() -> None:
    """from: plan §5 integrity (closed-vocab kind) — ledger leg.

    A ``kind`` outside ``KIND_VOCAB`` (``architectural``/``tactical``) is a hard error.
    """
    doc = (
        "| " + " | ".join(LEDGER_COLUMNS) + " |\n"
        "|" + "---|" * len(LEDGER_COLUMNS) + "\n"
        "| DEC-0001 | strategic | 2026-05-22 | active | — | VSC-User |"
        " PR #1 | a decision | docs/findings/finding-001.md |\n"
    )
    with pytest.raises(LedgerError):
        parse_ledger(doc)


def test_parse_ledger_unmapped_actor_raises() -> None:
    """from: plan §5 actor legacy map (unmapped novel name fails) — ledger leg."""
    doc = (
        "| " + " | ".join(LEDGER_COLUMNS) + " |\n"
        "|" + "---|" * len(LEDGER_COLUMNS) + "\n"
        "| DEC-0001 | architectural | 2026-05-22 | active | — | NovelUnmappedActor |"
        " PR #1 | a decision | docs/findings/finding-001.md |\n"
    )
    with pytest.raises(LedgerError):
        parse_ledger(doc)


def test_parse_ledger_maps_legacy_actor_via_map() -> None:
    """from: plan §5 actor legacy map (existing CHANGELOG names validate via the map).

    A ledger row authored with the legacy spelling ``VSC-Claude`` parses, and the actor
    is canonicalised to ``ClaudeCodeDevelopment`` (existing history validates).
    """
    doc = (
        "| " + " | ".join(LEDGER_COLUMNS) + " |\n"
        "|" + "---|" * len(LEDGER_COLUMNS) + "\n"
        "| DEC-0001 | tactical | 2026-05-22 | active | — | VSC-Claude |"
        " PR #1 | a decision | docs/findings/finding-001.md |\n"
    )
    rows = parse_ledger(doc)
    assert rows[0].actors == ("ClaudeCodeDevelopment",)

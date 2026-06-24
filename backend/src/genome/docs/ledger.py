"""Decision-ledger (``MEMORY.md``) table parser — the second grammar (plan Task 3).

The ledger is a single append-only markdown table. This parser is distinct from the
frontmatter parser (they share only the ``DEC-NNNN`` id-space, not a grammar — plan Task 0
closes the "one tolerant parser" conflation). It handles the free-text ``decision`` column
safely: raw ``|`` is escaped ``\\|`` and split only on *unescaped* pipes, so a decision
sentence containing a pipe, a backtick, or a colon round-trips.
"""

from __future__ import annotations

import re

from genome.docs.model import (
    BAD_KIND_VOCAB,
    BAD_STATUS_VOCAB,
    KIND_VOCAB,
    MALFORMED_LEDGER_ROW,
    NON_CANONICAL_ACTOR,
    STATUS_VOCAB,
    LedgerRow,
    canonicalize_actor,
)

#: The ledger table's column header, in file order. ``kind`` is the former ``grain``
#: (plan §0 resolution #2); ``status`` is the orthogonal lifecycle axis.
LEDGER_COLUMNS: tuple[str, ...] = (
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

#: Cell values that mean "no back-pointer" in the ``superseded_by`` column.
_NULL_POINTERS: frozenset[str] = frozenset({"", "—", "-"})
_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")


class LedgerError(ValueError):
    """Raised on a malformed ledger row.

    Carries a :attr:`code` (a ``genome.docs.model`` violation code) — wrong column count,
    a ``status``/``kind`` outside its closed vocab, or a non-canonical actor not covered by
    the legacy map — so ``genome docs check`` can surface it under the right code.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def escape_cell(value: str) -> str:
    """Escape a value for safe placement in a markdown table cell (raw ``|`` → ``\\|``).

    Inverse of the per-cell unescape applied by :func:`split_row`. Used when rendering DEC
    rows so a free-text decision never breaks the table.
    """
    return value.replace("|", "\\|")


def split_row(row: str) -> list[str]:
    """Split one markdown table row into cells on *unescaped* ``|``.

    Leading/trailing table-border pipes are dropped; each cell is stripped and has ``\\|``
    unescaped back to ``|``. This is the free-text-safe split the ``decision`` column
    depends on.
    """
    stripped = row.strip()
    stripped = stripped.removeprefix("|")
    stripped = stripped.removesuffix("|")
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(stripped):
        char = stripped[i]
        if char == "\\" and i + 1 < len(stripped) and stripped[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if char == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(char)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def _is_separator(cells: list[str]) -> bool:
    """True for a ``|---|---|`` table-separator row (every cell is dashes)."""
    return bool(cells) and all(_SEPARATOR_CELL_RE.match(cell) for cell in cells)


def iter_data_rows(text: str) -> list[tuple[int, list[str]]]:
    """Locate the ledger table and return ``(line_no, cells)`` for each data row.

    The table is found by its :data:`LEDGER_COLUMNS` header row; the ``|---|`` separator and
    all surrounding prose (a stray ``|`` in a sentence, the worked-example fences) are
    ignored. Parsing stops at the first non-table line after the header. ``line_no`` is
    1-based for error messages. Shared by :func:`parse_ledger` and the validator so the two
    cannot drift on where the table is.
    """
    lines = text.split("\n")
    header_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.strip().startswith("|") and split_row(line) == list(LEDGER_COLUMNS):
            header_idx = idx
            break
    if header_idx is None:
        return []
    out: list[tuple[int, list[str]]] = []
    for idx in range(header_idx + 1, len(lines)):
        if not lines[idx].strip().startswith("|"):
            break
        cells = split_row(lines[idx])
        if _is_separator(cells):
            continue
        out.append((idx + 1, cells))
    return out


def row_from_cells(cells: list[str]) -> tuple[LedgerRow | None, str | None]:
    """Build a :class:`LedgerRow` from split cells, or return ``(None, violation_code)``.

    The lenient core shared by :func:`parse_ledger` (which raises on a code) and the
    validator (which collects codes), so the two cannot drift on row validation.
    """
    if len(cells) != len(LEDGER_COLUMNS):
        return None, MALFORMED_LEDGER_ROW
    dec, kind, date_, status, superseded_by, actors_raw, provenance, decision, detail_link = cells
    if status not in STATUS_VOCAB:
        return None, BAD_STATUS_VOCAB
    if kind not in KIND_VOCAB:
        return None, BAD_KIND_VOCAB
    actors: list[str] = []
    for token in (part.strip() for part in actors_raw.split(",") if part.strip()):
        canonical = canonicalize_actor(token)
        if canonical is None:
            return None, NON_CANONICAL_ACTOR
        actors.append(canonical)
    row = LedgerRow(
        dec=dec,
        kind=kind,
        date=date_,
        status=status,
        actors=tuple(actors),
        provenance=provenance,
        decision=decision,
        detail_link=detail_link,
        superseded_by=None if superseded_by in _NULL_POINTERS else superseded_by,
    )
    return row, None


def parse_ledger(text: str) -> list[LedgerRow]:
    """Parse the decision table out of ``MEMORY.md`` text into :class:`LedgerRow` s.

    Locates the table by its :data:`LEDGER_COLUMNS` header, skips the ``|---|`` separator,
    and parses each subsequent table row. Prose and fenced examples above/below are ignored.
    Raises :class:`LedgerError` on the first malformed row.
    """
    rows: list[LedgerRow] = []
    for line_no, cells in iter_data_rows(text):
        row, code = row_from_cells(cells)
        if code is not None or row is None:
            msg = f"MEMORY.md ledger row at line {line_no}: {code}"
            raise LedgerError(msg, code=code or MALFORMED_LEDGER_ROW)
        rows.append(row)
    return rows


def render_row(row: LedgerRow) -> str:
    """Render a :class:`LedgerRow` as one pipe-delimited markdown table line (no newline).

    Round-trips with :func:`parse_ledger` (the worked example in ``MEMORY.md`` must parse
    through its own parser — the dogfood test). The free-text ``decision`` cell is escaped
    via :func:`escape_cell`; a ``None`` ``superseded_by`` renders as ``—``.
    """
    cells = (
        row.dec,
        row.kind,
        row.date,
        row.status,
        row.superseded_by or "—",
        ", ".join(row.actors),
        row.provenance,
        escape_cell(row.decision),
        row.detail_link,
    )
    return "| " + " | ".join(cells) + " |"

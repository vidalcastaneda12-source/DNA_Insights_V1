"""Finding-frontmatter parser — the first of the two grammars (plan Task 3).

A finding's frontmatter is a constrained, flat ``---``-fenced block prepended above the
``# Finding NNN`` H1. This parser is deliberately a **minimal stdlib parser**, not PyYAML
(plan Task 0): the block is a fixed, flat key set, and avoiding a new dependency keeps
``genome docs`` import-light and DB-free.

**The load-bearing safety rule:** the frontmatter block is recognised **only** when the very
first line of the document (after an optional UTF-8 BOM) is the fence ``---``. The parser
never scans the body. That is what keeps a body ``## Status`` heading, a ``|---|---|``
table-separator row (12 findings carry pipe-tables), or a ``---`` thematic break deeper in
the document from being mistaken for frontmatter.
"""

from __future__ import annotations

import re
from datetime import date

from genome.docs.model import (
    BAD_KIND_VOCAB,
    BAD_STATUS_VOCAB,
    BAD_TYPE_VOCAB,
    FINDING_TYPE_VOCAB,
    KIND_VOCAB,
    MALFORMED_FRONTMATTER,
    NON_CANONICAL_ACTOR,
    STATUS_VOCAB,
    Frontmatter,
    canonicalize_actor,
)

_FENCE_RE = re.compile(r"^---\s*$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ``kind`` is accepted on a finding (its canonical home is the DEC row, plan §0 #2) but not
# stored on :class:`Frontmatter`; it is validated against ``KIND_VOCAB`` if present.
_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"type", "status", "actors", "date", "supersedes", "superseded_by", "kind"},
)
_REQUIRED_KEYS: frozenset[str] = frozenset({"type", "status", "actors", "date"})


class FrontmatterError(ValueError):
    """Raised on a present-but-malformed frontmatter block.

    Carries a :attr:`code` (a ``genome.docs.model`` violation code) so
    ``genome docs check`` can surface the right dimension: a ``type``/``status``/``kind``
    outside its closed vocab, a non-canonical actor, or a structurally malformed block
    (unknown key, missing required key, bad date).
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split a finding document into ``(frontmatter_block, body)``.

    The block is the text **between** the leading ``---`` fence (which must be the first
    line, after an optional BOM) and the next ``---`` line. Returns ``(None, text)``
    unchanged when there is no leading fence — so the body is never re-scanned for a fence.
    ``frontmatter_block`` excludes the two fence lines.
    """
    probe = text.removeprefix("\ufeff")
    lines = probe.split("\n")
    if not lines or not _FENCE_RE.match(lines[0]):
        return None, text
    for i in range(1, len(lines)):
        if _FENCE_RE.match(lines[i]):
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    # Opened but never closed → not a valid frontmatter block.
    return None, text


def _parse_list(raw: str) -> list[str]:
    """Parse a flat flow sequence ``[a, b]`` (or ``[]``) into a list of trimmed tokens."""
    stripped = raw.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        return [p.strip() for p in inner.split(",") if p.strip()] if inner else []
    return [stripped] if stripped else []


def _collect_block(block: str) -> dict[str, str]:
    """Parse the ``key: value`` lines of a frontmatter block into a dict (raw values)."""
    data: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            msg = f"frontmatter line is not 'key: value': {line!r}"
            raise FrontmatterError(msg, code=MALFORMED_FRONTMATTER)
        key, _, raw = line.partition(":")
        key = key.strip()
        if key in data:
            msg = f"duplicate frontmatter key: {key!r}"
            raise FrontmatterError(msg, code=MALFORMED_FRONTMATTER)
        data[key] = raw.strip()
    return data


def _canonical_actors(raw: str) -> tuple[str, ...]:
    """Parse + canonicalise the ``actors`` list; raise on any unmapped token."""
    out: list[str] = []
    for token in _parse_list(raw):
        canonical = canonicalize_actor(token)
        if canonical is None:
            msg = f"non-canonical actor not covered by the legacy map: {token!r}"
            raise FrontmatterError(msg, code=NON_CANONICAL_ACTOR)
        out.append(canonical)
    return tuple(out)


def _build_frontmatter(data: dict[str, str]) -> Frontmatter:
    """Validate a collected key/value dict against the closed vocab and build the record."""
    unknown = set(data) - _ALLOWED_KEYS
    if unknown:
        msg = f"unknown frontmatter key(s): {sorted(unknown)}"
        raise FrontmatterError(msg, code=MALFORMED_FRONTMATTER)
    missing = _REQUIRED_KEYS - set(data)
    if missing:
        msg = f"missing required frontmatter key(s): {sorted(missing)}"
        raise FrontmatterError(msg, code=MALFORMED_FRONTMATTER)
    if data["type"] not in FINDING_TYPE_VOCAB:
        msg = f"type {data['type']!r} not in vocab"
        raise FrontmatterError(msg, code=BAD_TYPE_VOCAB)
    if data["status"] not in STATUS_VOCAB:
        msg = f"status {data['status']!r} not in vocab"
        raise FrontmatterError(msg, code=BAD_STATUS_VOCAB)
    if "kind" in data and data["kind"] not in KIND_VOCAB:
        msg = f"kind {data['kind']!r} not in vocab"
        raise FrontmatterError(msg, code=BAD_KIND_VOCAB)
    if not _ISO_DATE_RE.match(data["date"]) or not _is_iso_date(data["date"]):
        msg = f"date {data['date']!r} is not ISO-8601"
        raise FrontmatterError(msg, code=MALFORMED_FRONTMATTER)
    return Frontmatter(
        type=data["type"],
        status=data["status"],
        actors=_canonical_actors(data["actors"]),
        date=data["date"],
        supersedes=tuple(_parse_list(data.get("supersedes", "[]"))),
        superseded_by=tuple(_parse_list(data.get("superseded_by", "[]"))),
    )


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def parse_frontmatter(text: str) -> Frontmatter | None:
    """Parse the leading frontmatter block of a finding into a :class:`Frontmatter`.

    Returns ``None`` when the document has no leading fence (the CAPTURE-miss case the
    validator turns into ``MISSING_FRONTMATTER``). Raises :class:`FrontmatterError` on a
    present-but-malformed block. Actor tokens are canonicalised through the legacy map; an
    unmapped actor is a :class:`FrontmatterError`.
    """
    block, _ = split_frontmatter(text)
    if block is None:
        return None
    return _build_frontmatter(_collect_block(block))


def render_frontmatter(frontmatter: Frontmatter) -> str:
    """Render a :class:`Frontmatter` back into its ``---``-fenced text block.

    Round-trips with :func:`parse_frontmatter` (dogfooded by the new-finding scaffolding and
    the parser tests). Output ends with a trailing newline so it can be prepended directly
    above an existing ``# Finding NNN`` H1.
    """
    lines = [
        "---",
        f"type: {frontmatter.type}",
        f"status: {frontmatter.status}",
        f"actors: [{', '.join(frontmatter.actors)}]",
        f"date: {frontmatter.date}",
        f"supersedes: [{', '.join(frontmatter.supersedes)}]",
        f"superseded_by: [{', '.join(frontmatter.superseded_by)}]",
        "---",
    ]
    return "\n".join(lines) + "\n"

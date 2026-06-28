"""Validate the ``const SCHEMAS = { ... }`` block of a team workflow file.

The real Workflow engine passes each per-agent ``schema`` straight through as the Anthropic
StructuredOutput ``input_schema``; the API rejects any schema without a top-level ``type``
(``400 … input_schema.type: Field required``). This module locates the ``SCHEMAS`` object and
flags any top-level entry that does not declare ``type: 'object'`` — the schema-validity half of
the reversal-gate, which permanently locks the C2+D Phase 2 PR 1 fix against regression.

Pure text inspection (a small string/comment-aware brace matcher); imports no :mod:`genome.db`.
"""

from __future__ import annotations

import re

_SCHEMAS_RE = re.compile(r"const\s+SCHEMAS\s*=\s*\{")
_ENTRY_KEY_RE = re.compile(r"[A-Za-z_$][\w$]*")
_TYPE_OBJECT_RE = re.compile(r"""type\s*:\s*['"]object['"]""")


def _skip_string(src: str, i: int) -> int:
    """Given ``i`` at an opening quote (``'`` ``"`` `` ` ``), return the index past the close."""
    quote = src[i]
    i += 1
    n = len(src)
    while i < n:
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    return i


def _skip_comment(src: str, i: int) -> int:
    """Given ``i`` at the start of a ``//`` or ``/*`` comment, return the index just past it."""
    n = len(src)
    if src[i + 1] == "/":
        i += 2
        while i < n and src[i] != "\n":
            i += 1
        return i
    i += 2
    while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
        i += 1
    return i + 2


def _match_brace(src: str, start: int) -> int:
    """Index of the ``}`` matching the ``{`` at ``start`` (string/comment aware), or ``-1``."""
    depth = 0
    i = start
    n = len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] in "/*":
            i = _skip_comment(src, i)
            continue
        if c in "\"'`":
            i = _skip_string(src, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def extract_schemas_block(body: str) -> str | None:
    """Return the ``const SCHEMAS = { ... }`` object text (incl. braces), or ``None``.

    ``None`` is the fail-closed signal — the caller turns it into a ``SCHEMAS_NOT_LOCATED``
    violation.
    """
    match = _SCHEMAS_RE.search(body)
    if match is None:
        return None
    brace_start = body.find("{", match.start())
    if brace_start < 0:
        return None
    end = _match_brace(body, brace_start)
    if end < 0:
        return None
    return body[brace_start : end + 1]


def schema_entry_keys_without_type(block: str) -> list[str]:
    """Return the top-level ``SCHEMAS`` entry keys whose object lacks a ``type: 'object'``.

    ``block`` is the object text including its outer braces. Each top-level ``key: { ... }`` entry
    must declare ``type: 'object'`` (the post-400-fix shape); a bare ``{ required: [...] }`` is the
    regression this catches.
    """
    missing: list[str] = []
    n = len(block)
    i = 1  # just past the outer `{`
    while i < n:
        ch = block[i]
        if ch == "}":
            break
        key_match = _ENTRY_KEY_RE.match(block, i)
        if key_match is None:
            i += 1
            continue
        key = key_match.group(0)
        j = key_match.end()
        while j < n and (block[j].isspace() or block[j] == ":"):
            j += 1
        if j < n and block[j] == "{":
            end = _match_brace(block, j)
            if end < 0:
                break
            if _TYPE_OBJECT_RE.search(block[j : end + 1]) is None:
                missing.append(key)
            i = end + 1
        else:
            i = j
    return missing

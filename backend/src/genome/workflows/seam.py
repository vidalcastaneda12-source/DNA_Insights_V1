"""Locate and normalize the duplicated ``agent()``/retry seam in a team workflow file.

GT-1 forbids the three self-contained workflows from importing a shared lib, so the
``withRetry`` + ``call`` seam is **duplicated** per file and must stay logically identical.
This module is the Python half of the node mirror in
``.claude/workflows/__tests__/drift.test.mjs``: it locates the seam by the
``// agent-seam:start`` / ``// agent-seam:end`` sentinels and normalizes the two legitimate
per-file dimensions (the workflow-name label; incidental string-literal line-wrapping) so what
remains is the seam LOGIC.

The gate is **fail-closed on a missing sentinel**: unlike ``drift.test.mjs`` (which has a
brace-balanced fallback as a node-side convenience), the gate *requires* the sentinel
convention — an unlocatable seam is a violation, not a silent fallback. This is consistent with
the un-skipped ``drift.test.mjs`` test that asserts every seam is sentinel-delimited.
"""

from __future__ import annotations

import re

# `// agent-seam:start … // agent-seam:end` — DOTALL so the multi-line seam body is captured.
_SENTINEL_RE = re.compile(r"//\s*agent-seam:start(.*?)//\s*agent-seam:end", re.DOTALL)

# The two legitimate per-file dimensions normalized away before comparison:
#   1. adjacent string-literal concatenation (`'a ' + 'b'` vs `'a b'` — incidental line-wrap);
#   2. (handled by the caller) the per-workflow name label, replaced with a fixed placeholder.
_CONCAT_RE = re.compile(r"""['"`]\s*\+\s*['"`]""")
_WS_RE = re.compile(r"\s+")


def extract_seam(body: str) -> str | None:
    """Return the sentinel-delimited seam text (stripped), or ``None`` if not located.

    ``None`` is the fail-closed signal — the caller turns it into a ``SEAM_NOT_LOCATED``
    violation.
    """
    match = _SENTINEL_RE.search(body)
    if match is None:
        return None
    return match.group(1).strip()


def normalize_seam(text: str, stem: str) -> str:
    """Normalize the two legit per-file dimensions, mirroring ``drift.test.mjs`` ``normalizeSeam``.

    Lower-case, replace the (lower-cased) workflow-name ``stem`` with a fixed placeholder, drop
    adjacent string-literal concatenations, and collapse whitespace. What remains is the seam
    logic, which GT-1 requires to be identical across the three copies.
    """
    out = text.lower()
    out = out.replace(stem, "WF")
    out = _CONCAT_RE.sub("", out)
    out = _WS_RE.sub(" ", out)
    return out.strip()

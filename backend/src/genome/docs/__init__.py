"""Decision-tracking docs tooling — the ``genome docs`` surface (plan: decision-tracking
leak fix / finding-036).

Realizes the previously-dangling "MEMORY index" the agent automation already references
(`.claude/agents/knowledge-curator.md`, finding-034): a repo-root ``MEMORY.md`` decision
ledger + per-finding frontmatter, a generated single-index retrieval surface, and a
validator gate that relocates locked decision #7's no-torn-state invariant onto a
markdown substrate that has no transaction of its own.

**This package imports no** :mod:`genome.db`. ``python -c "import genome.docs"`` and
``genome docs check`` must run on a fresh checkout with no DuckDB / SQLCipher built
(plan Task 3). Do not add a database import here or in any module it pulls in.
"""

from __future__ import annotations

from genome.docs.cli import docs_app
from genome.docs.frontmatter import (
    FrontmatterError,
    parse_frontmatter,
    render_frontmatter,
    split_frontmatter,
)
from genome.docs.index import build_index, render_index_table
from genome.docs.ledger import (
    LEDGER_COLUMNS,
    LedgerError,
    escape_cell,
    parse_ledger,
    render_row,
    split_row,
)
from genome.docs.model import (
    CANONICAL_ACTORS,
    FINDING_TYPE_VOCAB,
    INDEX_BEGIN_MARKER,
    INDEX_END_MARKER,
    KIND_VOCAB,
    LEDGER_FILENAME,
    LEGACY_ACTOR_MAP,
    STATUS_VOCAB,
    CheckReport,
    CheckViolation,
    Frontmatter,
    IndexResult,
    LedgerRow,
    canonicalize_actor,
)
from genome.docs.validator import anchor_numbers, check

__all__ = [
    "CANONICAL_ACTORS",
    "FINDING_TYPE_VOCAB",
    "INDEX_BEGIN_MARKER",
    "INDEX_END_MARKER",
    "KIND_VOCAB",
    "LEDGER_COLUMNS",
    "LEDGER_FILENAME",
    "LEGACY_ACTOR_MAP",
    "STATUS_VOCAB",
    "CheckReport",
    "CheckViolation",
    "Frontmatter",
    "FrontmatterError",
    "IndexResult",
    "LedgerError",
    "LedgerRow",
    "anchor_numbers",
    "build_index",
    "canonicalize_actor",
    "check",
    "docs_app",
    "escape_cell",
    "parse_frontmatter",
    "parse_ledger",
    "render_frontmatter",
    "render_index_table",
    "render_row",
    "split_frontmatter",
    "split_row",
]

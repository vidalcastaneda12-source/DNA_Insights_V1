"""Apply DDL to a fresh DuckDB / SQLCipher pair and seed `user_preferences`.

`init_databases()` is idempotent: if either DB already exists it is left untouched.
Every DDL statement is applied; any failure is raised — the previous skip-on-fail
behavior is gone now that the schema is DuckDB-clean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection

if TYPE_CHECKING:
    from collections.abc import Iterable

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
DDL_DIR: Final[Path] = REPO_ROOT / "ddl"

DUCKDB_DDL_FILES: Final[tuple[str, ...]] = (
    "group_1_genotype.sql",
    "group_2_annotations.sql",
    "group_3_derived.sql",
    "group_4_insights.sql",
)
SQLITE_DDL_FILE: Final[str] = "group_5_app_state.sql"

USER_PREFERENCES_SEED: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("current_profile_id", "1", "number", "Active profile"),
    ("default_audience", "layperson", "string", "Insight rendering: eli5 / layperson / clinical"),
    ("imputation_r2_threshold", "0.3", "number", "Minimum R^2 to use imputed variants"),
    ("theme", "system", "string", "UI theme: light / dark / system"),
    ("llm_model", "claude-opus-4-7", "string", "Model for NL queries"),
    ("audit_retention_days", "365", "number", "How long to keep audit logs"),
    ("external_calls_enabled", "false", "boolean", "Master switch for any network egress"),
    ("pubmed_enrichment_enabled", "false", "boolean", "Auto-fetch PubMed for variants"),
    ("auto_snapshot_cadence", "90d", "string", "Auto snapshots every N days ('' = off)"),
    ("prs_min_coverage_pct", "80", "number", "Hide PGS results below this coverage"),
    ("font_size", "medium", "string", "UI font size"),
    ("cite_in_responses", "true", "boolean", "Include citations in LLM-generated text"),
)


@dataclass(frozen=True)
class InitResult:
    """Summary of what `init_databases` did on this invocation."""

    duckdb_created: bool
    sqlite_created: bool
    duckdb_path: Path
    sqlite_path: Path


@dataclass
class _SplitState:
    """Mutable parser state for `_split_sql`."""

    in_single: bool = False
    in_line_comment: bool = False
    in_block_comment: bool = False


def _consume_one(  # noqa: C901, PLR0911 — explicit branches per parser mode read more clearly than nested ones
    state: _SplitState,
    buf: list[str],
    c: str,
    nxt: str,
) -> int:
    """Append one character (or two) to ``buf`` and advance the cursor accordingly."""
    if state.in_line_comment:
        buf.append(c)
        if c == "\n":
            state.in_line_comment = False
        return 1
    if state.in_block_comment:
        buf.append(c)
        if c == "*" and nxt == "/":
            buf.append(nxt)
            state.in_block_comment = False
            return 2
        return 1
    if state.in_single:
        buf.append(c)
        if c == "'":
            if nxt == "'":  # escaped quote
                buf.append(nxt)
                return 2
            state.in_single = False
        return 1
    if c == "-" and nxt == "-":
        buf.append(c)
        state.in_line_comment = True
        return 1
    if c == "/" and nxt == "*":
        buf.append(c)
        state.in_block_comment = True
        return 1
    if c == "'":
        buf.append(c)
        state.in_single = True
        return 1
    buf.append(c)
    return 1


def _split_sql(content: str) -> list[str]:
    """Split a DDL script into individual statements, respecting strings and comments."""
    statements: list[str] = []
    buf: list[str] = []
    state = _SplitState()
    i, n = 0, len(content)
    while i < n:
        c = content[i]
        nxt = content[i + 1] if i + 1 < n else ""
        if (
            c == ";"
            and not state.in_single
            and not state.in_line_comment
            and not state.in_block_comment
        ):
            stmt = "".join(buf).strip()
            if _strip_comments(stmt):
                statements.append(stmt)
            buf = []
            i += 1
            continue
        i += _consume_one(state, buf, c, nxt)

    tail = "".join(buf).strip()
    if _strip_comments(tail):
        statements.append(tail)
    return statements


def _strip_comments(stmt: str) -> str:
    """Return the statement with line comments and whitespace removed (for emptiness check)."""
    lines = [line for line in stmt.split("\n") if not line.lstrip().startswith("--")]
    return "\n".join(lines).strip()


def _apply_duckdb_ddl(conn: DuckDBPyConnection, files: Iterable[Path]) -> None:
    for path in files:
        log = logger.bind(file=path.name)
        log.info("applying duckdb ddl")
        for stmt in _split_sql(path.read_text(encoding="utf-8")):
            conn.execute(stmt)


_CREATE_VIEW_RE: Final[re.Pattern[str]] = re.compile(r"\bCREATE\s+VIEW\b", re.IGNORECASE)


def _find_create_view(ddl_text: str, view_name: str) -> str | None:
    """Return the ``CREATE VIEW <view_name>`` statement from ``ddl_text``, or None."""
    pattern = re.compile(rf"\bCREATE\s+VIEW\s+{re.escape(view_name)}\b", re.IGNORECASE)
    for stmt in _split_sql(ddl_text):
        if pattern.search(stmt):
            return stmt
    return None


def materialize_view(
    conn: DuckDBPyConnection,
    view_name: str,
    *,
    ddl_file: str = "group_1_genotype.sql",
) -> None:
    """Create-or-replace a single view from its canonical DDL definition.

    A fresh ``genome init`` creates every view; an existing ``genome.duckdb``
    that predates a view-only schema addition needs the new view materialized
    *without* a full rebuild — the PR #68 "view-only ⇒ no ``rm -rf data/``"
    path. This reads the canonical ``CREATE VIEW`` statement from the DDL file
    (the source of truth, kept in lock-step with the schema markdown) and runs
    it as ``CREATE OR REPLACE VIEW``, so it is idempotent and a no-op when the
    live view already matches.
    """
    text = (DDL_DIR / ddl_file).read_text(encoding="utf-8")
    statement = _find_create_view(text, view_name)
    if statement is None:
        msg = f"CREATE VIEW {view_name!r} not found in {ddl_file}"
        raise ValueError(msg)
    conn.execute(_CREATE_VIEW_RE.sub("CREATE OR REPLACE VIEW", statement, count=1))


def _seed_user_preferences(conn: object) -> None:
    """Insert the canonical user_preferences seed rows. Idempotent via INSERT OR IGNORE."""
    conn.executemany(  # type: ignore[attr-defined]
        "INSERT OR IGNORE INTO user_preferences (pref_key, pref_value, value_type, description)"
        " VALUES (?, ?, ?, ?)",
        list(USER_PREFERENCES_SEED),
    )


def init_databases() -> InitResult:
    """Create both databases on first run, skip on subsequent runs.

    On first DuckDB creation, applies group 1-4 DDL in order. On first SQLite
    creation, applies group 5 DDL (which already inserts the seed `profiles`
    row) and then seeds `user_preferences`.
    """
    settings = get_settings()
    settings.genome_duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    settings.app_db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.archive_path.mkdir(parents=True, exist_ok=True)

    duckdb_existed = settings.genome_duckdb_path.exists()
    sqlite_existed = settings.app_db_path.exists()

    if duckdb_existed:
        logger.info("duckdb already present; skipping", path=str(settings.genome_duckdb_path))
    else:
        logger.info("creating duckdb", path=str(settings.genome_duckdb_path))
        with duckdb_connection() as conn:
            _apply_duckdb_ddl(conn, [DDL_DIR / f for f in DUCKDB_DDL_FILES])

    if sqlite_existed:
        logger.info("app.db already present; skipping", path=str(settings.app_db_path))
    else:
        logger.info("creating app.db", path=str(settings.app_db_path))
        from genome.db.sqlite_conn import sqlcipher_connection  # noqa: PLC0415

        with sqlcipher_connection() as conn:
            sql = (DDL_DIR / SQLITE_DDL_FILE).read_text(encoding="utf-8")
            conn.executescript(sql)
            _seed_user_preferences(conn)
            conn.commit()

    return InitResult(
        duckdb_created=not duckdb_existed,
        sqlite_created=not sqlite_existed,
        duckdb_path=settings.genome_duckdb_path,
        sqlite_path=settings.app_db_path,
    )

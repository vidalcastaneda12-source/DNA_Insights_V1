"""Context-managed DuckDB connection."""

from __future__ import annotations

import stat
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from genome.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from duckdb import DuckDBPyConnection

_OWNER_RW_ONLY = stat.S_IRUSR | stat.S_IWUSR  # 0o600


def _ensure_owner_only(path: Path) -> None:
    """Restrict the file to owner read/write only (0600).

    Idempotent. No-op if the file does not exist.
    """
    if not path.exists():
        return
    current = stat.S_IMODE(path.stat().st_mode)
    if current != _OWNER_RW_ONLY:
        path.chmod(_OWNER_RW_ONLY)


@contextmanager
def duckdb_connection(
    path: Path | str | None = None,
    *,
    read_only: bool = False,
) -> Iterator[DuckDBPyConnection]:
    """Open a DuckDB connection at ``path`` (defaults to settings) and close it on exit.

    On first creation the file is chmod'd to 0600. Per locked decision #6, the DuckDB
    file itself is not encrypted; rely on filesystem perms + OS full-disk encryption.
    """
    if path is None:
        path = get_settings().genome_duckdb_path
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(database=str(db_path), read_only=read_only)
    try:
        _ensure_owner_only(db_path)
        yield conn
    finally:
        conn.close()
        _ensure_owner_only(db_path)

"""Context-managed SQLCipher (encrypted SQLite) connection."""

from __future__ import annotations

import stat
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from pysqlcipher3 import dbapi2 as sqlcipher

from genome.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

_OWNER_RW_ONLY = stat.S_IRUSR | stat.S_IWUSR  # 0o600


def _quote_passphrase(passphrase: str) -> str:
    """Escape single quotes for safe inclusion in a ``PRAGMA key`` literal."""
    return passphrase.replace("'", "''")


def _ensure_owner_only(path: Path) -> None:
    if not path.exists():
        return
    current = stat.S_IMODE(path.stat().st_mode)
    if current != _OWNER_RW_ONLY:
        path.chmod(_OWNER_RW_ONLY)


@contextmanager
def sqlcipher_connection(
    path: Path | str | None = None,
    passphrase: str | None = None,
) -> Iterator[Connection]:
    """Open an encrypted SQLite connection and close it on exit.

    The first statement on every new connection sets ``PRAGMA key`` with the configured
    passphrase, then enables foreign keys and WAL journalling.
    """
    settings = get_settings()
    db_path = Path(path) if path is not None else settings.app_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    secret = passphrase if passphrase is not None else settings.app_db_passphrase.get_secret_value()
    if not secret:
        msg = "APP_DB_PASSPHRASE is required to open the encrypted app.db"
        raise RuntimeError(msg)

    conn = sqlcipher.connect(str(db_path))
    try:
        # Key MUST be the very first statement on the connection.
        conn.execute(f"PRAGMA key = '{_quote_passphrase(secret)}';")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        _ensure_owner_only(db_path)
        yield conn
    finally:
        conn.close()
        _ensure_owner_only(db_path)

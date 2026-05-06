"""Both DBs open, accept a trivial query, and close cleanly."""

from __future__ import annotations

from pathlib import Path

import pytest

from genome.db import duckdb_connection, init_databases, sqlcipher_connection


def test_duckdb_connection_round_trip(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    init_databases()
    with duckdb_connection() as conn:
        result = conn.execute("SELECT 1 + 1").fetchone()
    assert result == (2,)


def test_sqlcipher_connection_round_trip(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    init_databases()
    with sqlcipher_connection() as conn:
        result = conn.execute("SELECT 1 + 1").fetchone()
    assert result == (2,)


def test_sqlcipher_rejects_wrong_passphrase(isolated_settings: dict[str, str]) -> None:
    init_databases()
    path = Path(isolated_settings["APP_DB_PATH"])
    # pysqlcipher3 raises DatabaseError (subclass of Exception) on a wrong key
    # only when a query actually touches the cipher pages, which happens here
    # in the ``SELECT COUNT(*)``.
    with (
        pytest.raises(Exception, match=r".+"),
        sqlcipher_connection(path, passphrase="definitely-wrong") as conn,
    ):
        conn.execute("SELECT COUNT(*) FROM profiles").fetchone()


def test_sqlcipher_pragmas_applied(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    init_databases()
    with sqlcipher_connection() as conn:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()
        journal = conn.execute("PRAGMA journal_mode").fetchone()
    assert fk[0] == 1
    assert journal[0].lower() == "wal"

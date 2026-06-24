from genome.db.duckdb_conn import duckdb_connection
from genome.db.init_schema import init_databases

# `sqlcipher_connection` is intentionally NOT re-exported here. Re-exporting it eagerly pulls
# pysqlcipher3 into every `genome.db` consumer, which breaks `genome docs check` on a fresh
# checkout with no SQLCipher built (finding-036 follow-up). Import it directly from
# `genome.db.sqlite_conn` where the encrypted app.db is actually opened.
__all__ = ["duckdb_connection", "init_databases"]

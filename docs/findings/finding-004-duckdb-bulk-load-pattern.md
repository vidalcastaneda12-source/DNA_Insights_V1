---
type: decision
status: active
actors: [ClaudeCodeDevelopment]
date: 2026-05-12
supersedes: []
superseded_by: []
---
# DuckDB bulk-load: `executemany` vs PyArrow registration

## Context

The Phase 2 writer originally used
`conn.executemany("INSERT INTO _ingest_stage VALUES (?, ?, ...)", rows)` to
load ~600K rows into a temp staging table. Real-data ingestion took ~32
minutes on macOS (~14 minutes on Windows).

## Observation

DuckDB's `executemany` does not batch-bind; it re-prepares and re-executes
the prepared statement per row, with per-row parser/planner/binding
overhead. Documented in DuckDB's own performance docs as an anti-pattern for
bulk loads.

Replacement: Build a `pyarrow.Table` from the rows in one pass, then
`conn.register("staging_view", table)` followed by
`INSERT INTO _ingest_stage SELECT * FROM staging_view`. PyArrow Table
construction is C-implemented and effectively constant-time per row. DuckDB
consumes the registered Arrow Table zero-copy.

Result: Real-data ingest dropped from ~32 minutes to ~17 seconds — a ~117×
speedup. Output is byte-identical to the previous implementation.

## Implication

All future bulk inserts into DuckDB should use PyArrow Table registration,
not `executemany`. This is captured as a convention in `CLAUDE.md`. Phase 4
and later should follow the same pattern.

## Follow-up

None. The convention is documented and the benchmark test (100K rows in
<2s) protects against regression.

# Group 5 — App State Schema

The mutable, personal, sensitive layer: notes, bookmarks, observations, medications, queries, audit log, snapshots, jobs, preferences, and profiles.

**Target:** SQLite (`app.db`)
**Encryption:** SQLCipher with user passphrase
**Foreign keys to DuckDB tables are SOFT** — no DB-level enforcement (different DB), validated in application.

---

## Design principles

1. **Cross-DB references are application-validated.** When an `insight_id` lives in DuckDB but is referenced from `notes.subject_id` in SQLite, the app validates on insert. No DB-level FK is possible.

2. **Profile-aware from day one.** Every user-data table has `profile_id`. For v1, all rows have `profile_id = 1`. Each profile has its own DuckDB file path; multi-profile is just adding rows to `profiles` and pointing at new DuckDBs. No schema migration when v2 lands.

3. **Soft-delete where audit matters.** Medications use `is_active` rather than DELETE so the PGx warning history is preserved.

4. **Triggers for `updated_at`.** Unlike DuckDB, SQLite supports triggers — we use them to keep `updated_at` accurate without app-layer discipline.

5. **FTS5 for note search.** Notes are markdown blobs; full-text search is meaningfully better than `LIKE`.

6. **Encrypted at rest with SQLCipher.** All tables here contain personal data; the database file is unreadable without the passphrase. Combined with OS full-disk encryption from decision #6.

---

## Pragmas

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
```

---

## Profiles

```sql
CREATE TABLE profiles (
    profile_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    relationship        TEXT,                           -- 'self', 'spouse', 'child', 'parent', 'other'
    duckdb_path         TEXT NOT NULL,                  -- path to this profile's genome.duckdb
    is_active           INTEGER DEFAULT 1,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_profiles_active ON profiles(is_active);
```

> The "current profile" lives in `user_preferences` (key `current_profile_id`).

---

## Notes (with FTS5 full-text search)

```sql
CREATE TABLE notes (
    note_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          INTEGER NOT NULL REFERENCES profiles(profile_id),

    -- Polymorphic subject (matches insights.subject_type pattern)
    subject_type        TEXT NOT NULL CHECK (subject_type IN
        ('variant', 'gene', 'pathway', 'score', 'haplotype',
         'insight', 'trait', 'medication', 'observation', 'general')),
    subject_id          TEXT NOT NULL,                  -- variant_id, gene_symbol, insight UUID, etc.

    -- Content
    title               TEXT,
    body_md             TEXT NOT NULL,                  -- markdown

    -- Tags
    tags                TEXT,                           -- comma-separated; lightweight

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_notes_subject  ON notes(subject_type, subject_id);
CREATE INDEX idx_notes_profile  ON notes(profile_id);
CREATE INDEX idx_notes_created  ON notes(created_at);

-- FTS5 virtual table for note search
CREATE VIRTUAL TABLE notes_fts USING fts5(
    title, body_md, tags,
    content='notes',
    content_rowid='note_id',
    tokenize='porter unicode61'
);

-- FTS sync triggers
CREATE TRIGGER notes_fts_insert AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, body_md, tags)
    VALUES (new.note_id, new.title, new.body_md, new.tags);
END;

CREATE TRIGGER notes_fts_delete AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body_md, tags)
    VALUES ('delete', old.note_id, old.title, old.body_md, old.tags);
END;

CREATE TRIGGER notes_fts_update AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body_md, tags)
    VALUES ('delete', old.note_id, old.title, old.body_md, old.tags);
    INSERT INTO notes_fts(rowid, title, body_md, tags)
    VALUES (new.note_id, new.title, new.body_md, new.tags);
END;

CREATE TRIGGER notes_updated_at AFTER UPDATE ON notes BEGIN
    UPDATE notes SET updated_at = CURRENT_TIMESTAMP WHERE note_id = new.note_id;
END;
```

---

## Bookmarks

```sql
CREATE TABLE bookmarks (
    bookmark_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          INTEGER NOT NULL REFERENCES profiles(profile_id),

    subject_type        TEXT NOT NULL CHECK (subject_type IN
        ('variant', 'gene', 'pathway', 'score', 'haplotype',
         'insight', 'trait', 'query')),
    subject_id          TEXT NOT NULL,

    label               TEXT,                           -- user-given name
    tags                TEXT,
    folder              TEXT,                           -- optional grouping
    color               TEXT,                           -- hex code for UI

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (profile_id, subject_type, subject_id)
);

CREATE INDEX idx_bookmarks_subject ON bookmarks(subject_type, subject_id);
CREATE INDEX idx_bookmarks_folder  ON bookmarks(profile_id, folder);
```

---

## Personal observations (the differentiated layer)

Two-table model: `observation_phenotypes` defines what you're tracking; `observations` records each data point.

```sql
CREATE TABLE observation_phenotypes (
    phenotype_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          INTEGER NOT NULL REFERENCES profiles(profile_id),

    name                TEXT NOT NULL,                  -- 'caffeine sensitivity', 'LDL', 'sleep duration'
    description         TEXT,
    value_type          TEXT NOT NULL CHECK (value_type IN
        ('numeric', 'categorical', 'scale', 'boolean', 'text')),
    units               TEXT,                           -- 'mg/dL', 'hours', etc.
    allowed_values      TEXT,                           -- JSON array if categorical/scale

    -- Genotype linkage (so we can match observations to expectations)
    trait_ids           TEXT,                           -- JSON array of EFO/HPO IDs

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (profile_id, name)
);

CREATE INDEX idx_obspheno_profile ON observation_phenotypes(profile_id);

CREATE TABLE observations (
    observation_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          INTEGER NOT NULL REFERENCES profiles(profile_id),
    phenotype_id        INTEGER NOT NULL REFERENCES observation_phenotypes(phenotype_id),

    -- Value (one of these populated based on phenotype.value_type)
    value_numeric       REAL,
    value_text          TEXT,
    value_boolean       INTEGER,                        -- 0/1

    -- Context
    observed_at         TIMESTAMP NOT NULL,
    notes               TEXT,
    source              TEXT,                           -- 'self_report', 'lab_result', 'doctor_visit', 'wearable'

    -- Genotype reconciliation (computed by app on save)
    matches_expectation INTEGER,                        -- -1=contradicts, 0=neutral, 1=matches, NULL=unknown
    linked_insight_ids  TEXT,                           -- JSON array of insight UUIDs

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_obs_phenotype  ON observations(phenotype_id);
CREATE INDEX idx_obs_observed   ON observations(observed_at);
CREATE INDEX idx_obs_profile    ON observations(profile_id);
```

---

## Medications (live PGx checking)

```sql
CREATE TABLE medications (
    medication_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          INTEGER NOT NULL REFERENCES profiles(profile_id),

    drug_name           TEXT NOT NULL,
    rxnorm_id           TEXT,                           -- canonical drug ID
    atc_code            TEXT,                           -- alternative classification

    -- Prescription details
    dosage              TEXT,
    frequency           TEXT,                           -- 'daily', 'BID', 'PRN'
    indication          TEXT,
    prescriber          TEXT,

    -- Lifecycle
    started_at          DATE,
    ended_at            DATE,                           -- NULL if active
    is_active           INTEGER DEFAULT 1,

    -- PGx warning cache (recomputed when meds or PGx phenotypes change)
    pgx_warning_count   INTEGER DEFAULT 0,
    pgx_last_checked    TIMESTAMP,

    notes               TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_meds_active   ON medications(profile_id, is_active);
CREATE INDEX idx_meds_rxnorm   ON medications(rxnorm_id);
CREATE INDEX idx_meds_drug     ON medications(drug_name);

CREATE TRIGGER meds_updated_at AFTER UPDATE ON medications BEGIN
    UPDATE medications SET updated_at = CURRENT_TIMESTAMP WHERE medication_id = new.medication_id;
END;
```

---

## Saved queries (long-running NL investigations)

```sql
CREATE TABLE saved_queries (
    query_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          INTEGER NOT NULL REFERENCES profiles(profile_id),

    name                TEXT NOT NULL,
    description         TEXT,

    -- The query
    natural_language    TEXT NOT NULL,
    resolved_query      TEXT,                           -- cached SQL or tool call sequence
    resolution_method   TEXT,                           -- 'llm_to_sql', 'llm_tool_chain'

    -- Auto-rerun
    auto_rerun          INTEGER DEFAULT 0,
    rerun_cadence       TEXT,                           -- '1d', '7d', '1mo'
    next_run_at         TIMESTAMP,

    -- Last execution
    last_run_at         TIMESTAMP,
    last_result_hash    TEXT,                           -- to detect changes
    last_result_summary TEXT,

    -- Notification
    notify_on_change    INTEGER DEFAULT 0,

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_sq_profile     ON saved_queries(profile_id);
CREATE INDEX idx_sq_next_run    ON saved_queries(next_run_at) WHERE auto_rerun = 1;

CREATE TRIGGER sq_updated_at AFTER UPDATE ON saved_queries BEGIN
    UPDATE saved_queries SET updated_at = CURRENT_TIMESTAMP WHERE query_id = new.query_id;
END;
```

---

## Query history (every NL query)

```sql
CREATE TABLE query_history (
    query_run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          INTEGER NOT NULL REFERENCES profiles(profile_id),
    saved_query_id      INTEGER REFERENCES saved_queries(query_id),  -- NULL if ad-hoc

    natural_language    TEXT NOT NULL,
    resolved_query      TEXT,
    resolution_method   TEXT,

    executed_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    duration_ms         INTEGER,
    success             INTEGER NOT NULL,
    error_message       TEXT,

    result_hash         TEXT,
    result_summary      TEXT,
    result_row_count    INTEGER,

    llm_model           TEXT,
    tokens_used         INTEGER
);

CREATE INDEX idx_qh_executed    ON query_history(executed_at);
CREATE INDEX idx_qh_saved       ON query_history(saved_query_id);
```

---

## Audit log

The single most important privacy table. Every operation on personal data, every external call.

```sql
CREATE TABLE audit_log (
    log_id              INTEGER PRIMARY KEY AUTOINCREMENT,

    timestamp           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    profile_id          INTEGER REFERENCES profiles(profile_id),

    -- Action
    action_type         TEXT NOT NULL CHECK (action_type IN
        ('read', 'write', 'update', 'delete', 'export',
         'llm_call', 'login', 'config_change', 'snapshot_create')),
    resource_type       TEXT NOT NULL,                  -- 'variant', 'insight', 'observation', etc.
    resource_id         TEXT,

    operation_details   TEXT,                           -- JSON

    -- Privacy: did this leave the device?
    external_call       INTEGER NOT NULL DEFAULT 0,
    external_endpoint   TEXT,                           -- 'myvariant.info', 'pubmed', 'topmed', 'anthropic'
    external_payload_hash TEXT,                         -- hash of what was sent (not the payload)

    -- LLM-specific
    llm_provider        TEXT,
    llm_model           TEXT,
    llm_tokens          INTEGER,

    -- Source
    user_agent          TEXT,
    session_id          TEXT
);

CREATE INDEX idx_audit_timestamp    ON audit_log(timestamp);
CREATE INDEX idx_audit_external     ON audit_log(external_call, timestamp);
CREATE INDEX idx_audit_resource     ON audit_log(resource_type, resource_id);
```

> **Important:** `external_payload_hash` is a hash, not the payload itself. We never store the data we sent — just enough to prove later that we did, and to what endpoint.

---

## Snapshots

```sql
CREATE TABLE snapshots (
    snapshot_id         TEXT PRIMARY KEY,                       -- UUID
    profile_id          INTEGER NOT NULL REFERENCES profiles(profile_id),

    name                TEXT NOT NULL,
    description         TEXT,
    taken_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Source versions captured at snapshot time (JSON)
    -- Mirror of DuckDB's annotation_source_versions at this moment
    annotation_versions TEXT NOT NULL,                          -- JSON

    -- Payload (large; stored on disk under /archive/snapshots/)
    payload_path        TEXT,                                   -- /archive/snapshots/<uuid>.json.zst
    payload_hash        TEXT,
    payload_size_bytes  INTEGER,

    -- Counts (denormalized for fast list view)
    insight_count       INTEGER,
    pgs_count           INTEGER,
    pgx_count           INTEGER,
    acmg_sf_count       INTEGER,

    triggered_by        TEXT,                                   -- 'manual', 'scheduled', 'pre_export'
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_snapshots_taken    ON snapshots(taken_at);
CREATE INDEX idx_snapshots_profile  ON snapshots(profile_id);
```

---

## Jobs (work queue)

```sql
CREATE TABLE jobs (
    job_id              INTEGER PRIMARY KEY AUTOINCREMENT,

    job_type            TEXT NOT NULL CHECK (job_type IN (
        'annotation_refresh',
        'imputation_upload', 'imputation_monitor', 'imputation_download',
        'pgs_recompute', 'pgx_recompute', 'carrier_recompute',
        'acmg_sf_recompute', 'hla_recompute', 'roh_recompute',
        'haplogroup_recompute', 'ancestry_recompute', 'genome_qc_recompute',
        'pubmed_enrichment',
        'snapshot_create',
        'export_generate',
        'index_refresh',
        'medication_pgx_check',
        'audit_purge'
    )),

    profile_id          INTEGER REFERENCES profiles(profile_id),
    parameters          TEXT,                                   -- JSON

    -- Status
    status              TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued', 'running', 'completed', 'failed', 'cancelled')),
    priority            INTEGER DEFAULT 5,                      -- 1 (highest) to 10 (lowest)

    -- Scheduling
    scheduled_for       TIMESTAMP,                              -- NULL = ASAP
    started_at          TIMESTAMP,
    completed_at        TIMESTAMP,
    duration_ms         INTEGER,

    -- Result / error
    result              TEXT,                                   -- JSON
    error_message       TEXT,
    error_details       TEXT,                                   -- stack trace
    retry_count         INTEGER DEFAULT 0,
    max_retries         INTEGER DEFAULT 3,

    -- Recurrence
    is_recurring        INTEGER DEFAULT 0,
    cron_expression     TEXT,                                   -- '0 2 * * 0' = weekly Sun 2am
    next_run_at         TIMESTAMP,
    parent_job_id       INTEGER REFERENCES jobs(job_id),        -- recurring instances

    -- Dependencies
    depends_on_job_ids  TEXT,                                   -- JSON array

    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_jobs_status_priority ON jobs(status, priority, scheduled_for);
CREATE INDEX idx_jobs_recurring       ON jobs(is_recurring, next_run_at) WHERE is_recurring = 1;
CREATE INDEX idx_jobs_type            ON jobs(job_type, status);
```

---

## User preferences

```sql
CREATE TABLE user_preferences (
    pref_key            TEXT PRIMARY KEY,
    pref_value          TEXT NOT NULL,
    value_type          TEXT NOT NULL CHECK (value_type IN ('string', 'number', 'boolean', 'json')),
    description         TEXT,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER prefs_updated_at AFTER UPDATE ON user_preferences BEGIN
    UPDATE user_preferences SET updated_at = CURRENT_TIMESTAMP WHERE pref_key = new.pref_key;
END;
```

### Suggested seed data (insert on first run)

| Key | Value | Type | Description |
|---|---|---|---|
| `current_profile_id` | `1` | number | Active profile |
| `default_audience` | `layperson` | string | Insight rendering: `eli5` / `layperson` / `clinical` |
| `imputation_r2_threshold` | `0.3` | number | Minimum R² to use imputed variants |
| `theme` | `system` | string | UI theme: `light` / `dark` / `system` |
| `llm_model` | `claude-opus-4-7` | string | Model for NL queries |
| `audit_retention_days` | `365` | number | How long to keep audit logs |
| `external_calls_enabled` | `true` | boolean | Master switch for any network egress |
| `pubmed_enrichment_enabled` | `false` | boolean | Auto-fetch PubMed for variants |
| `auto_snapshot_cadence` | `90d` | string | Auto snapshots every N days (`""` = off) |
| `prs_min_coverage_pct` | `80` | number | Hide PGS results below this coverage |
| `font_size` | `medium` | string | UI font size |
| `cite_in_responses` | `true` | boolean | Include citations in LLM-generated text |

---

## Profiles seed (insert on first run)

```sql
INSERT INTO profiles (profile_id, name, relationship, duckdb_path)
VALUES (1, 'Me', 'self', '/data/genome.duckdb');
```

---

## Convenience views

```sql
-- Active medications with PGx warning summary
CREATE VIEW active_medications_v AS
SELECT
    m.medication_id, m.profile_id, m.drug_name, m.rxnorm_id,
    m.dosage, m.frequency, m.indication,
    m.started_at,
    m.pgx_warning_count, m.pgx_last_checked
FROM medications m
WHERE m.is_active = 1
ORDER BY m.pgx_warning_count DESC, m.drug_name;

-- Pending jobs ordered by priority + schedule
CREATE VIEW pending_jobs_v AS
SELECT *
FROM jobs
WHERE status IN ('queued', 'running')
ORDER BY status DESC, priority ASC, COALESCE(scheduled_for, created_at) ASC;

-- Recent audit (last 30 days, external calls highlighted)
CREATE VIEW recent_audit_v AS
SELECT *
FROM audit_log
WHERE timestamp > datetime('now', '-30 days')
ORDER BY timestamp DESC;

-- External call summary (privacy dashboard)
CREATE VIEW external_call_summary_v AS
SELECT
    external_endpoint,
    COUNT(*) AS call_count,
    MAX(timestamp) AS last_call,
    MIN(timestamp) AS first_call
FROM audit_log
WHERE external_call = 1
GROUP BY external_endpoint
ORDER BY call_count DESC;

-- Observations with latest value per phenotype
CREATE VIEW latest_observations_v AS
SELECT
    op.phenotype_id, op.name, op.value_type, op.units,
    o.value_numeric, o.value_text, o.value_boolean,
    o.observed_at, o.matches_expectation
FROM observation_phenotypes op
LEFT JOIN observations o
    ON o.phenotype_id = op.phenotype_id
   AND o.observed_at = (
       SELECT MAX(observed_at) FROM observations
       WHERE phenotype_id = op.phenotype_id
   );
```

---

## Application-layer concerns

1. **SQLCipher initialization.** On every connection, set the passphrase via `PRAGMA key = '<passphrase>'` before any other statement. Wrap in a connection helper.

2. **Cross-DB integrity.** When `subject_id` references a DuckDB row, validate via DuckDB lookup before inserting. App-level only; no FK.

3. **Audit log retention.** A recurring `audit_purge` job deletes rows older than `audit_retention_days`. Default 365 days. Critical findings (external calls) can be exempted via app rule.

4. **Snapshot payloads** live in `/archive/snapshots/<uuid>.json.zst`. The SQLite row holds metadata + counts only; actual content is on disk. Snapshots can be deleted via cascade: delete the file, then the row.

5. **Job execution model.** A single worker process polls `pending_jobs_v`, runs jobs, updates status. Recurring jobs spawn child rows tied via `parent_job_id`. Failed jobs increment `retry_count` until `max_retries`, then move to `failed`.

6. **Profile separation.** Each profile has its own DuckDB at `profiles.duckdb_path`. The app holds a connection pool keyed by `profile_id`. Switching profiles closes one connection and opens another. v1 ships with one profile; the schema is ready for more.

7. **Updated_at triggers** are defined per-table; SQLite supports them natively (DuckDB does not).

8. **FTS sync.** The notes_fts triggers keep search current. Rebuild on schema change with `INSERT INTO notes_fts(notes_fts) VALUES ('rebuild');`.

---

## Schema status — all five groups complete

| Group | Database | Status |
|---|---|---|
| 1. Genotype data | DuckDB | ✓ |
| 2. Reference annotations | DuckDB | ✓ |
| 3. Derived analyses | DuckDB | ✓ |
| 4. Insights & evidence | DuckDB | ✓ |
| 5. App state | SQLite | ✓ |

All cross-group `ALTER TABLE` statements have been distributed across the relevant groups. Apply each group's "ALTER" section after the dependent groups exist.

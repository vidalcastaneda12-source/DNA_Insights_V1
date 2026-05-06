# DNA Insights

A local-first personal DNA insights application. Ingests 23andMe and Ancestry raw exports, merges them with TopMed imputation, joins against curated reference annotations (ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog, gnomAD, VEP), runs analytical pipelines (PRS, PGx, carrier screening, ACMG SF, HLA, ROH, haplogroups, ancestry), and surfaces results as a unified insights model with full evidence provenance.

All data stays on the device. Network egress is opt-in and audited.

## Architecture at a glance

- `genome.duckdb` — analytical store (variants, annotations, derived analyses, insights).
- `app.db` — encrypted SQLite (notes, bookmarks, medications, jobs, preferences, audit log).
- `archive/` — raw uploads, snapshots, source dumps.

The full design lives in `docs/schemas/` (five schema markdown documents) and `CLAUDE.md`. The phased build plan lives in `ROADMAP.md`.

## Quick start

```bash
# 1. Install dependencies (Python 3.12+)
uv sync

# 2. Configure environment
cp .env.example .env
$EDITOR .env   # set APP_DB_PASSPHRASE and any other required values

# 3. Initialize databases
genome init

# 4. Verify
genome status

# 5. Run tests
pytest
```

## Project layout

```
.
├── CLAUDE.md                    # persistent context for any AI session
├── ROADMAP.md                   # phased build plan
├── README.md
├── pyproject.toml
├── .env.example
├── data/                        # gitignored runtime DBs
├── archive/                     # gitignored uploads + snapshots
├── docs/schemas/                # source-of-truth schema docs
├── ddl/                         # SQL extracted from the schema docs
├── backend/
│   ├── src/genome/
│   │   ├── config.py
│   │   ├── db/                  # connection helpers + init_schema
│   │   ├── ingest/ annotate/ analyze/ insights/ jobs/ api/  # later phases
│   │   └── cli.py
│   └── tests/
└── frontend/                    # Next.js app (later phases)
```

## Development

```bash
# Lint
ruff check
ruff format --check

# Types
mypy --strict backend/src

# Tests
pytest
```

## Status

Phase 1 (foundation) — complete. See `ROADMAP.md` for what's next.

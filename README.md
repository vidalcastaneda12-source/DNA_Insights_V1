# DNA Insights

A local-first personal DNA insights application. Ingests 23andMe and Ancestry raw exports, merges them with TopMed imputation, joins against curated reference annotations (ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog, gnomAD, VEP), runs analytical pipelines (PRS, PGx, carrier screening, ACMG SF, HLA, ROH, haplogroups, ancestry), and surfaces results as a unified insights model with full evidence provenance.

All data stays on the device. Network egress is opt-in and audited.

## Prerequisites

The encrypted notes table (`notes_fts`) is an FTS5 virtual table. Most distro
packages of SQLCipher (including `libsqlcipher-dev` 4.5.6 on Ubuntu 24.04) ship
with FTS3/FTS4 but **without FTS5**, so the schema cannot be applied against
those builds. You must build SQLCipher from source with `--enable-fts5` and
then build `pysqlcipher3` against that custom library.

The exact commands used to bootstrap this checkout:

```bash
# 1. System build deps for SQLCipher (FTS5 requires Tcl + OpenSSL headers)
sudo apt-get install -y build-essential tcl-dev libssl-dev

# 2. Build SQLCipher 4.5.6 with FTS5 enabled
cd /tmp
wget https://github.com/sqlcipher/sqlcipher/archive/refs/tags/v4.5.6.tar.gz
tar xzf v4.5.6.tar.gz
cd sqlcipher-4.5.6
./configure \
  --prefix=/usr/local \
  --enable-tempstore=yes \
  --enable-fts5 \
  CFLAGS="-DSQLITE_HAS_CODEC -DSQLITE_ENABLE_FTS5" \
  LDFLAGS="-lcrypto"
make -j"$(nproc)"
sudo make install
sudo ldconfig

# 3. Confirm FTS5 is in the new build
echo "PRAGMA compile_options;" | /usr/local/bin/sqlcipher | grep ENABLE_FTS5
# expected: ENABLE_FTS5

# 4. Reinstall pysqlcipher3 against the custom SQLCipher
#    (uv sync alone will fail without these flags because pip/uv would otherwise
#    link against the system libsqlcipher that lacks FTS5)
uv venv --python 3.12 --clear .venv
uv pip install --python .venv/bin/python setuptools wheel
CFLAGS="-I/usr/local/include/sqlcipher -DSQLITE_HAS_CODEC" \
LDFLAGS="-L/usr/local/lib -Wl,-rpath,/usr/local/lib -lsqlcipher" \
uv pip install \
  --python .venv/bin/python \
  --no-binary :all: \
  --no-build-isolation \
  pysqlcipher3==1.2.0

# 5. Install the rest of the project
uv pip install --python .venv/bin/python -e ".[dev]"

# 6. Smoke-test FTS5 in pysqlcipher3
.venv/bin/python -c "from pysqlcipher3 import dbapi2 as s; \
  c = s.connect(':memory:'); c.execute(\"PRAGMA key = 'x';\"); \
  c.execute('CREATE VIRTUAL TABLE t USING fts5(a);'); print('FTS5 OK')"
```

If `genome init` ever fails inside the SQLite step with `no such module: fts5`,
the SQLCipher build the Python extension is linked against does not have FTS5.
**Do not "fix" this by removing the `notes_fts` virtual table from the schema** —
rebuild SQLCipher with `--enable-fts5` per the steps above instead.

### Lift-over engine

GRCh37 inputs (Ancestry, older 23andMe chips) are lifted to GRCh38 using the
[`liftover`](https://pypi.org/project/liftover/) Python package by default
(C++/CFFI-backed, ~10–50× faster than `pyliftover`). It's installed as a
regular runtime dependency via `uv sync`; no system tooling is required. Pass
a local UCSC chain file with `--chain-file` (auto-download is disabled per the
local-first privacy policy).

`bcftools` and its `+liftover` plugin are **optional** and used only if you
already have a working install — the project no longer requires them. If you
ever want to fall back to the pure-Python implementation, pass
`--liftover-engine pyliftover`.

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

Phases 1–4 complete (foundation, ingestion, merge & discrepancy detection,
local imputation via Beagle 5.5). Phase 5 (reference annotation loaders) is
in progress: sub-phases 5.0 (scaffold), 5.1a (PharmGKB), 5.1b (CPIC), and
5.2 (ClinVar) have shipped; 5.3 (GWAS Catalog) is next. See `ROADMAP.md`
for the full sub-phase breakdown and `CHANGELOG.md` for release-level detail.

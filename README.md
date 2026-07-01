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

### cyvcf2 must be built from source

`cyvcf2`'s prebuilt manylinux wheel bundles libcurl 7.29.0 built against
**NSS**, not OpenSSL. NSS-libcurl ignores `CURL_CA_BUNDLE` and looks for an
NSS database at `/etc/pki/nssdb` (CentOS layout), which doesn't exist on
Ubuntu. Opening a remote tabix URL (e.g. the gnomAD GCS bucket used by the
Phase 5.5 filtered-AF loader) fails with `Libcurl reported error 77 (Problem
with the SSL CA cert (path? access rights?))` no matter what env vars are
exported.

The fix is a source build of cyvcf2 so its bundled htslib links against the
system libcurl (OpenSSL backend). `pyproject.toml` pins this via
`[tool.uv] no-binary-package = ["cyvcf2"]`, so any `uv sync` rebuilds cyvcf2
from source automatically — but the system must have the build deps:

```bash
sudo apt-get install -y \
  build-essential autoconf \
  libcurl4-openssl-dev libssl-dev \
  libbz2-dev liblzma-dev libdeflate-dev zlib1g-dev
```

After `uv sync`, confirm the resulting `.so` links to the system OpenSSL stack
(no `cyvcf2.libs/` directory should exist):

```bash
ls .venv/lib/python3.12/site-packages/cyvcf2.libs 2>&1
# expected: No such file or directory

ldd .venv/lib/python3.12/site-packages/cyvcf2/cyvcf2.cpython-*.so \
  | grep -E 'libcurl|libssl|libcrypto'
# expected: paths under /usr/lib/x86_64-linux-gnu, NOT under cyvcf2.libs/
```

Then run the remote-tabix smoke test, with **no env vars set**, to confirm
TLS works against Google Cloud Storage:

```bash
env -u SSL_CERT_FILE -u CURL_CA_BUNDLE -u CERTIFI_BUNDLE \
  .venv/bin/python -c "
from cyvcf2 import VCF
url = 'https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/vcf/exomes/gnomad.exomes.v4.1.sites.chr22.vcf.bgz'
v = VCF(url)
print('header sample count:', len(v.samples))
for rec in v('chr22:10500000-12000000'):
    print('first record:', rec.CHROM, rec.POS, rec.REF, rec.ALT)
    break
print('OK')
"
# expected last line: OK
```

Symptoms of the broken-wheel state, for future debugging:
- `uv pip show cyvcf2` reports a wheel install (the source build produces an
  unsuffixed local wheel filename in the install log, e.g.
  `cyvcf2-0.32.1-cp312-cp312-linux_x86_64.whl`, not a `manylinux*` filename).
- `.venv/lib/python3.12/site-packages/cyvcf2.libs/` exists and contains
  `libnss3-*.so`, `libssl3-*.so`, `libcurl-*.so.4.3.0`. Those are NSS, not
  OpenSSL; their presence means the source-only pin in `pyproject.toml` was
  not honored (check the `[tool.uv]` block) or the build deps were missing
  and uv silently fell back to the wheel.

### Lift-over engine

GRCh37 inputs (Ancestry, older 23andMe chips) are lifted to GRCh38 using the
[`liftover`](https://pypi.org/project/liftover/) Python package by default
(C++/CFFI-backed, ~10–50× faster than `pyliftover`). It's installed as a
regular runtime dependency via `uv sync`; no system tooling is required. Pass
a local UCSC chain file with `--chain-file` (auto-download is disabled per the
local-first privacy policy).

For lift-over, `bcftools` and its `+liftover` plugin are **optional** and used
only if you already have a working install. If you ever want to fall back to the
pure-Python implementation, pass `--liftover-engine pyliftover`.

### bcftools (required for chrX imputation)

`bcftools` **is** required for the chrX imputation path (PR 5a / M3-physical):
`genome imputation panel prepare-chrx` splits the chrX reference panel into
PAR1 / non-PAR / PAR2 subsets with `bcftools view -r`, and the chrX run
concatenates the per-region outputs with `bcftools concat -a` (also using
`bgzip` and `awk` for the R1 re-diploidize seam). Install `bcftools` (≥ 1.x,
which ships `concat -a`) on PATH before running chrX. Autosomal imputation does
not need it.

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

# Decision-tracking gate (MEMORY.md ledger + finding frontmatter; finding-036)
genome docs check
```

`scripts/verify.sh` runs this full local protocol in one shot. To run the
decision-tracking gate automatically on every commit, install the tracked
pre-commit hook once per clone:

```bash
./scripts/install-hooks.sh   # runs `genome docs check` on commit; bypass with --no-verify
```

The gate also runs as the `docs-check` GitHub Action on every PR.

## Status

Phases 1–5 complete (foundation, ingestion, merge & discrepancy detection, local
imputation via Beagle 5.5, and the reference annotation loaders — Phase 5 closed with
the `variant_annotations_index` rollup, sub-phase 5.7). The project is now executing a
pre-Phase-6 cleanup sequence (PRs 1–11 landed; PR 7 closed-as-moot 2026-06-26 against the live DB; PR 12 next) that clears the dbSNP-dependent
backfills and the deferred-item backlog before the Phase 6 analysis pipelines begin.
Several `/scope-run` enhancement sub-projects (agentic verify-gate, fast-follow drain
loop, scope-split, cross-run-learning calibration, workflow-engine migration) have also
landed alongside it. See `ROADMAP.md` for the sub-phase / PR breakdown and `CHANGELOG.md`
for release-level detail.

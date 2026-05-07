# Group 1 — Genotype Data Schema

The user's actual DNA: every variant call from every source, the resolved consensus, and the discrepancy catalog that powers the platform-comparison view.

**Target:** DuckDB (`genome.duckdb`)

---

## Design principles

1. **Every source's call is preserved.** We do not collapse 23andMe + Ancestry + imputation at ingest. Each source's call lives in `genotype_calls` with full provenance; the resolved value lives in `consensus_genotypes`. This is what makes the discrepancy view and re-reconciliation possible.

2. **Surrogate keys for performance.** `variant_id` is `BIGINT` from a sequence, not UUID. With ~30M variants joined heavily across hot paths, the 8-byte vs 16-byte difference matters. rsID and `(chrom, pos, ref, alt)` are both indexed for lookup; `variant_id` is the join key everywhere else.

3. **Both builds, GRCh38 primary.** `pos_grch38` is canonical. `pos_grch37` is stored alongside for cross-source matching and external API compatibility. Lift-over status is recorded.

4. **Multi-allelic split.** Each row in `variants_master` is biallelic (one `ref`, one `alt`). Multi-allelic sites are split into multiple rows during ingestion. This matches VCF norms and how every annotation source represents variants.

5. **Imputed and genotyped variants share `variants_master`.** `is_imputed` and `imputation_r2` live on `genotype_calls` (the source-specific layer), not on master. Master tracks denormalized booleans `has_genotyped_call` / `has_imputed_call` for fast filtering.

6. **Discrepancies are first-class.** Disagreements between sources are catalogued in their own table with type, severity, and resolution. Not just QC noise — they're a core product feature.

---

## ENUMs

```sql
CREATE TYPE source_enum AS ENUM (
  '23andme',
  'ancestry',
  'topmed_imputed'
);

CREATE TYPE consensus_method_enum AS ENUM (
  'both_concordant',          -- both platforms agreed
  'single_source',             -- only one platform had a call
  'imputed_only',              -- only imputation produced a call
  'disagreement_resolved',     -- platforms disagreed; rule chose
  'unresolvable'               -- conflict could not be resolved; held as no-call
);

CREATE TYPE discrepancy_type_enum AS ENUM (
  'genotype_mismatch',         -- both called, different genotypes
  'strand_ambiguous',          -- A/T or C/G site; strand cannot be inferred
  'build_mismatch',            -- coordinate disagreement at lift-over
  'no_call_diff',              -- one platform called, the other didn't
  'platform_unique',           -- variant only present on one chip
  'multi_allelic_split'        -- one source biallelic, the other multi
);

CREATE TYPE severity_enum AS ENUM (
  'critical',   -- ACMG SF gene, conflicting result
  'major',      -- shared SNP genotype mismatch
  'minor',      -- platform-unique on a curated SNP
  'info'        -- platform-unique on a low-impact SNP
);

CREATE TYPE strand_status_enum AS ENUM (
  'resolved_plus',
  'resolved_minus',
  'flipped_to_match',
  'ambiguous_palindrome',      -- A/T or C/G with no flanking context
  'unknown'
);

CREATE TYPE chromosome_enum AS ENUM (
  '1','2','3','4','5','6','7','8','9','10',
  '11','12','13','14','15','16','17','18','19','20',
  '21','22','X','Y','MT'
);

CREATE TYPE variant_type_enum AS ENUM (
  'SNV', 'INDEL', 'MNV'
);

CREATE TYPE qc_status_enum AS ENUM (
  'pass', 'warn', 'fail'
);

CREATE TYPE ingestion_status_enum AS ENUM (
  'pending', 'processing', 'completed', 'failed'
);
```

---

## Sequence (for surrogate variant_id)

```sql
CREATE SEQUENCE variant_id_seq START 1;
```

---

## Core tables

### `variants_master`

One row per unique biallelic variant in the merged set.

```sql
CREATE TABLE variants_master (
  variant_id            BIGINT PRIMARY KEY DEFAULT nextval('variant_id_seq'),

  -- Identity
  rsid                  VARCHAR,                -- nullable; some imputed variants lack rsID
  chrom                 chromosome_enum NOT NULL,
  pos_grch38            BIGINT NOT NULL,        -- canonical
  pos_grch37            BIGINT,                 -- nullable; populated via lift-over
  ref_allele            VARCHAR NOT NULL,
  alt_allele            VARCHAR NOT NULL,
  variant_type          variant_type_enum NOT NULL DEFAULT 'SNV',

  -- Denormalized for fast filtering (maintained by app)
  has_genotyped_call    BOOLEAN DEFAULT FALSE,
  has_imputed_call      BOOLEAN DEFAULT FALSE,
  is_acmg_sf            BOOLEAN DEFAULT FALSE,  -- populated when group 2 lands

  -- Gene context (denormalized; full annotation lives in group 2)
  gene_symbols          VARCHAR[],

  -- Lift-over provenance
  liftover_chain        VARCHAR,                -- e.g. 'hg19_to_hg38'
  liftover_status       VARCHAR,                -- 'native_grch38', 'lifted_ok',
                                                -- 'lifted_with_warning', 'lift_failed'

  -- Audit
  created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  CONSTRAINT uq_variant_position UNIQUE (chrom, pos_grch38, ref_allele, alt_allele)
);

CREATE INDEX idx_vm_rsid          ON variants_master(rsid);
CREATE INDEX idx_vm_pos38         ON variants_master(chrom, pos_grch38);
CREATE INDEX idx_vm_pos37         ON variants_master(chrom, pos_grch37);
CREATE INDEX idx_vm_acmg_sf       ON variants_master(is_acmg_sf);
```

### `genotype_calls`

One active row per `(variant, source)`. Old calls preserved with `is_active = FALSE` for audit.

```sql
CREATE TABLE genotype_calls (
  call_id               BIGINT PRIMARY KEY,     -- assigned by app (or sequence)
  variant_id            BIGINT NOT NULL REFERENCES variants_master(variant_id),

  -- Source provenance
  source                source_enum NOT NULL,
  source_chip_version   VARCHAR,                -- e.g. '23andme_v5_GSA', 'ancestry_v2'
  ingestion_run_id      BIGINT NOT NULL,        -- FK to ingestion_runs

  -- The genotype
  genotype_raw          VARCHAR,                -- as reported, e.g. 'AG', 'AT', '--'
  allele_1              VARCHAR(20),            -- normalized to GRCh38 + strand
  allele_2              VARCHAR(20),
  is_no_call            BOOLEAN DEFAULT FALSE,

  -- Imputation context
  is_imputed            BOOLEAN DEFAULT FALSE,
  imputation_r2         DOUBLE,                 -- nullable; only if imputed
  imputation_panel      VARCHAR,                -- e.g. 'topmed_r3'

  -- Strand handling
  raw_strand            VARCHAR(10),            -- '+', '-', or 'unknown'
  strand_status         strand_status_enum NOT NULL DEFAULT 'unknown',

  -- Quality
  quality_flags         VARCHAR[],              -- e.g. ['low_confidence', 'het_outlier']

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,   -- one active per (variant, source)
  superseded_by         BIGINT,
  superseded_reason     VARCHAR,

  -- Audit
  ingested_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_gc_variant       ON genotype_calls(variant_id);
CREATE INDEX idx_gc_source        ON genotype_calls(source);
CREATE INDEX idx_gc_active        ON genotype_calls(variant_id, source, is_active);
CREATE INDEX idx_gc_ingestion     ON genotype_calls(ingestion_run_id);
CREATE INDEX idx_gc_imputed       ON genotype_calls(is_imputed, imputation_r2);
```

> **Application invariant:** at most one row per `(variant_id, source)` may have `is_active = TRUE`. Re-ingestion creates a new row and deactivates the prior one. Enforced in application code (DuckDB lacks partial unique indexes).

### `consensus_genotypes`

The resolved final call per variant. One row per variant.

```sql
CREATE TABLE consensus_genotypes (
  variant_id            BIGINT PRIMARY KEY REFERENCES variants_master(variant_id),

  -- The consensus call
  consensus_allele_1    VARCHAR(20),
  consensus_allele_2    VARCHAR(20),
  is_no_call            BOOLEAN DEFAULT FALSE,
  dosage                SMALLINT,               -- 0/1/2 for ALT count; NULL if no-call

  -- How we got here
  consensus_method      consensus_method_enum NOT NULL,
  is_imputed            BOOLEAN DEFAULT FALSE,  -- TRUE if consensus comes from imputation
  consensus_r2          DOUBLE,                 -- only when imputed
  contributing_calls    BIGINT[],               -- call_ids that fed into the consensus
  resolution_rule       VARCHAR NOT NULL,       -- e.g. 'consensus_v1'

  -- Confidence (0-1; rolled up from contributing calls)
  confidence            DECIMAL(3,2),

  -- Audit
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_cg_method        ON consensus_genotypes(consensus_method);
CREATE INDEX idx_cg_imputed       ON consensus_genotypes(is_imputed);
CREATE INDEX idx_cg_dosage        ON consensus_genotypes(dosage);
```

### `discrepancies`

Catalogued disagreements. Powers the discrepancy view.

```sql
CREATE TABLE discrepancies (
  discrepancy_id        BIGINT PRIMARY KEY,
  variant_id            BIGINT NOT NULL REFERENCES variants_master(variant_id),

  -- What kind
  discrepancy_type      discrepancy_type_enum NOT NULL,
  severity              severity_enum NOT NULL,

  -- The conflicting calls
  source_a              source_enum NOT NULL,
  call_a_id             BIGINT REFERENCES genotype_calls(call_id),
  genotype_a            VARCHAR,
  source_b              source_enum,
  call_b_id             BIGINT REFERENCES genotype_calls(call_id),
  genotype_b            VARCHAR,

  -- Resolution
  resolution            VARCHAR,                -- 'taken_from_a', 'taken_from_b',
                                                -- 'resolved_by_imputation', 'unresolved',
                                                -- 'flipped_strand_match'
  resolution_reason     TEXT,
  resolved_at           TIMESTAMP,

  -- Audit
  detected_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_disc_variant     ON discrepancies(variant_id);
CREATE INDEX idx_disc_type        ON discrepancies(discrepancy_type);
CREATE INDEX idx_disc_severity    ON discrepancies(severity);
CREATE INDEX idx_disc_unresolved  ON discrepancies(resolution);
```

### `ingestion_runs`

Every file upload event, for full file-level provenance.

```sql
CREATE TABLE ingestion_runs (
  run_id                BIGINT PRIMARY KEY,
  source                source_enum NOT NULL,
  source_chip_version   VARCHAR,

  -- File provenance
  file_path             VARCHAR NOT NULL,       -- under /archive/
  file_hash_sha256      VARCHAR(64) NOT NULL,
  file_size_bytes       BIGINT,
  file_native_build     VARCHAR,                -- e.g. 'GRCh37' from header

  -- Counts
  variants_total        INTEGER,
  variants_called       INTEGER,
  variants_no_call      INTEGER,
  variants_imputed      INTEGER,                -- 0 for raw 23andMe/Ancestry uploads
  variants_dropped_non_canonical INTEGER DEFAULT 0, -- variants on non-canonical GRCh38
                                                -- contigs (alt e.g. 8_KI270821v1_alt;
                                                -- random e.g. 4_GL000008v2_random;
                                                -- unplaced Un_*/chrUn_*; *_decoy)
                                                -- filtered at parse time; not in
                                                -- chromosome_enum

  -- Status
  status                ingestion_status_enum NOT NULL DEFAULT 'pending',
  error_log             TEXT,

  -- Versioning
  pipeline_version      VARCHAR NOT NULL,       -- e.g. 'pipeline_v0.3.1'

  -- Audit
  started_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  completed_at          TIMESTAMP
);

CREATE INDEX idx_ir_source        ON ingestion_runs(source);
CREATE INDEX idx_ir_status        ON ingestion_runs(status);
CREATE INDEX idx_ir_hash          ON ingestion_runs(file_hash_sha256);
```

### `imputation_runs`

TopMed (or other server) round-trips.

```sql
CREATE TABLE imputation_runs (
  imputation_id         BIGINT PRIMARY KEY,
  input_run_ids         BIGINT[],               -- which ingestion_runs were merged in

  imputation_server     VARCHAR NOT NULL,       -- 'topmed_r3', 'michigan_hrc', etc.
  reference_panel       VARCHAR,
  submitted_at          TIMESTAMP,
  completed_at          TIMESTAMP,
  status                ingestion_status_enum NOT NULL DEFAULT 'pending',

  -- Volumes
  variants_input        INTEGER,
  variants_output       INTEGER,
  mean_r2               DOUBLE,
  variants_above_r2_0_3 INTEGER,
  variants_above_r2_0_8 INTEGER,

  -- File provenance
  output_file_path      VARCHAR,
  output_file_hash_sha256 VARCHAR(64),

  pipeline_version      VARCHAR NOT NULL
);

CREATE INDEX idx_imp_status       ON imputation_runs(status);
```

### `sample_qc`

Per-ingestion QC dashboard data.

```sql
CREATE TABLE sample_qc (
  qc_id                 BIGINT PRIMARY KEY,
  run_id                BIGINT NOT NULL REFERENCES ingestion_runs(run_id),

  -- Standard sample-level QC
  call_rate             DECIMAL(5,4),           -- fraction of variants with a call
  heterozygosity_rate   DECIMAL(5,4),
  het_outlier           BOOLEAN,                -- > 3 SD from population mean

  -- Sex check
  sex_inferred          VARCHAR(10),            -- 'M', 'F', 'ambiguous'
  sex_expected          VARCHAR(10),            -- user-declared, optional
  sex_check_passed      BOOLEAN,
  chr_x_het_rate        DECIMAL(5,4),

  -- Imputation quality (when applicable)
  mean_imputation_r2    DOUBLE,
  low_r2_count          INTEGER,                -- variants below threshold

  -- Concordance with prior runs (when applicable)
  prior_run_id          BIGINT REFERENCES ingestion_runs(run_id),
  concordance_rate      DECIMAL(5,4),           -- vs prior_run_id

  -- Status
  qc_status             qc_status_enum NOT NULL,
  qc_notes              TEXT,

  computed_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_qc_run           ON sample_qc(run_id);
CREATE INDEX idx_qc_status        ON sample_qc(qc_status);
```

---

## Views

### Discrepancy dashboard

```sql
-- Cross-platform concordance summary (top of the discrepancy page)
CREATE VIEW concordance_summary_v AS
WITH paired AS (
  SELECT
    g1.variant_id,
    g1.source AS source_a,
    g2.source AS source_b,
    g1.allele_1 = g2.allele_1 AND g1.allele_2 = g2.allele_2 AS concordant
  FROM genotype_calls g1
  JOIN genotype_calls g2
    ON g1.variant_id = g2.variant_id
   AND g1.source < g2.source
   AND g1.is_active AND g2.is_active
   AND NOT g1.is_no_call AND NOT g2.is_no_call
)
SELECT
  source_a, source_b,
  COUNT(*)                                           AS shared_calls,
  COUNT(*) FILTER (WHERE concordant)                 AS concordant_calls,
  COUNT(*) FILTER (WHERE NOT concordant)             AS discordant_calls,
  CAST(COUNT(*) FILTER (WHERE concordant) AS DOUBLE)
    / NULLIF(COUNT(*), 0)                            AS concordance_rate
FROM paired
GROUP BY source_a, source_b;

-- Per-platform variant counts (Venn diagram data)
CREATE VIEW platform_coverage_v AS
SELECT
  vm.variant_id,
  vm.rsid,
  vm.chrom,
  BOOL_OR(gc.source = '23andme'       AND gc.is_active) AS in_23andme,
  BOOL_OR(gc.source = 'ancestry'      AND gc.is_active) AS in_ancestry,
  BOOL_OR(gc.source = 'topmed_imputed' AND gc.is_active) AS in_imputed
FROM variants_master vm
LEFT JOIN genotype_calls gc ON gc.variant_id = vm.variant_id
GROUP BY vm.variant_id, vm.rsid, vm.chrom;

-- Detailed per-variant call comparison (the discrepancy detail row)
CREATE VIEW call_comparison_v AS
SELECT
  vm.variant_id,
  vm.rsid,
  vm.chrom,
  vm.pos_grch38,
  vm.ref_allele,
  vm.alt_allele,
  MAX(CASE WHEN gc.source = '23andme'       THEN gc.allele_1 || '/' || gc.allele_2 END) AS gt_23andme,
  MAX(CASE WHEN gc.source = 'ancestry'      THEN gc.allele_1 || '/' || gc.allele_2 END) AS gt_ancestry,
  MAX(CASE WHEN gc.source = 'topmed_imputed' THEN gc.allele_1 || '/' || gc.allele_2 END) AS gt_imputed,
  MAX(CASE WHEN gc.source = 'topmed_imputed' THEN gc.imputation_r2 END)                  AS imputed_r2,
  cg.consensus_allele_1 || '/' || cg.consensus_allele_2                                  AS consensus,
  cg.consensus_method
FROM variants_master vm
LEFT JOIN genotype_calls gc       ON gc.variant_id = vm.variant_id AND gc.is_active
LEFT JOIN consensus_genotypes cg  ON cg.variant_id = vm.variant_id
GROUP BY vm.variant_id, vm.rsid, vm.chrom, vm.pos_grch38, vm.ref_allele,
         vm.alt_allele, cg.consensus_allele_1, cg.consensus_allele_2, cg.consensus_method;
```

---

## Variant matching strategy (how merge actually works)

Three lookup keys, applied in order:

1. **Primary key:** `(chrom, pos_grch38, ref_allele, alt_allele)`. After lift-over, this is the universal key. If both sources match here, they are the same variant.

2. **Secondary key:** `rsid`. Useful when one source's lift-over fails or when positions differ slightly. dbSNP rsID merges/withdrawals are resolved via `variant_aliases` (group 2).

3. **Tertiary fuzzy:** `(chrom, pos_grch38)` only — for cases where alleles are reported on different strands. A/T and C/G palindromes here trigger `strand_ambiguous` discrepancies; non-palindromic mismatches trigger a strand flip if alleles match the complement.

---

## Discrepancy detection rules

Run during merge, populating `discrepancies`:

| Rule | Type | Severity |
|---|---|---|
| Both platforms call, alleles differ (after strand resolution) | `genotype_mismatch` | `major` |
| Site is A/T or C/G, alleles match neither strand cleanly | `strand_ambiguous` | `minor` |
| Lift-over disagrees between platforms | `build_mismatch` | `major` |
| One platform calls, other reports `--` | `no_call_diff` | `minor` |
| Variant present on only one chip | `platform_unique` | `info` |
| One platform reports biallelic, other multi-allelic | `multi_allelic_split` | `minor` |

Severity escalates to `critical` for any of the above when the variant is in an ACMG SF gene (resolved via `variants_master.is_acmg_sf` once group 2 lands).

---

## Application-layer concerns (not in DDL)

1. **Sequences for `call_id`, `discrepancy_id`, `run_id`, `imputation_id`, `qc_id`.** Add `CREATE SEQUENCE` statements for each, paralleling `variant_id_seq`.

2. **`(variant_id, source)` active-uniqueness** is application-enforced via INSERT-then-deactivate, since DuckDB lacks partial unique indexes.

3. **Consensus recomputation** is triggered whenever `genotype_calls` changes. Versioned via `resolution_rule` so old consensus values can be re-derived from history.

4. **Denormalized booleans** (`has_genotyped_call`, `has_imputed_call` on `variants_master`) are maintained by app logic on call insert/deactivate.

5. **Sex check / phenotype expectations** require user input or family data; sex_expected can be NULL until provided.

6. **Non-canonical contig filtering at parse time.** 23andMe v5 and some AncestryDNA
   exports include variants on non-canonical GRCh38 contigs: alt contigs (`*_alt`, e.g.
   `8_KI270821v1_alt`), unlocalized contigs (`*_random`, e.g. `4_GL000008v2_random`),
   unplaced contigs (`Un_*` and `chrUn_*`, e.g. `Un_GL000226v1`), and decoy sequences
   (`*_decoy`). These chromosome labels are not members of `chromosome_enum` (the enum
   covers `1..22, X, Y, MT` only). The parser uses a positive-rule filter — only labels
   that resolve to the canonical set after the `23/24/25/26` alias remap are kept — drops
   the rest before the row reaches DuckDB, logs each drop at `debug` level with its chrom
   value, emits a single `info` summary at end-of-parse, and the per-run total lands in
   `ingestion_runs.variants_dropped_non_canonical`. This is intentional and matches
   standard clinical bioinformatics practice — non-canonical contigs are excluded from the
   canonical reference space used for variant calling and annotation.

---

## Cross-group references — application-validated

DuckDB does not support `ALTER TABLE ... ADD CONSTRAINT` for foreign keys, so cross-group
linkage is validated in application code rather than enforced by the database — consistent
with the SQLite (group 5) pattern where cross-DB references are also application-validated:

- `insight_variants.variant_id` → `variants_master.variant_id` (group 4 → group 1)
- `variants_master.is_acmg_sf` is populated by ETL via a join to `genes.is_acmg_sf` once
  group 2 is loaded.
- The `insights.subject_id` polymorphic reference is application-validated; no DB-level FK.

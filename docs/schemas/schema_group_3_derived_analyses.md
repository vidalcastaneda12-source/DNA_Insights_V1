# Group 3 — Derived Analyses Schema

The structured outputs of analysis pipelines: PGS scores, PGx star alleles, carrier findings, ACMG SF hits, HLA typing, ROH, haplogroups, ancestry, archaic introgression, compound het, and genome-wide QC. These tables are what insight-generation logic in group 4 reads from.

**Target:** DuckDB (`genome.duckdb`)

---

## Design principles

1. **Every derived row points to an `analysis_run`.** Same provenance pattern as `ingestion_runs` in group 1. Captures method, version, parameters, source-DB versions used, and timing.

2. **Supersession over update.** When a pipeline re-runs (new PharmCAT version, fresh ClinVar), insert new rows and deactivate the old. Never overwrite. Identical to the pattern from groups 1 and 4.

3. **Method version on every result.** `method` + `method_version` on every derived row. Lets you tell that one PGx call came from PharmCAT 2.13 and a re-run came from 2.14.

4. **Source-version snapshot.** `analysis_runs.source_versions_used` (JSON) captures which version of every consumed source (ClinVar, PharmGKB, PGS Catalog, etc.) was active at run time. This is what makes snapshot reproducibility (group 5) actually work.

5. **Confidence as numeric, quality as enum.** Most derived rows have both `confidence` (0.00–1.00) and a coarser `call_quality` enum (high/moderate/low) for fast filtering.

---

## Process tracking

### `analysis_runs`

```sql
CREATE TABLE analysis_runs (
  analysis_run_id       BIGINT PRIMARY KEY,
  analysis_type         VARCHAR NOT NULL,        -- 'pgs', 'pgx', 'carrier', 'acmg_sf',
                                                 -- 'hla', 'roh', 'haplogroup',
                                                 -- 'global_ancestry', 'local_ancestry',
                                                 -- 'archaic_ancestry', 'compound_het',
                                                 -- 'genome_qc'
  method                VARCHAR NOT NULL,        -- 'pharmcat', 'hibag', 'plink_roh',
                                                 -- 'rfmix_v2', 'haplogrep', 'sprime', etc.
  method_version        VARCHAR NOT NULL,

  -- Inputs
  input_run_ids         BIGINT[],                -- ingestion_runs / imputation_runs consumed
  input_variant_count   INTEGER,
  parameters            JSON,                    -- method-specific config

  -- Status
  status                ingestion_status_enum NOT NULL DEFAULT 'pending',
                                                 -- (reuses enum from group 1)
  started_at            TIMESTAMP,
  completed_at          TIMESTAMP,
  duration_seconds      INTEGER,
  error_log             TEXT,

  -- Output
  output_count          INTEGER,                 -- rows produced

  -- Reproducibility
  source_versions_used  JSON,                    -- {clinvar: '...', pharmgkb: '...', ...}
  pipeline_version      VARCHAR NOT NULL
);

CREATE INDEX idx_ar_type    ON analysis_runs(analysis_type);
CREATE INDEX idx_ar_status  ON analysis_runs(status);
```

### Common ENUM additions

```sql
CREATE TYPE call_quality_enum AS ENUM ('high', 'moderate', 'low');

CREATE TYPE pgx_phenotype_enum AS ENUM (
  'PM',     -- poor metabolizer
  'IM',     -- intermediate metabolizer
  'NM',     -- normal metabolizer
  'RM',     -- rapid metabolizer
  'UM',     -- ultrarapid metabolizer
  'IND'     -- indeterminate
);

CREATE TYPE carrier_status_enum AS ENUM (
  'carrier',           -- one P/LP allele in AR gene
  'likely_carrier',    -- one LP/VUS-leaning-P allele
  'affected',          -- two P/LP alleles
  'compound_het',      -- two different P/LP alleles in trans
  'clear',             -- no P/LP alleles found
  'inconclusive'       -- insufficient evidence
);

CREATE TYPE haplogroup_type_enum AS ENUM ('Y', 'mtDNA');

CREATE TYPE archaic_source_enum AS ENUM ('neanderthal', 'denisovan');
```

---

## Core derived tables

### `derived_pgs` — polygenic risk scores

```sql
CREATE TABLE derived_pgs (
  derived_pgs_id        BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),
  pgs_id                VARCHAR NOT NULL REFERENCES pgs_catalog_scores(pgs_id),

  -- Score
  raw_score             DOUBLE NOT NULL,

  -- Reference distribution
  percentile            DECIMAL(5,2),            -- 0.00–100.00
  z_score               DOUBLE,
  reference_population  VARCHAR,                 -- which distribution we compared against

  -- Coverage
  variants_used         INTEGER,
  variants_missing      INTEGER,
  coverage_pct          DECIMAL(5,2),
  variants_imputed      INTEGER,
  mean_imputation_r2    DOUBLE,

  -- Quality flags
  low_coverage          BOOLEAN,                 -- coverage < 80%
  high_imputation_share BOOLEAN,                 -- > 50% from imputation
  ancestry_mismatch     BOOLEAN,                 -- score's ancestry differs from user's

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  superseded_by         BIGINT,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_dpgs_pgs     ON derived_pgs(pgs_id);
CREATE INDEX idx_dpgs_active  ON derived_pgs(is_active);
CREATE INDEX idx_dpgs_high    ON derived_pgs(percentile);   -- extremes filtered at query time
```

### `derived_pgx_phenotypes` — pharmacogenomic calls

```sql
CREATE TABLE derived_pgx_phenotypes (
  derived_pgx_id        BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  -- Gene
  gene_symbol           VARCHAR NOT NULL REFERENCES genes(gene_symbol),

  -- Diplotype
  haplotype_1           VARCHAR NOT NULL,        -- e.g. '*1'
  haplotype_2           VARCHAR NOT NULL,        -- e.g. '*4'
  diplotype             VARCHAR,                 -- e.g. 'CYP2D6 *1/*4'

  -- Phenotype
  phenotype             VARCHAR,                 -- e.g. 'CYP2D6 Intermediate Metabolizer'
  phenotype_category    pgx_phenotype_enum,
  activity_score        DOUBLE,                  -- numeric, where applicable

  -- Confidence
  confidence            DECIMAL(3,2),
  call_quality          call_quality_enum,
  ambiguous_calls       BOOLEAN,
  alternative_diplotypes JSON,                   -- alternates if ambiguous

  -- Coverage
  variants_used         INTEGER,
  variants_missing      INTEGER,

  -- Method
  method                VARCHAR NOT NULL,        -- 'pharmcat'
  method_version        VARCHAR NOT NULL,

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  superseded_by         BIGINT,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_pgx_gene       ON derived_pgx_phenotypes(gene_symbol);
CREATE INDEX idx_pgx_phenotype  ON derived_pgx_phenotypes(phenotype_category);
CREATE INDEX idx_pgx_active     ON derived_pgx_phenotypes(is_active);
```

### `derived_carrier_findings`

```sql
CREATE TABLE derived_carrier_findings (
  derived_carrier_id    BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  -- Gene / condition
  gene_symbol           VARCHAR NOT NULL REFERENCES genes(gene_symbol),
  condition_name        VARCHAR NOT NULL,
  condition_id          VARCHAR,                 -- OMIM / MONDO ID
  inheritance           VARCHAR NOT NULL,        -- 'AR', 'XL', 'mitochondrial'

  -- Finding
  carrier_status        carrier_status_enum NOT NULL,
  variant_ids           BIGINT[],                -- which variants drove the call
  zygosity              VARCHAR,                 -- 'het', 'hom', 'compound_het'

  -- Confidence
  confidence            DECIMAL(3,2),
  variants_with_p_lp    INTEGER,                 -- count of P/LP variants found

  -- Reproductive risk (when partner data exists)
  partner_carrier_status carrier_status_enum,
  child_risk_pct        DOUBLE,

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  superseded_by         BIGINT,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_carrier_gene    ON derived_carrier_findings(gene_symbol);
CREATE INDEX idx_carrier_status  ON derived_carrier_findings(carrier_status);
CREATE INDEX idx_carrier_active  ON derived_carrier_findings(is_active);
```

### `derived_acmg_sf_findings`

```sql
CREATE TABLE derived_acmg_sf_findings (
  derived_acmg_id       BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  -- Gene context
  gene_symbol           VARCHAR NOT NULL REFERENCES genes(gene_symbol),
  acmg_sf_version       VARCHAR NOT NULL,        -- 'v3.2'
  disease               VARCHAR NOT NULL,
  inheritance           VARCHAR,

  -- Variant
  variant_id            BIGINT NOT NULL REFERENCES variants_master(variant_id),
  zygosity              VARCHAR,
  clinical_significance VARCHAR,                 -- from ClinVar
  hgvs_c                VARCHAR,
  hgvs_p                VARCHAR,

  -- Action
  recommended_action    TEXT,
  resource_url          VARCHAR,                 -- NIH/ACMG guidance link
  estimated_penetrance  VARCHAR,                 -- 'high', 'moderate', 'reduced', 'unknown'

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  superseded_by         BIGINT,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_acmg_gene    ON derived_acmg_sf_findings(gene_symbol);
CREATE INDEX idx_acmg_active  ON derived_acmg_sf_findings(is_active);
```

### `derived_hla_typing`

```sql
CREATE TABLE derived_hla_typing (
  derived_hla_id        BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  -- Locus
  locus                 VARCHAR NOT NULL,        -- 'HLA-A', 'HLA-B', 'HLA-C',
                                                 -- 'HLA-DRB1', 'HLA-DQB1', etc.

  -- Alleles
  allele_1              VARCHAR NOT NULL,        -- e.g. 'A*02:01'
  allele_2              VARCHAR NOT NULL,
  resolution            VARCHAR,                 -- '2-digit', '4-digit', 'G-group'

  -- Confidence (HIBAG)
  posterior_probability DOUBLE,
  call_quality          call_quality_enum,

  -- Method
  method                VARCHAR NOT NULL,        -- 'hibag'
  method_version        VARCHAR NOT NULL,
  reference_population  VARCHAR,                 -- HIBAG model used

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  superseded_by         BIGINT,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_hla_locus   ON derived_hla_typing(locus);
CREATE INDEX idx_hla_active  ON derived_hla_typing(is_active);
```

### `derived_roh` — runs of homozygosity (segment-level)

```sql
CREATE TABLE derived_roh (
  derived_roh_id        BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  -- Location
  chrom                 chromosome_enum NOT NULL,
  start_grch38          BIGINT NOT NULL,
  end_grch38            BIGINT NOT NULL,
  length_bp             BIGINT,
  length_cm             DOUBLE,

  -- Composition
  variant_count         INTEGER,
  homozygous_count      INTEGER,
  heterozygous_count    INTEGER,                 -- allowed exceptions

  -- Classification
  roh_class             VARCHAR,                 -- 'short' (<1Mb), 'medium' (1–5Mb),
                                                 -- 'long' (>5Mb)
  -- Method
  method                VARCHAR NOT NULL,        -- 'plink_roh'
  method_version        VARCHAR NOT NULL,

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_roh_chrom   ON derived_roh(chrom, start_grch38);
CREATE INDEX idx_roh_active  ON derived_roh(is_active);
```

### `derived_haplogroups` — Y and mtDNA

```sql
CREATE TABLE derived_haplogroups (
  derived_haplogroup_id BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  haplogroup_type       haplogroup_type_enum NOT NULL,
  haplogroup            VARCHAR NOT NULL,        -- 'R1b1a2a1a2c1', 'H1a1', etc.
  haplogroup_short      VARCHAR,                 -- top-level: 'R1b', 'H'

  -- Branch path
  ancestral_path        VARCHAR[],               -- ['R', 'R1', 'R1b', ...]

  -- Confidence
  confidence            DECIMAL(3,2),
  call_quality          call_quality_enum,
  defining_markers      VARCHAR[],

  -- Geographic / temporal context (optional, populated from haplogroup tree DB)
  estimated_origin_region VARCHAR,
  estimated_age_kya     INTEGER,

  -- Method
  method                VARCHAR NOT NULL,        -- 'yfull', 'haplogrep3'
  method_version        VARCHAR NOT NULL,

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_hap_type    ON derived_haplogroups(haplogroup_type);
CREATE INDEX idx_hap_active  ON derived_haplogroups(is_active);
```

---

## Ancestry analyses

### `derived_global_ancestry` — admixture component proportions

```sql
CREATE TABLE derived_global_ancestry (
  derived_ancestry_id   BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  -- Component
  population            VARCHAR NOT NULL,        -- 'Northwestern European', 'East Asian'
  population_level      VARCHAR NOT NULL,        -- 'continental', 'subcontinental', 'fine'
  fraction              DECIMAL(6,5),            -- 0.00000–1.00000
  ci_low                DECIMAL(6,5),
  ci_high               DECIMAL(6,5),

  -- Method
  method                VARCHAR NOT NULL,        -- 'admixture', 'rfmix', 'flare'
  method_version        VARCHAR NOT NULL,
  reference_panel       VARCHAR,                 -- '1000G', 'HGDP', etc.

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ga_active ON derived_global_ancestry(is_active);
```

### `derived_local_ancestry` — chromosome painting

```sql
CREATE TABLE derived_local_ancestry (
  derived_local_id      BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  -- Segment
  chrom                 chromosome_enum NOT NULL,
  start_grch38          BIGINT NOT NULL,
  end_grch38            BIGINT NOT NULL,
  haplotype             SMALLINT,                -- 1 or 2 (which diploid copy)

  -- Assignment
  assigned_population   VARCHAR NOT NULL,
  posterior_probability DOUBLE,

  -- Method
  method                VARCHAR NOT NULL,        -- 'rfmix_v2', 'flare'
  method_version        VARCHAR NOT NULL,

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_la_pos     ON derived_local_ancestry(chrom, start_grch38);
CREATE INDEX idx_la_active  ON derived_local_ancestry(is_active);
```

### `derived_archaic_ancestry` — Neanderthal / Denisovan

```sql
CREATE TABLE derived_archaic_ancestry (
  derived_archaic_id    BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  archaic_source        archaic_source_enum NOT NULL,

  -- Summary
  total_introgression_pct DECIMAL(6,5),
  segment_count         INTEGER,
  total_segment_length_mb DOUBLE,

  -- Detail
  segments              JSON,                    -- [{chrom, start, end, score}, ...]
                                                 -- kept as JSON because it's read whole

  -- Method
  method                VARCHAR NOT NULL,        -- 'sprime', 'ibdmix'
  method_version        VARCHAR NOT NULL,

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_arch_source ON derived_archaic_ancestry(archaic_source);
```

### `derived_genetic_distance` — distance to reference populations

```sql
CREATE TABLE derived_genetic_distance (
  derived_distance_id   BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  reference_population  VARCHAR NOT NULL,
  population_level      VARCHAR,                 -- 'continental', 'subcontinental', 'fine'
  fst_distance          DOUBLE,
  pca_distance          DOUBLE,
  rank                  INTEGER,                 -- among all reference pops

  is_active             BOOLEAN DEFAULT TRUE,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_gd_population ON derived_genetic_distance(reference_population);
```

---

## Other analyses

### `derived_compound_het`

```sql
CREATE TABLE derived_compound_het (
  derived_ch_id         BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  gene_symbol           VARCHAR NOT NULL REFERENCES genes(gene_symbol),
  variant_id_1          BIGINT NOT NULL REFERENCES variants_master(variant_id),
  variant_id_2          BIGINT NOT NULL REFERENCES variants_master(variant_id),

  -- Phasing
  in_trans              BOOLEAN,                 -- TRUE if on different haplotypes
  phasing_confidence    DECIMAL(3,2),
  phasing_method        VARCHAR,                 -- 'shapeit', 'family_inference', 'unphased'

  -- Predicted impact
  combined_significance VARCHAR,                 -- combined ClinVar significance
  predicted_impact      VARCHAR,                 -- 'biallelic_loss', 'partial_loss'

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  computed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ch_gene    ON derived_compound_het(gene_symbol);
CREATE INDEX idx_ch_active  ON derived_compound_het(is_active);
```

### `derived_genome_qc` — genome-wide QC summary

Distinct from `sample_qc` (group 1, per-ingestion). This is computed across the merged + imputed call set.

```sql
CREATE TABLE derived_genome_qc (
  derived_qc_id         BIGINT PRIMARY KEY,
  analysis_run_id       BIGINT NOT NULL REFERENCES analysis_runs(analysis_run_id),

  -- Diversity
  heterozygosity_rate     DECIMAL(6,5),
  inbreeding_coefficient_f DOUBLE,

  -- ROH summary
  roh_total_length_mb     DOUBLE,
  roh_segment_count       INTEGER,
  longest_roh_mb          DOUBLE,

  -- Coverage
  total_variants          INTEGER,
  genotyped_variants      INTEGER,
  imputed_variants        INTEGER,
  no_call_variants        INTEGER,

  -- Quality flags
  excess_heterozygosity   BOOLEAN,
  excess_homozygosity     BOOLEAN,
  high_no_call_rate       BOOLEAN,

  -- Lifecycle
  is_active               BOOLEAN DEFAULT TRUE,
  computed_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_dqc_active ON derived_genome_qc(is_active);
```

---

## Views

```sql
-- Active PGx phenotypes joined with affected drugs (powers the medication checker)
CREATE VIEW pgx_phenotype_drugs_v AS
SELECT
  p.derived_pgx_id,
  p.gene_symbol,
  p.diplotype,
  p.phenotype,
  p.phenotype_category,
  p.confidence,
  ARRAY_AGG(DISTINCT cg.drug_name) AS affected_drugs,
  ARRAY_AGG(DISTINCT cg.recommendation)
    FILTER (WHERE cg.cpic_level IN ('A', 'B'))
    AS strong_recommendations
FROM derived_pgx_phenotypes p
LEFT JOIN cpic_guidelines cg
  ON cg.gene_symbol = p.gene_symbol
 AND cg.phenotype LIKE p.phenotype || '%'
 AND cg.is_active
WHERE p.is_active
GROUP BY p.derived_pgx_id, p.gene_symbol, p.diplotype,
         p.phenotype, p.phenotype_category, p.confidence;

-- Active ACMG SF findings with full clinical context (clinician PDF source)
CREATE VIEW acmg_sf_active_v AS
SELECT
  a.*,
  vm.rsid, vm.chrom, vm.pos_grch38, vm.ref_allele, vm.alt_allele,
  cg.consensus_allele_1, cg.consensus_allele_2, cg.dosage,
  vai.clinvar_star_rating
FROM derived_acmg_sf_findings a
JOIN variants_master vm ON vm.variant_id = a.variant_id
LEFT JOIN consensus_genotypes cg ON cg.variant_id = vm.variant_id
LEFT JOIN variant_annotations_index vai ON vai.variant_id = vm.variant_id
WHERE a.is_active;

-- High-percentile PGS dashboard (top/bottom 10%)
CREATE VIEW pgs_extremes_v AS
SELECT
  p.derived_pgs_id, p.pgs_id, s.trait_reported, s.trait_category,
  p.percentile, p.z_score, p.coverage_pct, p.low_coverage,
  CASE
    WHEN p.percentile >= 90 THEN 'high_risk'
    WHEN p.percentile <= 10 THEN 'low_risk'
  END AS bucket
FROM derived_pgs p
JOIN pgs_catalog_scores s ON s.pgs_id = p.pgs_id
WHERE p.is_active
  AND (p.percentile >= 90 OR p.percentile <= 10)
  AND NOT p.low_coverage;

-- Carrier panel — every active carrier finding by gene
CREATE VIEW carrier_panel_v AS
SELECT
  c.gene_symbol,
  c.condition_name,
  c.inheritance,
  c.carrier_status,
  c.zygosity,
  c.confidence,
  c.partner_carrier_status,
  c.child_risk_pct,
  c.computed_at
FROM derived_carrier_findings c
WHERE c.is_active
ORDER BY c.gene_symbol;

-- Cross-derivation summary count for the home dashboard
CREATE VIEW derived_summary_v AS
SELECT
  'pgs'           AS analysis_type, COUNT(*) AS active_count FROM derived_pgs           WHERE is_active
UNION ALL SELECT 'pgx',           COUNT(*) FROM derived_pgx_phenotypes  WHERE is_active
UNION ALL SELECT 'carrier',       COUNT(*) FROM derived_carrier_findings WHERE is_active
UNION ALL SELECT 'acmg_sf',       COUNT(*) FROM derived_acmg_sf_findings WHERE is_active
UNION ALL SELECT 'hla',           COUNT(*) FROM derived_hla_typing       WHERE is_active
UNION ALL SELECT 'roh',           COUNT(*) FROM derived_roh              WHERE is_active
UNION ALL SELECT 'haplogroup',    COUNT(*) FROM derived_haplogroups      WHERE is_active
UNION ALL SELECT 'global_ancestry', COUNT(*) FROM derived_global_ancestry WHERE is_active
UNION ALL SELECT 'archaic',       COUNT(*) FROM derived_archaic_ancestry WHERE is_active
UNION ALL SELECT 'compound_het',  COUNT(*) FROM derived_compound_het     WHERE is_active;
```

---

## Application-layer concerns

1. **Re-run triggers.** Each derived analysis re-runs when (a) variant data changes (new ingestion or imputation), or (b) a source DB it depends on updates. Dependency map (in app code):

   | Analysis | Depends on |
   |---|---|
   | `pgs` | variants, PGS Catalog scores+weights, gnomAD (for percentile) |
   | `pgx` | variants, PharmGKB, CPIC, PharmCAT bundle |
   | `carrier` | variants, ClinVar, genes |
   | `acmg_sf` | variants, ClinVar, genes (is_acmg_sf) |
   | `hla` | variants, HIBAG model |
   | `roh` | variants only |
   | `haplogroup` | Y / mtDNA variants, haplogroup tree |
   | `global_ancestry` | variants, reference panel |
   | `local_ancestry` | variants, reference panel |
   | `archaic_ancestry` | variants, archaic reference genomes |
   | `compound_het` | variants, ClinVar (for P/LP filter), phasing |
   | `genome_qc` | variants only |

2. **`derived_*` rows feed insights, not the user directly.** The insight generators in group 4 read these tables and produce `insights` + `evidence` rows. The user never reads derived tables directly — they read the insights.

3. **Polymorphic `insights.subject_id` resolution:** the application maps `subject_type` to the appropriate derived table when needed (e.g., `subject_type='score' AND subject_id='PGS000123'` → `derived_pgs`).

4. **Confidence scoring is method-specific.** Each pipeline has its own confidence calibration; do not compare across methods without normalization.

5. **Storage estimates** (orders of magnitude):

   | Table | Expected rows |
   |---|---|
   | `derived_pgs` | ~2,000–4,000 (one per applicable PGS) |
   | `derived_pgx_phenotypes` | ~25 (one per actionable PGx gene) |
   | `derived_carrier_findings` | ~10–50 |
   | `derived_acmg_sf_findings` | typically 0–5 |
   | `derived_hla_typing` | ~6–10 (one per locus) |
   | `derived_roh` | dozens to hundreds |
   | `derived_haplogroups` | 2 (Y + mtDNA) |
   | `derived_global_ancestry` | dozens (one per population component) |
   | `derived_local_ancestry` | thousands (segments) |

---

## Cross-group references — application-validated

DuckDB does not support `ALTER TABLE ... ADD CONSTRAINT`, so the following group-3 → group-1
links are validated in application code rather than enforced by the database — consistent
with the SQLite (group 5) pattern where cross-DB references are also application-validated:

- `derived_acmg_sf_findings.variant_id` → `variants_master.variant_id`
- `derived_compound_het.variant_id_1` → `variants_master.variant_id`
- `derived_compound_het.variant_id_2` → `variants_master.variant_id`

The remaining group 4 → group 3 polymorphic linkage is also application-level (no FK).

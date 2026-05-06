-- Group 4 — Insights & Evidence Schema
-- Target: DuckDB (genome.duckdb)
-- Extracted verbatim from docs/schemas/schema_group_4_insights_evidence.md

-- ENUMs

CREATE TYPE insight_type_enum AS ENUM (
  'pgx',           -- pharmacogenomic phenotype
  'prs',           -- polygenic risk score
  'carrier',       -- recessive carrier finding
  'clinvar',       -- ClinVar pathogenic / likely-pathogenic finding
  'acmg_sf',       -- ACMG secondary-findings hit
  'trait',         -- general trait / predisposition
  'nutrition',     -- nutrigenomic
  'chronotype',    -- sleep / circadian
  'hla',           -- HLA-driven (autoimmune, drug hypersensitivity, transplant)
  'pleiotropy',    -- one variant, many traits
  'compound',      -- multiple variants converging on one trait
  'pathway'        -- pathway-level finding
);

CREATE TYPE actionability_enum AS ENUM (
  'high', 'medium', 'low', 'informational'
);

CREATE TYPE evidence_tier_enum AS ENUM (
  '1A', '1B', '2A', '2B', '3', '4'
);

CREATE TYPE subject_type_enum AS ENUM (
  'variant', 'gene', 'pathway', 'score', 'haplotype', 'genome'
);

CREATE TYPE effect_direction_enum AS ENUM (
  'increased_risk', 'decreased_risk', 'protective',
  'altered_function', 'neutral', 'unknown'
);

-- Core tables

-- insights

CREATE TABLE insights (
  insight_id            UUID PRIMARY KEY DEFAULT uuid(),

  -- Classification
  insight_type          insight_type_enum NOT NULL,

  -- Display
  title                 VARCHAR NOT NULL,
  summary_short         VARCHAR NOT NULL,    -- one-liner for cards
  summary_long          TEXT,                -- paragraph for detail view
  rendering_eli5        TEXT,                -- audience-rendered text (cached)
  rendering_layperson   TEXT,
  rendering_clinical    TEXT,

  -- Subject (polymorphic)
  subject_type          subject_type_enum NOT NULL,
  subject_id            VARCHAR NOT NULL,    -- variant_id / gene_symbol / pgs_id / etc.

  -- Evidence rollup
  actionability         actionability_enum NOT NULL,
  evidence_tier         evidence_tier_enum NOT NULL,
  confidence_score      DECIMAL(3,2),        -- 0.00–1.00
  evidence_count        INTEGER DEFAULT 0,
  conflicting_evidence  BOOLEAN DEFAULT FALSE,

  -- Effect
  effect_size_value     DOUBLE,
  effect_size_unit      VARCHAR,             -- 'OR', 'beta', 'percentile', 'fold_change'
  effect_direction      effect_direction_enum,

  -- Generation provenance
  generation_method     VARCHAR NOT NULL,    -- 'rule_based', 'pgs_calc', 'pharmcat',
                                             -- 'hibag', 'llm_synth'
  generation_version    VARCHAR NOT NULL,    -- e.g. 'pharmcat_2.13.0'
  generated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  regenerate_after      TIMESTAMP,           -- staleness target

  -- User state (denormalized for fast filtering)
  is_starred            BOOLEAN DEFAULT FALSE,
  is_reviewed           BOOLEAN DEFAULT FALSE,
  reviewed_at           TIMESTAMP,

  -- Lifecycle
  is_active             BOOLEAN DEFAULT TRUE,
  superseded_by         UUID,                -- self-FK to newer version
  supersede_reason      VARCHAR,             -- 'source_updated', 'reclassified',
                                             -- 'tier_changed', 'evidence_added'

  -- Audit
  created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_insights_type        ON insights(insight_type);
CREATE INDEX idx_insights_subject     ON insights(subject_type, subject_id);
CREATE INDEX idx_insights_actionable  ON insights(actionability, is_active);
CREATE INDEX idx_insights_tier        ON insights(evidence_tier, is_active);
CREATE INDEX idx_insights_active      ON insights(is_active);

-- evidence

CREATE TABLE evidence (
  evidence_id         UUID PRIMARY KEY DEFAULT uuid(),
  insight_id          UUID NOT NULL REFERENCES insights(insight_id),

  -- Source provenance
  source_db           VARCHAR NOT NULL,    -- 'clinvar', 'gwas_catalog', 'pharmgkb',
                                           -- 'cpic', 'pgs_catalog', 'pubmed', 'vep',
                                           -- 'gnomad', 'hibag', 'pharmcat', 'custom'
  source_version      VARCHAR NOT NULL,    -- e.g. 'clinvar_2026_04_15'
  source_record_id    VARCHAR,             -- ID inside that source for re-fetching
  retrieval_date      TIMESTAMP NOT NULL,
  retrieval_query     TEXT,                -- exact query used (reproducibility)

  -- Claim
  claim               TEXT NOT NULL,

  -- Statistics (nullable per source type)
  effect_size         DOUBLE,
  effect_size_unit    VARCHAR,
  p_value             DOUBLE,
  confidence_interval VARCHAR,             -- e.g. '0.85-1.42'
  sample_size         INTEGER,
  ancestry            VARCHAR,             -- 'EUR', 'AFR', 'EAS', 'SAS', 'AMR', 'multi'
  replication_count   INTEGER,

  -- Tier mapping
  tier_assigned       evidence_tier_enum NOT NULL,
  tier_mapping_rule   VARCHAR NOT NULL,    -- e.g. 'cpic_to_unified_v1'
  weight_in_rollup    DECIMAL(3,2) DEFAULT 1.0,

  -- Citations
  citation_pmids      VARCHAR[],
  citation_dois       VARCHAR[],
  citation_urls       VARCHAR[],

  -- Lifecycle
  is_active           BOOLEAN DEFAULT TRUE,
  superseded_by       UUID,
  supersede_reason    VARCHAR,

  created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_evidence_insight ON evidence(insight_id);
CREATE INDEX idx_evidence_source  ON evidence(source_db, source_version);
CREATE INDEX idx_evidence_active  ON evidence(is_active);

-- Junction tables

-- Variants that drive an insight
CREATE TABLE insight_variants (
  insight_id          UUID NOT NULL REFERENCES insights(insight_id),
  variant_id          BIGINT NOT NULL,     -- FK to variants_master (group 1)
  role                VARCHAR NOT NULL,    -- 'causal', 'supporting',
                                           -- 'contributing', 'tagging'
  allele_dosage       SMALLINT,            -- 0, 1, 2 (NULL = missing)
  effect_allele       VARCHAR(20),
  PRIMARY KEY (insight_id, variant_id)
);
CREATE INDEX idx_iv_variant ON insight_variants(variant_id);

-- Genes related to an insight
CREATE TABLE insight_genes (
  insight_id          UUID NOT NULL REFERENCES insights(insight_id),
  gene_symbol         VARCHAR NOT NULL,    -- FK to genes (group 2)
  relationship        VARCHAR NOT NULL,    -- 'primary', 'related', 'in_pathway'
  PRIMARY KEY (insight_id, gene_symbol)
);
CREATE INDEX idx_ig_gene ON insight_genes(gene_symbol);

-- Traits related to an insight (powers compound view + filters)
CREATE TABLE insight_traits (
  insight_id          UUID NOT NULL REFERENCES insights(insight_id),
  trait_id            VARCHAR NOT NULL,    -- FK to traits (group 2; EFO/HPO/MeSH)
  trait_role          VARCHAR NOT NULL,    -- 'primary', 'related', 'comorbid'
  PRIMARY KEY (insight_id, trait_id)
);
CREATE INDEX idx_it_trait ON insight_traits(trait_id);

-- Views

-- Per-gene rollup (drives gene drill-down pages)
CREATE VIEW gene_rollup_v AS
SELECT
  ig.gene_symbol,
  COUNT(DISTINCT i.insight_id)                                            AS insight_count,
  COUNT(DISTINCT i.insight_id)
    FILTER (WHERE i.actionability = 'high')                               AS high_actionable_count,
  MIN(i.evidence_tier::VARCHAR)                                           AS strongest_tier,
  MAX(i.confidence_score)                                                 AS max_confidence,
  ARRAY_AGG(DISTINCT i.insight_type)                                      AS insight_types
FROM insight_genes ig
JOIN insights i USING (insight_id)
WHERE i.is_active = TRUE
GROUP BY ig.gene_symbol;

-- Pleiotropy: variants driving insights across multiple traits
CREATE VIEW pleiotropy_v AS
SELECT
  iv.variant_id,
  COUNT(DISTINCT it.trait_id)         AS trait_count,
  ARRAY_AGG(DISTINCT it.trait_id)     AS trait_ids,
  ARRAY_AGG(DISTINCT i.insight_id)    AS insight_ids
FROM insight_variants iv
JOIN insights i USING (insight_id)
JOIN insight_traits it USING (insight_id)
WHERE i.is_active = TRUE
GROUP BY iv.variant_id
HAVING COUNT(DISTINCT it.trait_id) > 1;

-- Compound effects: multiple insights converging on one trait
CREATE VIEW compound_effects_v AS
SELECT
  it.trait_id,
  COUNT(DISTINCT i.insight_id)                                AS converging_insights,
  ARRAY_AGG(DISTINCT i.insight_id)                            AS insight_ids,
  ARRAY_AGG(DISTINCT iv.variant_id)                           AS contributing_variants,
  AVG(i.confidence_score)                                     AS avg_confidence,
  COUNT(*) FILTER (WHERE i.effect_direction = 'increased_risk')  AS increase_n,
  COUNT(*) FILTER (WHERE i.effect_direction = 'decreased_risk')  AS decrease_n
FROM insight_traits it
JOIN insights i USING (insight_id)
LEFT JOIN insight_variants iv USING (insight_id)
WHERE i.is_active = TRUE
GROUP BY it.trait_id
HAVING COUNT(DISTINCT i.insight_id) >= 2;

-- Materialized summary (refreshed by job, not a view)

CREATE TABLE summary_dashboard (
  category            VARCHAR PRIMARY KEY,    -- 'pharmacogenomics', 'cancer_risk', etc.
  total_insights      INTEGER,
  high_actionable     INTEGER,
  medium_actionable   INTEGER,
  unreviewed_count    INTEGER,
  starred_count       INTEGER,
  highest_tier        evidence_tier_enum,
  last_updated        TIMESTAMP,
  metadata_json       JSON
);

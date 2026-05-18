# Group 2 — Reference Annotations Schema

The bulk-loaded knowledge layer: dbSNP, ClinVar, GWAS Catalog, PharmGKB, CPIC, PGS Catalog, gnomAD, VEP, plus the gene/trait/pathway dictionaries. Everything in groups 3 and 4 joins against this.

**Target:** DuckDB (`genome.duckdb`)

---

## Design principles

1. **Versioning on every row.** Every annotation references `annotation_source_versions` for full traceability. When ClinVar updates, old rows are deactivated rather than overwritten — supports supersession (group 4) and snapshot reproducibility.

2. **Two coverage strategies.** Some sources are bulk-loaded in full because their utility depends on completeness; others are filtered to variants we care about because they're too large.

3. **rsID + position both indexed everywhere.** Some annotation sources key on rsID, others on `(chrom, pos, ref, alt)`. Index both on every table that has both.

4. **Denormalized rollup table.** `variant_annotations_index` is a precomputed per-variant summary across all sources — powers fast SNP-detail page loads. Refreshed by job whenever source data changes.

5. **Soft-delete for evolving sources.** ClinVar, GWAS Catalog, PharmGKB, CPIC all evolve. Keep history via `is_active` + `superseded_by` in the row, then refresh `variant_annotations_index`.

---

## Coverage strategy

| Source | Strategy | Reason |
|---|---|---|
| ClinVar | **Full bulk-load** | Whole point is presence/absence checks |
| GWAS Catalog | **Full bulk-load** | Small enough (~500K associations) |
| PharmGKB | **Full bulk-load** | Curated; not large |
| CPIC | **Full bulk-load** | ~25 guidelines, fully load |
| PGS Catalog scores | **Full bulk-load (metadata)** | Just the score-list |
| PGS Catalog weights | **Overlapping-only** | Per locked decision #5 |
| gnomAD | **Filtered to overlap** | Full is ~140GB; filter to variants in (user ∪ ClinVar ∪ GWAS ∪ PGS) |
| dbSNP | **Filtered to overlap** | Same reasoning |
| VEP | **Computed on user variants** | Run locally via Ensembl VEP CLI; no bulk-load needed |
| Genes / Traits / Pathways | **Full bulk-load** | Small reference dictionaries |

---

## Master version registry

```sql
CREATE TABLE annotation_source_versions (
  source_version_id     BIGINT PRIMARY KEY,
  source_db             VARCHAR NOT NULL,        -- 'clinvar', 'gwas_catalog', 'pharmgkb',
                                                 -- 'cpic', 'pgs_catalog', 'gnomad',
                                                 -- 'dbsnp', 'vep', 'hgnc', 'efo', 'kegg'
  version               VARCHAR NOT NULL,        -- e.g. '2026_04_15'
  ingested_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  source_url            VARCHAR,                 -- where downloaded from
  source_file_hash      VARCHAR(64),
  source_file_size      BIGINT,
  record_count          INTEGER,
  is_current            BOOLEAN DEFAULT TRUE,    -- one current row per source_db
  notes                 TEXT,
  UNIQUE (source_db, version)
);

CREATE INDEX idx_asv_current ON annotation_source_versions(source_db, is_current);
```

> **Application invariant:** at most one `is_current = TRUE` row per `source_db`. New ingest deactivates the prior current row.

---

## Variant-level annotations

### `dbsnp_annotations`

```sql
CREATE TABLE dbsnp_annotations (
  rsid                  VARCHAR PRIMARY KEY,
  chrom                 chromosome_enum,
  pos_grch38            BIGINT,
  pos_grch37            BIGINT,
  ref_allele            VARCHAR,
  alt_alleles           VARCHAR[],               -- multi-allelic preserved here
  variant_class         VARCHAR,                 -- 'snv', 'in-del', 'mnv'
  gene_symbols          VARCHAR[],
  functional_class      VARCHAR,                 -- 'missense', 'synonymous', 'intron', 'utr'
  is_clinical           BOOLEAN,                 -- has any clinical significance
  source_version_id     BIGINT NOT NULL REFERENCES annotation_source_versions(source_version_id),
  retrieval_date        TIMESTAMP NOT NULL
);

CREATE INDEX idx_dbsnp_pos38 ON dbsnp_annotations(chrom, pos_grch38);
```

### `variant_aliases` — rsID merges and withdrawals

```sql
CREATE TABLE variant_aliases (
  alias_rsid            VARCHAR PRIMARY KEY,
  current_rsid          VARCHAR NOT NULL,        -- the canonical rsID after merge
  alias_type            VARCHAR,                 -- 'merged', 'withdrawn', 'split'
  source_version_id     BIGINT NOT NULL REFERENCES annotation_source_versions(source_version_id),
  retrieval_date        TIMESTAMP NOT NULL
);

CREATE INDEX idx_va_current ON variant_aliases(current_rsid);
```

### `clinvar_annotations`

```sql
CREATE TABLE clinvar_annotations (
  clinvar_id            BIGINT PRIMARY KEY,
  variation_id          VARCHAR,                 -- ClinVar VariationID
  rsid                  VARCHAR,                 -- nullable; not all entries have rsID
  chrom                 chromosome_enum,
  pos_grch38            BIGINT,
  ref_allele            VARCHAR,
  alt_allele            VARCHAR,

  -- Clinical interpretation
  clinical_significance VARCHAR,                 -- 'Pathogenic', 'Likely pathogenic',
                                                 -- 'Uncertain significance', 'Likely benign',
                                                 -- 'Benign', 'Conflicting', 'drug response'
  review_status         VARCHAR,                 -- text; e.g. 'reviewed by expert panel'
  star_rating           SMALLINT,                -- 0–4
  last_evaluated        DATE,

  -- Conditions
  conditions            VARCHAR[],               -- disease names
  condition_ids         VARCHAR[],               -- MedGen / OMIM / MONDO IDs

  -- Submission info
  submission_count      INTEGER,
  submitter_categories  VARCHAR[],               -- 'expert_panel', 'clinical_lab', 'lit_only'

  -- HGVS
  hgvs_c                VARCHAR,
  hgvs_p                VARCHAR,

  -- Inheritance
  inheritance           VARCHAR,                 -- 'AD', 'AR', 'XL', 'mitochondrial'

  -- Lifecycle
  source_version_id     BIGINT NOT NULL REFERENCES annotation_source_versions(source_version_id),
  retrieval_date        TIMESTAMP NOT NULL,
  is_active             BOOLEAN DEFAULT TRUE,
  superseded_by         BIGINT
);

CREATE INDEX idx_cv_rsid          ON clinvar_annotations(rsid);
CREATE INDEX idx_cv_pos           ON clinvar_annotations(chrom, pos_grch38);
CREATE INDEX idx_cv_significance  ON clinvar_annotations(clinical_significance, is_active);
CREATE INDEX idx_cv_active        ON clinvar_annotations(is_active);
```

### `gwas_catalog_associations`

```sql
CREATE TABLE gwas_catalog_associations (
  association_id        BIGINT PRIMARY KEY,
  study_accession       VARCHAR,                 -- GCST...
  pmid                  VARCHAR,

  -- Variant
  rsid                  VARCHAR NOT NULL,
  chrom                 chromosome_enum,
  pos_grch38            BIGINT,

  -- Trait
  trait_id              VARCHAR,                 -- EFO term
  trait_name            VARCHAR,
  mapped_trait_uri      VARCHAR,

  -- Statistics
  effect_size           DOUBLE,
  effect_size_unit      VARCHAR,                 -- 'beta', 'OR', 'HR'
  effect_allele         VARCHAR(20),
  other_allele          VARCHAR(20),
  effect_allele_freq    DOUBLE,
  ci_95_lower           DOUBLE,
  ci_95_upper           DOUBLE,
  p_value               DOUBLE,

  -- Study context
  sample_size_initial      INTEGER,
  sample_size_replication  INTEGER,
  ancestry              VARCHAR,                 -- 'EUR', 'AFR', 'multi', etc.
  is_replicated         BOOLEAN,

  -- Lifecycle
  source_version_id     BIGINT NOT NULL REFERENCES annotation_source_versions(source_version_id),
  retrieval_date        TIMESTAMP NOT NULL,
  is_active             BOOLEAN DEFAULT TRUE
);

CREATE INDEX idx_gwas_rsid    ON gwas_catalog_associations(rsid);
CREATE INDEX idx_gwas_trait   ON gwas_catalog_associations(trait_id);
CREATE INDEX idx_gwas_pos     ON gwas_catalog_associations(chrom, pos_grch38);
CREATE INDEX idx_gwas_pvalue  ON gwas_catalog_associations(p_value);
```

### `gnomad_frequencies` — population AFs

```sql
CREATE TABLE gnomad_frequencies (
  freq_id               BIGINT PRIMARY KEY,
  rsid                  VARCHAR,
  chrom                 chromosome_enum,
  pos_grch38            BIGINT,
  ref_allele            VARCHAR,
  alt_allele            VARCHAR,

  -- Global
  af_global             DOUBLE,
  ac_global             INTEGER,
  an_global             INTEGER,

  -- Per-population (gnomAD v4)
  af_afr                DOUBLE,                  -- African / African American
  af_ami                DOUBLE,                  -- Amish
  af_amr                DOUBLE,                  -- Latino / Admixed American
  af_asj                DOUBLE,                  -- Ashkenazi Jewish
  af_eas                DOUBLE,                  -- East Asian
  af_fin                DOUBLE,                  -- Finnish
  af_nfe                DOUBLE,                  -- Non-Finnish European
  af_sas                DOUBLE,                  -- South Asian
  af_oth                DOUBLE,                  -- Other / unspecified

  -- Quality
  filter_status         VARCHAR,                 -- 'PASS' or filter codes

  source_version_id     BIGINT NOT NULL REFERENCES annotation_source_versions(source_version_id),
  retrieval_date        TIMESTAMP NOT NULL
);

CREATE INDEX idx_gnomad_rsid  ON gnomad_frequencies(rsid);
CREATE INDEX idx_gnomad_pos   ON gnomad_frequencies(chrom, pos_grch38);
```

### `vep_consequences` — functional predictions

```sql
CREATE TABLE vep_consequences (
  consequence_id          BIGINT PRIMARY KEY,
  variant_id              BIGINT,                -- FK to variants_master once ETL'd
  rsid                    VARCHAR,
  chrom                   chromosome_enum,
  pos_grch38              BIGINT,
  ref_allele              VARCHAR,
  alt_allele              VARCHAR,

  -- Consequence
  most_severe_consequence VARCHAR,               -- 'missense_variant', 'stop_gained', etc.
  impact                  VARCHAR,               -- 'HIGH', 'MODERATE', 'LOW', 'MODIFIER'
  gene_symbol             VARCHAR,
  feature_type            VARCHAR,               -- 'transcript', 'regulatory_feature'
  feature_id              VARCHAR,
  is_canonical            BOOLEAN,
  biotype                 VARCHAR,

  -- HGVS
  hgvs_c                  VARCHAR,
  hgvs_p                  VARCHAR,

  -- Pathogenicity scores
  sift_score              DOUBLE,
  sift_prediction         VARCHAR,
  polyphen_score          DOUBLE,
  polyphen_prediction     VARCHAR,
  cadd_phred              DOUBLE,
  revel_score             DOUBLE,
  alphamissense_score     DOUBLE,
  alphamissense_class     VARCHAR,
  spliceai_max            DOUBLE,

  source_version_id       BIGINT NOT NULL REFERENCES annotation_source_versions(source_version_id),
  retrieval_date          TIMESTAMP NOT NULL
);

CREATE INDEX idx_vep_variant    ON vep_consequences(variant_id);
CREATE INDEX idx_vep_rsid       ON vep_consequences(rsid);
CREATE INDEX idx_vep_pos        ON vep_consequences(chrom, pos_grch38);
CREATE INDEX idx_vep_gene       ON vep_consequences(gene_symbol);
CREATE INDEX idx_vep_canonical  ON vep_consequences(variant_id, is_canonical);
```

---

## Pharmacogenomics

### `pharmgkb_annotations`

```sql
CREATE TABLE pharmgkb_annotations (
  pharmgkb_id           BIGINT PRIMARY KEY,
  pgkb_accession        VARCHAR,                 -- 'PA' IDs

  -- Variant context
  rsid                  VARCHAR,
  chrom                 chromosome_enum,
  pos_grch38            BIGINT,
  gene_symbol           VARCHAR,
  star_allele           VARCHAR,                 -- e.g. 'CYP2D6*4'
  haplotype             VARCHAR,

  -- Drug
  drug_name             VARCHAR,
  drug_rxnorm_id        VARCHAR,
  drug_atc_code         VARCHAR,

  -- Annotation
  phenotype_category    VARCHAR,                 -- 'metabolizer', 'response', 'toxicity'
  functional_status     VARCHAR,                 -- 'poor', 'intermediate', 'normal',
                                                 -- 'rapid', 'ultrarapid'
  evidence_level        VARCHAR,                 -- '1A', '1B', '2A', '2B', '3', '4'
  guideline_summary     TEXT,
  guideline_url         VARCHAR,

  source_version_id     BIGINT NOT NULL REFERENCES annotation_source_versions(source_version_id),
  retrieval_date        TIMESTAMP NOT NULL,
  is_active             BOOLEAN DEFAULT TRUE
);

CREATE INDEX idx_pgkb_rsid  ON pharmgkb_annotations(rsid);
CREATE INDEX idx_pgkb_gene  ON pharmgkb_annotations(gene_symbol);
CREATE INDEX idx_pgkb_drug  ON pharmgkb_annotations(drug_name);
CREATE INDEX idx_pgkb_star  ON pharmgkb_annotations(star_allele);
```

### `cpic_guidelines` — clinical-grade drug-gene guidance

```sql
CREATE TABLE cpic_guidelines (
  guideline_id            BIGINT PRIMARY KEY,
  cpic_id                 VARCHAR,

  -- Drug-gene pair
  gene_symbol             VARCHAR NOT NULL,
  drug_name               VARCHAR NOT NULL,
  drug_rxnorm_id          VARCHAR,

  -- Recommendation
  phenotype               VARCHAR,               -- 'CYP2C19 Poor Metabolizer'
  recommendation          TEXT,                  -- the actual clinical guidance
  classification_strength VARCHAR,               -- 'Strong', 'Moderate', 'Optional'
  cpic_level              VARCHAR,               -- 'A', 'B', 'C', 'D'
  pediatric               BOOLEAN,

  guideline_url           VARCHAR,
  publication_pmid        VARCHAR,
  last_updated            DATE,

  source_version_id       BIGINT NOT NULL REFERENCES annotation_source_versions(source_version_id),
  retrieval_date          TIMESTAMP NOT NULL,
  is_active               BOOLEAN DEFAULT TRUE
);

CREATE INDEX idx_cpic_gene_drug ON cpic_guidelines(gene_symbol, drug_name);
CREATE INDEX idx_cpic_drug      ON cpic_guidelines(drug_name);
```

---

## Polygenic scores

### `pgs_catalog_scores` — score definitions

```sql
CREATE TABLE pgs_catalog_scores (
  score_record_id       BIGINT PRIMARY KEY,      -- surrogate PK, app-allocated
  pgs_id                VARCHAR NOT NULL,        -- 'PGS000001' etc.
  pgs_name              VARCHAR,

  -- Trait
  trait_efo             VARCHAR,
  trait_reported        VARCHAR,
  trait_category        VARCHAR,

  -- Publication
  publication_pmid      VARCHAR,
  publication_doi       VARCHAR,
  publication_year      INTEGER,

  -- Volume
  variants_total        INTEGER,                 -- in original score
  weights_storage       VARCHAR DEFAULT 'overlapping_only',  -- or 'full' for promoted

  -- Population
  reference_population  VARCHAR,
  ancestry_distribution VARCHAR,

  -- Performance
  performance_auc       DOUBLE,
  performance_or_per_sd DOUBLE,

  -- Lifecycle
  source_version_id     BIGINT NOT NULL REFERENCES annotation_source_versions(source_version_id),
  retrieval_date        TIMESTAMP NOT NULL,
  is_active             BOOLEAN DEFAULT TRUE
);

CREATE INDEX idx_pgs_id    ON pgs_catalog_scores(pgs_id, is_active);
CREATE INDEX idx_pgs_trait ON pgs_catalog_scores(trait_efo);
```

### `pgs_score_weights` — overlapping-only weights

```sql
CREATE TABLE pgs_score_weights (
  weight_id             BIGINT PRIMARY KEY,
  pgs_id                VARCHAR NOT NULL,        -- application-validated
                                                 -- against pgs_catalog_scores(pgs_id)

  rsid                  VARCHAR,
  chrom                 chromosome_enum,
  pos_grch38            BIGINT,
  effect_allele         VARCHAR(20),
  other_allele          VARCHAR(20),
  weight                DOUBLE NOT NULL,         -- beta or log-OR

  source_version_id     BIGINT NOT NULL REFERENCES annotation_source_versions(source_version_id)
);

CREATE INDEX idx_psw_pgs   ON pgs_score_weights(pgs_id);
CREATE INDEX idx_psw_rsid  ON pgs_score_weights(rsid);
CREATE INDEX idx_psw_pos   ON pgs_score_weights(chrom, pos_grch38);
```

---

## Reference dictionaries

### `genes`

```sql
CREATE TABLE genes (
  gene_symbol           VARCHAR PRIMARY KEY,     -- HGNC official symbol
  ensembl_gene_id       VARCHAR,                 -- ENSG...
  entrez_gene_id        INTEGER,
  hgnc_id               VARCHAR,

  -- Location
  chrom                 chromosome_enum,
  start_grch38          BIGINT,
  end_grch38            BIGINT,
  strand                VARCHAR(1),

  -- Biology
  gene_type             VARCHAR,                 -- 'protein_coding', 'lncRNA', etc.
  description           TEXT,

  -- Cross-references
  omim_id               VARCHAR,
  uniprot_id            VARCHAR,

  -- Clinical flags
  is_acmg_sf            BOOLEAN DEFAULT FALSE,
  acmg_sf_disease       VARCHAR,
  acmg_sf_inheritance   VARCHAR,
  acmg_sf_version       VARCHAR,                 -- 'v3.2'

  is_pgx_relevant       BOOLEAN DEFAULT FALSE,
  is_haploinsufficient  BOOLEAN,

  source_version_id     BIGINT REFERENCES annotation_source_versions(source_version_id),
  retrieval_date        TIMESTAMP
);

CREATE INDEX idx_genes_chrom  ON genes(chrom);
CREATE INDEX idx_genes_acmg   ON genes(is_acmg_sf);
CREATE INDEX idx_genes_pgx    ON genes(is_pgx_relevant);
```

### `traits`

```sql
CREATE TABLE traits (
  trait_id              VARCHAR PRIMARY KEY,     -- EFO/HPO/MONDO/MeSH ID
  ontology              VARCHAR NOT NULL,        -- 'EFO', 'HPO', 'MONDO', 'MeSH'
  trait_name            VARCHAR NOT NULL,
  description           TEXT,
  synonyms              VARCHAR[],

  category              VARCHAR,                 -- top-level UI grouping
  parent_trait_ids      VARCHAR[],               -- hierarchical browse

  is_disease            BOOLEAN,
  is_quantitative       BOOLEAN,

  source_version_id     BIGINT REFERENCES annotation_source_versions(source_version_id),
  retrieval_date        TIMESTAMP
);

CREATE INDEX idx_traits_ontology  ON traits(ontology);
CREATE INDEX idx_traits_category  ON traits(category);
```

### `pathways` and `pathway_genes`

```sql
CREATE TABLE pathways (
  pathway_id            VARCHAR PRIMARY KEY,     -- 'KEGG:hsa00010', 'R-HSA-1234567'
  ontology              VARCHAR NOT NULL,        -- 'KEGG', 'Reactome', 'WikiPathways'
  pathway_name          VARCHAR NOT NULL,
  description           TEXT,
  category              VARCHAR,
  gene_symbols          VARCHAR[],               -- denormalized for fast pathway lookup

  source_version_id     BIGINT REFERENCES annotation_source_versions(source_version_id),
  retrieval_date        TIMESTAMP
);

CREATE INDEX idx_pathways_ontology ON pathways(ontology);

CREATE TABLE pathway_genes (
  pathway_id            VARCHAR NOT NULL REFERENCES pathways(pathway_id),
  gene_symbol           VARCHAR NOT NULL REFERENCES genes(gene_symbol),
  PRIMARY KEY (pathway_id, gene_symbol)
);

CREATE INDEX idx_pg_gene ON pathway_genes(gene_symbol);
```

---

## Materialized rollup — `variant_annotations_index`

Per-variant precomputed summary across all sources. Refreshed by job after annotation updates. **This is what the SNP detail page reads from for its overview.**

```sql
CREATE TABLE variant_annotations_index (
  variant_id              BIGINT PRIMARY KEY REFERENCES variants_master(variant_id),

  -- ClinVar rollup
  clinvar_significance    VARCHAR,               -- worst significance found
  clinvar_star_rating     SMALLINT,              -- highest star rating
  clinvar_count           INTEGER,
  clinvar_conditions      VARCHAR[],

  -- GWAS rollup
  gwas_trait_count        INTEGER,
  gwas_min_p_value        DOUBLE,
  gwas_traits             VARCHAR[],
  gwas_strongest_trait    VARCHAR,

  -- gnomAD
  af_global               DOUBLE,
  af_max_population       DOUBLE,
  af_min_population       DOUBLE,
  is_rare                 BOOLEAN,               -- AF < 0.01
  is_ultrarare            BOOLEAN,               -- AF < 0.001

  -- VEP (canonical transcript)
  most_severe_consequence VARCHAR,
  impact                  VARCHAR,
  cadd_phred              DOUBLE,
  alphamissense_class     VARCHAR,

  -- PharmGKB
  has_pgx                 BOOLEAN,
  pgx_drug_count          INTEGER,
  pgx_drugs               VARCHAR[],

  -- Flags
  is_acmg_sf              BOOLEAN,
  is_curated              BOOLEAN,               -- present in any curated source

  -- Refresh metadata
  last_refreshed          TIMESTAMP NOT NULL,
  refresh_versions        JSON                   -- {clinvar: '2026_04_15', gwas: '...', ...}
);

CREATE INDEX idx_vai_clinvar  ON variant_annotations_index(clinvar_significance);
CREATE INDEX idx_vai_acmg     ON variant_annotations_index(is_acmg_sf);
CREATE INDEX idx_vai_rare     ON variant_annotations_index(is_rare);
CREATE INDEX idx_vai_pgx      ON variant_annotations_index(has_pgx);
CREATE INDEX idx_vai_impact   ON variant_annotations_index(impact);
```

---

## Views

```sql
-- One-row-per-variant lookup combining master + consensus + annotation index
-- (the canonical "everything about this variant" query)
CREATE VIEW variant_full_v AS
SELECT
  vm.*,
  cg.consensus_allele_1, cg.consensus_allele_2, cg.dosage,
  cg.consensus_method, cg.is_imputed AS consensus_is_imputed, cg.confidence,
  vai.clinvar_significance, vai.clinvar_star_rating,
  vai.gwas_trait_count, vai.gwas_strongest_trait, vai.gwas_min_p_value,
  vai.af_global, vai.is_rare, vai.is_ultrarare,
  vai.most_severe_consequence, vai.impact, vai.cadd_phred,
  vai.has_pgx, vai.pgx_drug_count,
  vai.is_acmg_sf
FROM variants_master vm
LEFT JOIN consensus_genotypes        cg  ON cg.variant_id  = vm.variant_id
LEFT JOIN variant_annotations_index  vai ON vai.variant_id = vm.variant_id;

-- Per-gene clinically-flagged variant rollup
CREATE VIEW gene_variant_summary_v AS
SELECT
  g.gene_symbol,
  g.is_acmg_sf,
  COUNT(DISTINCT vm.variant_id) AS user_variants_in_gene,
  COUNT(DISTINCT vm.variant_id) FILTER (
    WHERE vai.clinvar_significance IN ('Pathogenic', 'Likely pathogenic')
  ) AS pathogenic_count,
  COUNT(DISTINCT vm.variant_id) FILTER (WHERE vai.has_pgx) AS pgx_variant_count,
  COUNT(DISTINCT vm.variant_id) FILTER (
    WHERE vai.gwas_trait_count > 0
  ) AS gwas_variant_count
FROM genes g
LEFT JOIN variants_master vm
  ON vm.chrom = g.chrom
 AND vm.pos_grch38 BETWEEN g.start_grch38 AND g.end_grch38
LEFT JOIN variant_annotations_index vai ON vai.variant_id = vm.variant_id
GROUP BY g.gene_symbol, g.is_acmg_sf;

-- All PGx-relevant variants in user's data with current call
CREATE VIEW user_pgx_variants_v AS
SELECT DISTINCT
  vm.variant_id, vm.rsid, vm.chrom, vm.pos_grch38,
  cg.dosage, cg.consensus_method,
  pa.gene_symbol, pa.star_allele,
  ARRAY_AGG(DISTINCT pa.drug_name) AS affected_drugs,
  MIN(pa.evidence_level) AS strongest_evidence
FROM variants_master vm
JOIN consensus_genotypes cg ON cg.variant_id = vm.variant_id
JOIN pharmgkb_annotations pa
  ON pa.rsid = vm.rsid
 AND pa.is_active
WHERE cg.dosage > 0
GROUP BY vm.variant_id, vm.rsid, vm.chrom, vm.pos_grch38,
         cg.dosage, cg.consensus_method, pa.gene_symbol, pa.star_allele;
```

---

## Application-layer concerns (not in DDL)

1. **`variant_annotations_index` refresh.** Job-driven. Triggered when any source-DB version changes, or after a new ingestion adds variants. Idempotent rebuild.

2. **rsID resolution at lookup.** All annotation joins should canonicalize via `variant_aliases` first — given an input rsID, look up `current_rsid`, then join.

3. **Active-row filtering.** Most queries should filter `is_active = TRUE`. Convenience views or wrapper functions help avoid forgetting.

4. **gnomAD filtering set.** Before bulk-loading gnomAD, build the filter set: `(user variants) ∪ (ClinVar variants) ∪ (GWAS Catalog variants) ∪ (PGS Catalog variants)`. Drop everything else.

5. **VEP runs locally.** Use the Ensembl VEP CLI against your variants. Capture output into `vep_consequences`. Store the VEP version in `annotation_source_versions`.

6. **Refresh schedule** (suggested):

   | Source | Cadence |
   |---|---|
   | ClinVar | Weekly |
   | GWAS Catalog | Monthly |
   | PharmGKB / CPIC | Quarterly |
   | PGS Catalog | Monthly |
   | gnomAD | When new release drops (yearly-ish) |
   | VEP | When CLI version updates |
   | Genes / Traits / Pathways | Quarterly |

---

## Application-validated references

DuckDB does not support `ALTER TABLE ... ADD CONSTRAINT`, and its FK constraint additionally
requires that the target column carry a `PRIMARY KEY` or `UNIQUE` constraint. The following
links are therefore validated in application code rather than enforced by the database —
consistent with the SQLite (group 5) pattern where cross-DB references are also
application-validated:

- `insight_variants.variant_id` → `variants_master.variant_id` (group 4 → group 1)
- `insight_genes.gene_symbol` → `genes.gene_symbol` (group 4 → group 2)
- `insight_traits.trait_id` → `traits.trait_id` (group 4 → group 2)
- `vep_consequences.variant_id` → `variants_master.variant_id` (group 2 → group 1)
- `pgs_score_weights.pgs_id` → `pgs_catalog_scores.pgs_id` (same-group; the
  supersession pattern allows multiple rows per `pgs_id` so the natural key is no
  longer unique and DB-level FK is no longer expressible). This relationship was
  originally DB-enforced in sub-phase 5.0 but the 5.4 schema correction (surrogate
  PK on `pgs_catalog_scores`) demoted it to application-validated.
- `derived_pgs.pgs_id` → `pgs_catalog_scores.pgs_id` (group 3 → group 2; same
  reason as above: the 5.4 schema correction made `pgs_id` non-unique on
  `pgs_catalog_scores`, and DuckDB can no longer enforce a cross-group FK against
  it). The corresponding `derived_pgs.pgs_id` is now declared `VARCHAR NOT NULL`
  without a DB-level FK; application code in the PGS analysis pipeline is
  responsible for validating that the value exists in
  `pgs_catalog_scores(pgs_id)` (typically the active row).

The polymorphic `insights.subject_id` remains application-validated; no DB-level FK.

---

## Deliberately deferred to later groups

- **HLA reference data** (allele frequencies, disease associations) → group 3, accompanies `derived_hla_typing`
- **Y / mtDNA haplogroup trees** → group 3, accompanies `derived_haplogroups`
- **Star-allele definitions** for PGx → consumed by PharmCAT; not stored here. PharmCAT's bundled definitions are versioned via `derived_pgx_phenotypes.method`
- **Drug-drug interaction data** → out of scope; could add later via DrugBank

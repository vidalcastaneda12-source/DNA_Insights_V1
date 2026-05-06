-- Cross-group ALTER TABLE statements
-- Apply after all groups (1, 2, 3, 4) are loaded.
-- Extracted verbatim from the per-group schema markdown files.

-- ============================================================================
-- From group 1 — schema_group_1_genotype_data.md
-- ============================================================================

-- Forward references — apply once group 2 lands
-- After group 2 (reference annotations) is built:
ALTER TABLE variants_master
  ADD CONSTRAINT fk_acmg_sf_gene
  -- (handled via ETL: populate is_acmg_sf by joining to genes.is_acmg_sf)
;

-- FK constraints to add now (for group 4 → group 1 references)
ALTER TABLE insight_variants
  ADD CONSTRAINT fk_iv_variant
  FOREIGN KEY (variant_id) REFERENCES variants_master(variant_id);

-- ============================================================================
-- From group 2 — schema_group_2_reference_annotations.md
-- ============================================================================

-- group 4 → group 1
ALTER TABLE insight_variants
  ADD CONSTRAINT fk_iv_variant
  FOREIGN KEY (variant_id) REFERENCES variants_master(variant_id);

-- group 4 → group 2
ALTER TABLE insight_genes
  ADD CONSTRAINT fk_ig_gene
  FOREIGN KEY (gene_symbol) REFERENCES genes(gene_symbol);

ALTER TABLE insight_traits
  ADD CONSTRAINT fk_it_trait
  FOREIGN KEY (trait_id) REFERENCES traits(trait_id);

-- group 1 → group 2 (VEP)
ALTER TABLE vep_consequences
  ADD CONSTRAINT fk_vep_variant
  FOREIGN KEY (variant_id) REFERENCES variants_master(variant_id);

-- ============================================================================
-- From group 3 — schema_group_3_derived_analyses.md
-- ============================================================================

-- The remaining group 4 → group 3 polymorphic linkage is application-level (no FK).

-- Wire up derived_acmg_sf to its variant
ALTER TABLE derived_acmg_sf_findings
  ADD CONSTRAINT fk_acmg_variant
  FOREIGN KEY (variant_id) REFERENCES variants_master(variant_id);

-- Wire up compound_het to its two variants
ALTER TABLE derived_compound_het
  ADD CONSTRAINT fk_ch_variant_1
  FOREIGN KEY (variant_id_1) REFERENCES variants_master(variant_id);

ALTER TABLE derived_compound_het
  ADD CONSTRAINT fk_ch_variant_2
  FOREIGN KEY (variant_id_2) REFERENCES variants_master(variant_id);

# TopMed is not viable for personal genomics

## Context

Phase 4 originally targeted the TopMed Imputation Server as the imputation
backend. The local `genome imputation prepare` step shipped cleanly and
produced per-chromosome GRCh38 VCFs from `consensus_genotypes` matching
TopMed's documented input contract (gzipped VCFv4.2, chr-prefixed contigs,
biallelic SNVs, dosage-derived genotypes). A real upload was then attempted
against the user's merged 23andMe v5 + Ancestry v2 corpus.

## Observation

TopMed rejected the upload in ~50 seconds with a validation error reporting
a minimum of 20 samples per submission. This is not a free-tier limit, a
configuration option, or a documentation oversight — it is intrinsic to
TopMed's design. Their QC step performs intra-batch allele-frequency
comparison and population-level sanity checks against the submitted cohort
before the imputation engine ever runs; with a single sample, those checks
have no signal and the pipeline refuses to proceed.

The obvious workarounds are all unsafe:

- **Padding with duplicate copies of the user's genome.** Either
  intra-batch duplicate detection catches it and the upload is rejected
  for a different reason, or the duplicates slip through and the
  allele-frequency QC sees an artificial cohort whose statistics are
  meaningless. Imputation output derived against such a "cohort" has no
  defensible interpretation.
- **Padding with public reference samples (e.g. 1000 Genomes).** The
  resulting imputed VCFs mix the user's calls with strangers' calls,
  inflate the output by 20× with rows we don't want, and require a
  fragile filter pass to recover the user's row by sample ID. The
  reference panel itself is also the cohort used for imputation
  reference, so submitting it as input is methodologically incoherent.
- **Terms-of-use risk.** TopMed's submission terms assume a genuine
  multi-sample cohort owned or controlled by the submitter. Any padding
  strategy arguably violates those terms regardless of technical
  feasibility.

## Implication

External imputation services with cohort-based designs are fundamentally
mismatched with personal genomics. Phase 4 pivots to local imputation
using Beagle 5.5 against a 1000 Genomes Phase 3 reference panel held on
disk. The privacy posture improves — no external transmission of genome
data is required — at the cost of additional engineering (Java tooling,
subprocess management, panel and genetic-map handling) and disk usage
(~30–50 GB for the panel plus PLINK genetic map). Both are acceptable
for a personal-use app and align with the local-first principle locked
in `CLAUDE.md`.

## Follow-up

The Phase 4 pivot includes:

- Deleting TopMed-specific code (`topmed_client.py` and the
  `genome imputation submit | status | download` CLI subcommands tied to
  the external workflow).
- Introducing `reference_panel.py` and `beagle_runner.py` modules under
  `genome.imputation`, plus a `genome imputation panel install | status`
  subcommand pair for one-time reference-panel setup.
- Adding `'beagle_imputed'` to `source_enum`. The existing
  `'topmed_imputed'` value is retained for backward compatibility with
  any database files that already carry the enum but is unused going
  forward.
- Rewriting `docs/runbooks/imputation.md` for the local Beagle workflow
  (panel install, per-chromosome run, import).

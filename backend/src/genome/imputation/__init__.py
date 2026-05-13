"""Phase 4 — imputation pipeline.

Public entry points the CLI calls into:

* :func:`prepare_run` builds the upload VCFs from active genotype calls.
* :func:`import_result` streams the imputed VCFs into ``genotype_calls`` /
  ``variants_master``.
* :func:`list_runs` enumerates ``imputation_runs`` for display.
"""

from __future__ import annotations

from genome.imputation.archive import ImputationArchive
from genome.imputation.ingest import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_R2_THRESHOLD,
    IMPUTATION_PIPELINE_VERSION,
    DryRunResult,
    ImportResult,
    import_result,
    parse_chromosomes_filter,
)
from genome.imputation.reference_panel import (
    PANEL_CHROMOSOMES,
    ReferencePanel,
    default_panel_root,
    install_panel,
    validate_panel,
)
from genome.imputation.runs import (
    ImputationRun,
    list_runs,
    update_status,
)
from genome.imputation.vcf_export import (
    EXPORT_PIPELINE_VERSION,
    PreparedUpload,
    prepare_run,
)

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_R2_THRESHOLD",
    "EXPORT_PIPELINE_VERSION",
    "IMPUTATION_PIPELINE_VERSION",
    "PANEL_CHROMOSOMES",
    "DryRunResult",
    "ImportResult",
    "ImputationArchive",
    "ImputationRun",
    "PreparedUpload",
    "ReferencePanel",
    "default_panel_root",
    "import_result",
    "install_panel",
    "list_runs",
    "parse_chromosomes_filter",
    "prepare_run",
    "update_status",
    "validate_panel",
]

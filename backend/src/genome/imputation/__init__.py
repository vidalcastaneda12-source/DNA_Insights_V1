"""Phase 4 — imputation pipeline.

Public entry points the CLI calls into:

* :func:`prepare_run` builds the upload VCFs from active genotype calls.
* :func:`run_imputation` pipes each upload VCF through Beagle 5.5 against
  the local reference panel.
* :func:`import_result` streams the imputed VCFs into ``genotype_calls`` /
  ``variants_master``.
* :func:`list_runs` enumerates ``imputation_runs`` for display.
"""

from __future__ import annotations

from genome.imputation.archive import ImputationArchive
from genome.imputation.beagle_runner import (
    BEAGLE_RUNNER_VERSION,
    BeagleRunResult,
    run_imputation,
)
from genome.imputation.ingest import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_DCONF_THRESHOLD,
    DEFAULT_R2_THRESHOLD,
    IMPUTATION_PIPELINE_VERSION,
    DryRunResult,
    ImportResult,
    RegisterError,
    RegisterResult,
    import_result,
    parse_chromosomes_filter,
    register_existing_result,
)
from genome.imputation.reference_panel import (
    PANEL_CHROMOSOMES,
    ReferencePanel,
    default_panel_root,
    install_panel,
    validate_panel,
)
from genome.imputation.rsid_cleanup import normalize_imputed_rsids
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
    "BEAGLE_RUNNER_VERSION",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_DCONF_THRESHOLD",
    "DEFAULT_R2_THRESHOLD",
    "EXPORT_PIPELINE_VERSION",
    "IMPUTATION_PIPELINE_VERSION",
    "PANEL_CHROMOSOMES",
    "BeagleRunResult",
    "DryRunResult",
    "ImportResult",
    "ImputationArchive",
    "ImputationRun",
    "PreparedUpload",
    "ReferencePanel",
    "RegisterError",
    "RegisterResult",
    "default_panel_root",
    "import_result",
    "install_panel",
    "list_runs",
    "normalize_imputed_rsids",
    "parse_chromosomes_filter",
    "prepare_run",
    "register_existing_result",
    "run_imputation",
    "update_status",
    "validate_panel",
]

"""Phase 4 — TopMed imputation roundtrip.

The roundtrip is intentionally split across five CLI subcommands so the user
can pause and resume between them — TopMed processing takes hours to days
and the local state has to survive a closed laptop.

Public entry points the CLI calls into:

* :func:`prepare_run` builds the upload VCFs from active genotype calls.
* :func:`check_status` polls TopMed for status of a queued / running job.
* :func:`download_result` fetches the encrypted result archive once complete.
* :func:`import_result` streams the imputed VCFs into ``genotype_calls`` /
  ``variants_master``.
* :func:`list_runs` enumerates ``imputation_runs`` for display.

Everything goes through :mod:`genome.privacy.external_client`, so every call to
TopMed lands in ``audit_log``.
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
from genome.imputation.runs import (
    ImputationRun,
    list_runs,
    update_status,
)
from genome.imputation.topmed_client import (
    TOPMED_ENDPOINT_LABEL,
    TOPMED_PANEL,
    TopMedClient,
    TopMedStatus,
    check_status,
    download_result,
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
    "TOPMED_ENDPOINT_LABEL",
    "TOPMED_PANEL",
    "DryRunResult",
    "ImportResult",
    "ImputationArchive",
    "ImputationRun",
    "PreparedUpload",
    "TopMedClient",
    "TopMedStatus",
    "check_status",
    "download_result",
    "import_result",
    "list_runs",
    "parse_chromosomes_filter",
    "prepare_run",
    "update_status",
]

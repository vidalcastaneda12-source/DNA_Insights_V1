"""Per-source annotation loaders.

Importing this subpackage triggers the per-module ``register_loader``
side effects so the registry is populated before any CLI dispatch
runs. The parent ``genome.annotate`` package imports this subpackage at
the bottom of its own ``__init__`` for the same reason.

Sub-phase 5.1a shipped the PharmGKB loader; 5.1b adds CPIC alongside;
5.2 adds ClinVar; 5.3 adds GWAS Catalog; 5.4 adds PGS Catalog metadata;
5.5 adds gnomAD filtered allele frequencies; 5.6 adds dbSNP. Each
subsequent sub-phase adds one module and one line below it.
"""

from __future__ import annotations

from genome.annotate.loaders import (
    clinvar,  # noqa: F401 — side-effect: registers ClinVar
    cpic,  # noqa: F401 — side-effect: registers CPIC
    dbsnp,  # noqa: F401 — side-effect: registers dbSNP
    gnomad,  # noqa: F401 — side-effect: registers gnomAD
    gwas_catalog,  # noqa: F401 — side-effect: registers GWAS Catalog
    pgs_catalog,  # noqa: F401 — side-effect: registers PGS Catalog
    pharmgkb,  # noqa: F401 — side-effect: registers PharmGKB
)

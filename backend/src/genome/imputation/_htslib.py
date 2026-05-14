"""htslib log-level helpers scoped to the imputation VCF readers.

Beagle 5.5's output VCFs declare contigs only via implicit length-derived
headers that htslib does not accept as canonical, so every cyvcf2 read of
a Beagle result fires ``[W::vcf_parse] Contig 'chr<N>' is not defined in
the header`` once per record. The warning is cosmetic — the parse itself
succeeds and the records are well-formed — but it floods stderr on a
multi-million-variant import.

cyvcf2 exposes htslib's process-global log level via
``cyvcf2.cyvcf2.set_htslib_log_level``. The helper here lowers the level
to ``HTS_LOG_ERROR`` for the duration of a single imputation read and
restores ``HTS_LOG_WARNING`` (htslib's documented default) on exit. The
suppression is therefore scoped to imputation-module read paths only:
unrelated cyvcf2 readers elsewhere in the process keep htslib's default
verbosity, and real errors — truncated body, malformed records — fire at
``HTS_LOG_ERROR`` and continue to surface.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# htslib log-level constants. The module surfacing them isn't part of
# cyvcf2's public API, so we hard-code the values per htslib's own
# documentation rather than depending on a stable Python export.
_HTS_LOG_ERROR = 1
_HTS_LOG_WARNING_DEFAULT = 3


@contextmanager
def silence_htslib_contig_warnings() -> Iterator[None]:
    """Suppress htslib's per-record contig warning for one VCF read.

    Use around a ``cyvcf2.VCF`` open + iterate + close block. The level
    is restored on exit even if the body raises, so a malformed file
    that aborts iteration still leaves the global log level at the
    htslib default.
    """
    from cyvcf2.cyvcf2 import (  # noqa: PLC0415 — import deferred so this module loads without cyvcf2 at type-check time
        set_htslib_log_level,
    )

    set_htslib_log_level(_HTS_LOG_ERROR)
    try:
        yield
    finally:
        set_htslib_log_level(_HTS_LOG_WARNING_DEFAULT)


__all__ = ["silence_htslib_contig_warnings"]

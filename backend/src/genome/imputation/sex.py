"""Resolve a single profile sex for the chrX imputation path (PR 5a).

chrX imputation needs one determinate sex: the corrected-dosage view
(``consensus_chrx_dosage_v``) halves male non-PAR dosage, and the het guard
hangs off the same fact. This resolves it from an explicit ``--sex`` override
or, failing that, from the confident aggregate of the per-source chip
``sample_qc.sex_inferred`` values.

The aggregate rule is intentionally identical to the ``profile_sex`` CTE inside
``consensus_chrx_dosage_v`` — a parity test pins the two together. A confident
``'M'`` or ``'F'`` requires that exactly one of the two appears across the chip
sources with no conflict; a conflict, an all-``'ambiguous'`` set, or no chip QC
at all yields ``'ambiguous'``.

Nothing is persisted: ``--sex`` drives only the transient prepare / run /
import-QC path (CLAUDE.md locked decision — schema-free except the one carve-out
view). The all-ambiguous edge and its deferred remedy (persist to the existing
``sample_qc.sex_expected`` column and ``COALESCE`` it in the view) are written up
in finding-029; it cannot arise for the 23andMe(M) + Ancestry(ambiguous) corpus
this ships against, where the aggregate is a determinate ``'M'``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, get_args

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

ProfileSex = Literal["M", "F", "ambiguous"]
ResolvedSex = Literal["M", "F"]

# The chip sources whose per-run sex inference feeds the aggregate. Imputed runs
# carry their own inferred sex but must not vote on the profile's sex — that
# would be circular once chrX imputation lands.
CHIP_SOURCES: Final[tuple[str, ...]] = ("23andme", "ancestry")

# The same confident-aggregate rule encoded by the view's ``profile_sex`` CTE.
# Kept here as one statement so :func:`profile_sex_label` and the DDL view can
# be held to byte-for-byte parity by a test.
_PROFILE_SEX_SQL: Final[str] = """
SELECT CASE
  WHEN COUNT(*) FILTER (WHERE sq.sex_inferred = 'M') > 0
   AND COUNT(*) FILTER (WHERE sq.sex_inferred = 'F') = 0 THEN 'M'
  WHEN COUNT(*) FILTER (WHERE sq.sex_inferred = 'F') > 0
   AND COUNT(*) FILTER (WHERE sq.sex_inferred = 'M') = 0 THEN 'F'
  ELSE 'ambiguous'
END
FROM sample_qc sq
JOIN ingestion_runs ir ON ir.run_id = sq.run_id
WHERE CAST(ir.source AS VARCHAR) IN ('23andme', 'ancestry')
"""


class AmbiguousSexError(RuntimeError):
    """The profile sex could not be confidently resolved and no ``--sex`` was given."""


def _validate_explicit(explicit: str) -> ResolvedSex:
    """Normalize an explicit ``--sex`` value to ``'M'`` / ``'F'`` or raise."""
    normalized = explicit.strip().upper()
    if normalized in get_args(ResolvedSex):
        return normalized  # type: ignore[return-value]
    msg = f"--sex must be 'M', 'F', or 'auto'; got {explicit!r}"
    raise ValueError(msg)


def profile_sex_label(conn: DuckDBPyConnection, explicit: str | None = None) -> ProfileSex:
    """Resolve the profile sex, returning ``'ambiguous'`` instead of raising.

    An ``explicit`` ``'M'`` / ``'F'`` override wins outright; otherwise the
    in-SQL confident aggregate over the chip sources decides. This mirrors the
    view's ``profile_sex`` CTE exactly, so a profile the view treats as male is
    the profile this calls male. Used where a soft answer is wanted — the prepare
    step records it as provenance without blocking an ambiguous-sex profile from
    preparing its autosomes.
    """
    if explicit is not None:
        return _validate_explicit(explicit)
    row = conn.execute(_PROFILE_SEX_SQL).fetchone()
    label = str(row[0]) if row is not None and row[0] is not None else "ambiguous"
    if label in get_args(ProfileSex):
        return label  # type: ignore[return-value]
    return "ambiguous"


def resolve_sex(conn: DuckDBPyConnection, explicit: str | None = None) -> ResolvedSex:
    """Resolve a determinate ``'M'`` / ``'F'``, or raise :class:`AmbiguousSexError`.

    ``--sex`` wins; otherwise the confident chip aggregate. A conflict, an
    all-``'ambiguous'`` chip set, or no chip QC at all raises with an actionable
    message — chrX imputation needs a determinate sex to correct male non-PAR
    dosage, so the caller must disambiguate with ``--sex``.
    """
    label = profile_sex_label(conn, explicit)
    if label in get_args(ResolvedSex):
        return label  # type: ignore[return-value]
    msg = (
        "could not confidently infer profile sex from the chip sample_qc rows "
        "(the chip sources disagree, or are all 'ambiguous'); pass --sex M or "
        "--sex F. chrX imputation needs a determinate sex to correct male "
        "non-PAR dosage."
    )
    raise AmbiguousSexError(msg)


__all__ = [
    "CHIP_SOURCES",
    "AmbiguousSexError",
    "ProfileSex",
    "ResolvedSex",
    "profile_sex_label",
    "resolve_sex",
]

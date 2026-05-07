"""GRCh37 → GRCh38 coordinate lift-over.

The :class:`Liftover` Protocol lets the pipeline accept any concrete
implementation. The default engine is :class:`LiftoverPyLib` (the ``liftover``
PyPI package, CFFI/C++-backed); :class:`PyLiftoverWrapper` is a pure-Python
fallback. :class:`IdentityLiftover` is the test stub. Per CLAUDE.md the real
lift-over **must not** auto-download chain files at runtime — pass an explicit
local path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

import structlog

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = structlog.get_logger(__name__)

LiftoverEngine = Literal["auto", "liftover", "pyliftover"]


@runtime_checkable
class Liftover(Protocol):
    """Map a (chrom, 1-based pos) tuple from a source build to a target build."""

    chain_label: str

    def lift(self, chrom: str, pos: int) -> tuple[str, int] | None:
        """Return the (chrom, pos) in the target build, or ``None`` on failure."""


class IdentityLiftover:
    """Pass-through lift-over.

    Used when the input file is already in the target build, and as the default
    test stub. Coordinates are returned unchanged.
    """

    def __init__(self, chain_label: str = "identity") -> None:
        self.chain_label = chain_label

    def lift(self, chrom: str, pos: int) -> tuple[str, int] | None:
        return (chrom, pos)


def _to_ucsc(chrom: str) -> str:
    """Translate the canonical-chrom label into UCSC ``chr*`` form."""
    ucsc = chrom if chrom.startswith("chr") else f"chr{chrom}"
    if ucsc == "chrMT":
        ucsc = "chrM"
    return ucsc


def _from_ucsc(chrom: str) -> str:
    """Strip UCSC ``chr*`` prefix and remap ``chrM`` back to ``MT``."""
    clean = chrom.removeprefix("chr")
    if clean == "M":
        clean = "MT"
    return clean


class LiftoverPyLib:
    """Default engine: the ``liftover`` PyPI package (C++/CFFI).

    Roughly an order of magnitude faster than ``pyliftover`` on whole-array
    23andMe / Ancestry inputs, with identical output for canonical
    coordinates. Reads a local UCSC chain file; auto-download is **disabled**.
    """

    def __init__(self, chain_file: Path, chain_label: str | None = None) -> None:
        from liftover import ChainFile  # noqa: PLC0415 — optional dep

        if not chain_file.is_file():
            msg = f"chain file not found: {chain_file}"
            raise FileNotFoundError(msg)
        self._cf = ChainFile(str(chain_file), one_based=True)
        self.chain_label = chain_label or chain_file.stem

    def lift(self, chrom: str, pos: int) -> tuple[str, int] | None:
        ucsc = _to_ucsc(chrom)
        results: Sequence[tuple[str, int, str]] = self._cf.convert_coordinate(ucsc, pos) or ()
        if not results:
            return None
        new_chrom, new_pos, _strand = results[0]
        return (_from_ucsc(new_chrom), new_pos)


class PyLiftoverWrapper:
    """Pure-Python ``pyliftover``-backed fallback.

    Kept for environments where the ``liftover`` C++ wheel isn't available.
    Same chain-file requirement: local path, no auto-download.
    """

    def __init__(self, chain_file: Path, chain_label: str | None = None) -> None:
        from pyliftover import LiftOver  # noqa: PLC0415 — optional dep

        if not chain_file.is_file():
            msg = f"chain file not found: {chain_file}"
            raise FileNotFoundError(msg)
        self._lo = LiftOver(str(chain_file))
        self.chain_label = chain_label or chain_file.stem

    def lift(self, chrom: str, pos: int) -> tuple[str, int] | None:
        # pyliftover expects 0-based positions.
        ucsc = _to_ucsc(chrom)
        results: Sequence[tuple[str, int, str, int]] = (
            self._lo.convert_coordinate(ucsc, pos - 1) or ()
        )
        if not results:
            return None
        new_chrom, new_pos, _strand, _conv = results[0]
        return (_from_ucsc(new_chrom), new_pos + 1)


def _liftover_pkg_available() -> bool:
    try:
        import liftover  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def _pyliftover_available() -> bool:
    try:
        import pyliftover  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def _build_grch37_engine(
    chain_file: Path,
    engine: LiftoverEngine,
) -> Liftover:
    """Construct the requested engine, raising on explicit-engine unavailability."""
    if engine == "liftover":
        if not _liftover_pkg_available():
            msg = (
                "liftover engine requested but the `liftover` PyPI package is "
                "not installed. Install it (`uv add liftover`) or pick a "
                "different --liftover-engine."
            )
            raise RuntimeError(msg)
        return LiftoverPyLib(chain_file, chain_label="hg19_to_hg38")
    if engine == "pyliftover":
        if not _pyliftover_available():
            msg = (
                "pyliftover engine requested but the `pyliftover` PyPI package "
                "is not installed. Install it (`uv add pyliftover`) or pick a "
                "different --liftover-engine."
            )
            raise RuntimeError(msg)
        return PyLiftoverWrapper(chain_file, chain_label="hg19_to_hg38")
    # auto: liftover (default) → pyliftover (fallback). Loud, not silent.
    if _liftover_pkg_available():
        return LiftoverPyLib(chain_file, chain_label="hg19_to_hg38")
    if _pyliftover_available():
        logger.info(
            "liftover.engine_fallback",
            chosen="pyliftover",
            reason="liftover package not importable; using pure-Python fallback",
        )
        return PyLiftoverWrapper(chain_file, chain_label="hg19_to_hg38")
    msg = (
        "no lift-over engine available — install either `liftover` "
        "(default, faster) or `pyliftover`."
    )
    raise RuntimeError(msg)


def make_liftover(
    native_build: str,
    *,
    chain_file: Path | None = None,
    engine: LiftoverEngine = "auto",
) -> Liftover:
    """Choose the right lift-over for a file's native build.

    - Native ``GRCh38`` → :class:`IdentityLiftover` labelled ``native_grch38``
      (engine selection ignored — there's nothing to lift).
    - Native ``GRCh37`` → :class:`LiftoverPyLib` (``engine='liftover'`` or
      ``'auto'`` with the package available), :class:`PyLiftoverWrapper`
      (``engine='pyliftover'`` or ``'auto'`` with only pyliftover available);
      raises ``ValueError`` if no chain file is supplied.
    """
    if native_build == "GRCh38":
        return IdentityLiftover(chain_label="native_grch38")
    if native_build == "GRCh37":
        if chain_file is None:
            msg = (
                "GRCh37 input requires a chain file. Download UCSC's "
                "hg19ToHg38.over.chain.gz and pass --chain-file."
            )
            raise ValueError(msg)
        return _build_grch37_engine(chain_file, engine)
    msg = f"unsupported native build: {native_build!r}"
    raise ValueError(msg)

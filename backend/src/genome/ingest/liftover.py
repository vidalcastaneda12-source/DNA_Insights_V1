"""GRCh37 → GRCh38 coordinate lift-over.

The :class:`Liftover` Protocol lets the pipeline accept either the real
``pyliftover``-backed implementation (which needs a chain file on disk) or a
test/identity stub. Per CLAUDE.md the real lift-over **must not** auto-download
chain files at runtime — pass an explicit local path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


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


class PyLiftover:
    """``pyliftover``-backed lift-over against a local chain file.

    The chain file must be downloaded out of band and stored under
    ``data/chains/`` (or anywhere readable). Auto-download is **disabled**.
    """

    def __init__(self, chain_file: Path, chain_label: str | None = None) -> None:
        from pyliftover import LiftOver  # noqa: PLC0415 — optional dep

        if not chain_file.is_file():
            msg = f"chain file not found: {chain_file}"
            raise FileNotFoundError(msg)
        self._lo = LiftOver(str(chain_file))
        self.chain_label = chain_label or chain_file.stem

    def lift(self, chrom: str, pos: int) -> tuple[str, int] | None:
        # pyliftover expects 'chr1' (UCSC) and 0-based positions.
        ucsc = chrom if chrom.startswith("chr") else f"chr{chrom}"
        if ucsc == "chrMT":
            ucsc = "chrM"
        results: Sequence[tuple[str, int, str, int]] = (
            self._lo.convert_coordinate(
                ucsc,
                pos - 1,
            )
            or ()
        )
        if not results:
            return None
        new_chrom, new_pos, _strand, _conv = results[0]
        clean = new_chrom.removeprefix("chr")
        if clean == "M":
            clean = "MT"
        return (clean, new_pos + 1)


def make_liftover(
    native_build: str,
    *,
    chain_file: Path | None = None,
) -> Liftover:
    """Choose the right lift-over for a file's native build.

    - Native ``GRCh38``  → :class:`IdentityLiftover` labelled ``native_grch38``.
    - Native ``GRCh37``  → :class:`PyLiftover` against ``chain_file``; raises
      ``ValueError`` if no chain file is supplied.
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
        return PyLiftover(chain_file, chain_label="hg19_to_hg38")
    msg = f"unsupported native build: {native_build!r}"
    raise ValueError(msg)

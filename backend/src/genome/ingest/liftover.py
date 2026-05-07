"""GRCh37 → GRCh38 coordinate lift-over.

The :class:`Liftover` Protocol lets the pipeline accept any of three
implementations:

- :class:`IdentityLiftover` — passthrough used for native-GRCh38 inputs and as
  the default test stub. Coordinates are returned unchanged.
- :class:`BcftoolsLiftover` — default for GRCh37 inputs. Calls
  ``bcftools +liftover`` as a subprocess against the supplied chain file,
  pre-computes a coordinate map for a batch of ``(chrom, pos)`` queries, and
  answers per-variant ``lift()`` calls from an in-memory cache. ~150x faster
  than :class:`PyLiftover` on real 23andMe data (~20 minutes → under one
  minute for 631K variants), and ~30x faster than :class:`PyLiftover` will be
  on the ~30M imputed variants Phase 4 produces.
- :class:`PyLiftover` — pure-Python fallback against the ``pyliftover``
  package. Kept for testing, comparison, and environments where ``bcftools``
  isn't installed. Selectable via ``--liftover-engine pyliftover``.

Per CLAUDE.md the real lift-over **must not** auto-download chain files at
runtime — pass an explicit local path.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from contextlib import AbstractContextManager
    from typing import IO

logger = structlog.get_logger(__name__)

LiftoverEngine = Literal["auto", "bcftools", "pyliftover"]

_FASTA_LINE_WIDTH: Final[int] = 60
_FASTA_BUFFER_LINES: Final[int] = 1024  # ~64 KiB per write
_BCFTOOLS_MIN_VERSION: Final[str] = "1.19"


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
    """Internal chromosome label → UCSC label (``'1'`` → ``'chr1'``, ``'MT'`` → ``'chrM'``)."""
    ucsc = chrom if chrom.startswith("chr") else f"chr{chrom}"
    if ucsc == "chrMT":
        ucsc = "chrM"
    return ucsc


def _from_ucsc(chrom: str) -> str:
    """UCSC chromosome label → internal label (``'chr1'`` → ``'1'``, ``'chrM'`` → ``'MT'``)."""
    clean = chrom.removeprefix("chr")
    if clean == "M":
        clean = "MT"
    return clean


class PyLiftover:
    """``pyliftover``-backed lift-over against a local chain file.

    Pure-Python and roughly 100-200x slower than :class:`BcftoolsLiftover` at
    the scale of a full 23andMe export. Kept available as a fallback for
    environments where ``bcftools`` isn't installed and for cross-checking
    results during debugging via ``--liftover-engine pyliftover``.
    """

    def __init__(self, chain_file: Path, chain_label: str | None = None) -> None:
        from pyliftover import LiftOver  # noqa: PLC0415 — optional dep

        if not chain_file.is_file():
            msg = f"chain file not found: {chain_file}"
            raise FileNotFoundError(msg)
        self._lo = LiftOver(str(chain_file))
        self.chain_label = chain_label or chain_file.stem

    def lift(self, chrom: str, pos: int) -> tuple[str, int] | None:
        # pyliftover expects UCSC labels and 0-based positions.
        ucsc = _to_ucsc(chrom)
        results: Sequence[tuple[str, int, str, int]] = (
            self._lo.convert_coordinate(ucsc, pos - 1) or ()
        )
        if not results:
            return None
        new_chrom, new_pos, _strand, _conv = results[0]
        return (_from_ucsc(new_chrom), new_pos + 1)


def _open_chain(chain_file: Path) -> AbstractContextManager[IO[str]]:
    """Open a chain file, transparently handling gzip."""
    if str(chain_file).endswith(".gz"):
        return gzip.open(chain_file, "rt")
    return chain_file.open("rt")


def _parse_chain_contigs(chain_file: Path) -> tuple[dict[str, int], dict[str, int]]:
    """Return ``({source_contig: size}, {destination_contig: size})`` for every chain.

    Chain file format: each chain header line begins with ``chain`` and has 13
    space-separated fields:
    ``chain score tName tSize tStrand tStart tEnd qName qSize qStrand qStart qEnd id``.
    ``tName`` / ``tSize`` are the source contig (target — e.g. hg19 in an
    ``hg19ToHg38`` chain), ``qName`` / ``qSize`` are the destination contig
    (query — e.g. hg38). Multiple chains may share a contig; we take the
    first size we see (sizes are stable per contig).
    """
    sources: dict[str, int] = {}
    destinations: dict[str, int] = {}
    chain_min_fields = 13
    with _open_chain(chain_file) as fh:
        for line in fh:
            if not line.startswith("chain "):
                continue
            parts = line.rstrip("\n").split()
            if len(parts) < chain_min_fields:
                continue
            try:
                tsize = int(parts[3])
                qsize = int(parts[8])
            except ValueError:
                continue
            sources.setdefault(parts[2], tsize)
            destinations.setdefault(parts[7], qsize)
    return sources, destinations


def _write_synthetic_fasta(path: Path, sizes: dict[str, int]) -> None:
    """Write a FASTA filled with ``N`` for every (contig, size) pair.

    bcftools +liftover requires a destination FASTA to determine contig
    lengths and validate REF alleles after the lift. Our pipeline does
    coordinate-only lift (we re-key REF/ALT alphabetically downstream), so we
    feed it a synthetic FASTA whose every base is ``N``. Combined with
    ``REF=N`` placeholders in the input VCF this satisfies the plugin's
    REF-match check while preserving the chain-derived coordinate mapping.

    Compresses well in transit (~3 GiB → ~10 MiB bgzipped) but we keep it
    plain so bcftools/htslib auto-indexes it without an extra bgzip step.
    """
    line = b"N" * _FASTA_LINE_WIDTH + b"\n"
    chunk = line * _FASTA_BUFFER_LINES
    with path.open("wb") as fh:
        for name, size in sizes.items():
            fh.write(f">{name}\n".encode())
            full_lines, remainder = divmod(size, _FASTA_LINE_WIDTH)
            big_blocks, leftover_lines = divmod(full_lines, _FASTA_BUFFER_LINES)
            for _ in range(big_blocks):
                fh.write(chunk)
            if leftover_lines:
                fh.write(line * leftover_lines)
            if remainder:
                fh.write(b"N" * remainder + b"\n")


class BcftoolsLiftover:
    """Batch lift-over via ``bcftools +liftover`` as a subprocess.

    Workflow:

    1. ``__init__`` validates the chain file, locates ``bcftools`` on
       ``$PATH``, and builds a synthetic destination FASTA (all ``N``) under a
       ``tempfile.TemporaryDirectory``. The FASTA's contig names and sizes
       come from the chain file's ``qName`` / ``qSize`` columns so every
       destination contig the chain might map to — including non-canonical
       ones like ``chr4_GL000008v2_random`` — exists in the FASTA. That keeps
       ``normalize.py``'s post-lift non-canonical filter accounting (the
       ``variants_dropped_lift_to_non_canonical`` counter) intact regardless
       of which lift engine is in use.
    2. :meth:`prepare` writes the batch as a minimal VCF (``REF=ALT=N``,
       INFO/KEY preserves the source ``chrom:pos``), pipes it through
       ``bcftools +liftover``, and parses the output VCF into
       ``self._cache``. Variants in ``--reject`` get cached as ``None``.
    3. :meth:`lift` answers from the cache. Calling :meth:`lift` for a
       coordinate that wasn't in :meth:`prepare` falls back to a one-shot
       subprocess invocation, which is correct but slow — always pre-prepare
       in production.

    Same chain-file argument as :class:`PyLiftover`; the public Liftover
    Protocol is preserved.
    """

    chain_label: str

    def __init__(
        self,
        chain_file: Path,
        chain_label: str | None = None,
        *,
        bcftools_executable: str = "bcftools",
    ) -> None:
        if not chain_file.is_file():
            msg = f"chain file not found: {chain_file}"
            raise FileNotFoundError(msg)
        if shutil.which(bcftools_executable) is None:
            msg = (
                f"{bcftools_executable!r} is not on $PATH. Install it "
                f"(Ubuntu/WSL: 'sudo apt install -y bcftools', minimum "
                f"version {_BCFTOOLS_MIN_VERSION}) or pick a different "
                "--liftover-engine."
            )
            raise FileNotFoundError(msg)
        self._chain_file = chain_file
        self._bcftools = bcftools_executable
        self.chain_label = chain_label or chain_file.stem
        self._cache: dict[tuple[str, int], tuple[str, int] | None] = {}
        # Source contig sizes are needed for ##contig declarations in every
        # input VCF. Destination sizes are needed for the synthetic FASTA.
        # Both come from the chain file — one parse covers both.
        self._source_contigs, self._destination_contigs = _parse_chain_contigs(
            chain_file,
        )
        if not self._destination_contigs:
            msg = (
                f"chain file {chain_file} contained no parseable chain "
                "headers; cannot prepare a bcftools-backed lift-over"
            )
            raise ValueError(msg)
        # Synthetic FASTA gets built lazily on first prepare(); the
        # TemporaryDirectory keeps it alive for the lifetime of the instance
        # and cleans up automatically on GC / interpreter exit.
        self._tmp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._synthetic_fasta: Path | None = None

    # --- public API ---

    def prepare(self, coords: Iterable[tuple[str, int]]) -> None:
        """Pre-compute coordinate lifts for a batch.

        Idempotent: coordinates already cached are skipped. The first call
        constructs the synthetic destination FASTA (one-time cost ~5-10 s for
        a full hg19→hg38 chain). Subsequent calls just stream the new batch
        through bcftools.
        """
        new = sorted(
            {(c, p) for c, p in coords if (c, p) not in self._cache},
        )
        if not new:
            return
        self._lift_batch(new)

    def lift(self, chrom: str, pos: int) -> tuple[str, int] | None:
        key = (chrom, pos)
        if key not in self._cache:
            # One-shot fallback. Correct but slow; the pipeline pre-calls
            # prepare() to amortize the subprocess cost across the full
            # batch.
            self._lift_batch([key])
        return self._cache.get(key)

    # --- internals ---

    def _ensure_synthetic_fasta(self) -> Path:
        if self._synthetic_fasta is not None:
            return self._synthetic_fasta
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="genome_liftover_")
        fa_path = Path(self._tmp_dir.name) / "synthetic.fa"
        _write_synthetic_fasta(fa_path, self._destination_contigs)
        self._synthetic_fasta = fa_path
        logger.info(
            "bcftools_liftover.synthetic_fasta_built",
            path=str(fa_path),
            contigs=len(self._destination_contigs),
        )
        return fa_path

    def _lift_batch(self, coords: Sequence[tuple[str, int]]) -> None:
        fasta = self._ensure_synthetic_fasta()
        with tempfile.TemporaryDirectory(
            prefix="genome_liftover_run_",
        ) as run_dir:
            run = Path(run_dir)
            in_vcf = run / "in.vcf"
            out_vcf = run / "out.vcf"
            reject_vcf = run / "reject.vcf"
            self._write_input_vcf(in_vcf, coords)
            self._run_bcftools(in_vcf, out_vcf, reject_vcf, fasta)
            lifted = self._parse_output_vcf(out_vcf)
        # Anything we sent in but didn't see in the output → lift failed.
        for coord in coords:
            self._cache[coord] = lifted.get(coord)

    def _write_input_vcf(
        self,
        path: Path,
        coords: Sequence[tuple[str, int]],
    ) -> None:
        # bcftools refuses to lift variants whose contig isn't declared in
        # the VCF header (it logs "Contig 'chr1' is not defined in the
        # header"). Emit one ##contig line per source contig in the chain.
        with path.open("w") as fh:
            fh.write("##fileformat=VCFv4.2\n")
            for name, size in self._source_contigs.items():
                fh.write(f"##contig=<ID={name},length={size}>\n")
            fh.write(
                "##INFO=<ID=KEY,Number=1,Type=String,"
                'Description="genome.ingest source chrom:pos">\n',
            )
            fh.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            for chrom, pos in coords:
                ucsc = _to_ucsc(chrom)
                fh.write(f"{ucsc}\t{pos}\t.\tN\tN\t.\t.\tKEY={chrom}:{pos}\n")

    def _run_bcftools(
        self,
        in_vcf: Path,
        out_vcf: Path,
        reject_vcf: Path,
        fasta: Path,
    ) -> None:
        cmd = [
            self._bcftools,
            "+liftover",
            "--no-version",
            str(in_vcf),
            "--",
            "--fasta-ref",
            str(fasta),
            "--chain",
            str(self._chain_file),
            "--reject",
            str(reject_vcf),
            "--reject-type",
            "v",
            "--no-left-align",
        ]
        with out_vcf.open("w") as out_fh:
            result = subprocess.run(  # noqa: S603 — argv built from validated paths
                cmd,
                check=False,
                stdout=out_fh,
                stderr=subprocess.PIPE,
                text=True,
            )
        if result.returncode != 0:
            msg = f"bcftools +liftover failed (rc={result.returncode}): {result.stderr.strip()}"
            raise RuntimeError(msg)

    @staticmethod
    def _parse_output_vcf(
        path: Path,
    ) -> dict[tuple[str, int], tuple[str, int]]:
        """Return ``{(src_chrom, src_pos): (dst_chrom, dst_pos)}`` for every lifted row."""
        lifted: dict[tuple[str, int], tuple[str, int]] = {}
        info_min_columns = 8
        with path.open() as fh:
            for line in fh:
                if not line or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < info_min_columns:
                    continue
                lifted_chrom = _from_ucsc(parts[0])
                try:
                    lifted_pos = int(parts[1])
                except ValueError:
                    continue
                src_key = _extract_info_key(parts[7])
                if src_key is None:
                    continue
                lifted[src_key] = (lifted_chrom, lifted_pos)
        return lifted


def _extract_info_key(info: str) -> tuple[str, int] | None:
    for field in info.split(";"):
        if field.startswith("KEY="):
            value = field[len("KEY=") :]
            chrom, _, pos_s = value.partition(":")
            if not chrom or not pos_s:
                return None
            try:
                return (chrom, int(pos_s))
            except ValueError:
                return None
    return None


def make_liftover(
    native_build: str,
    *,
    chain_file: Path | None = None,
    engine: LiftoverEngine = "auto",
) -> Liftover:
    """Choose the right lift-over for a file's native build.

    - Native ``GRCh38`` → :class:`IdentityLiftover` labelled ``native_grch38``.
    - Native ``GRCh37``:
        - ``engine='auto'`` (default): :class:`BcftoolsLiftover` if ``bcftools``
          is on ``$PATH``, otherwise :class:`PyLiftover` with a warning log.
        - ``engine='bcftools'``: forces :class:`BcftoolsLiftover`; raises if
          ``bcftools`` is missing.
        - ``engine='pyliftover'``: forces :class:`PyLiftover`. Useful for
          comparison / fallback.
    """
    if native_build == "GRCh38":
        return IdentityLiftover(chain_label="native_grch38")
    if native_build != "GRCh37":
        msg = f"unsupported native build: {native_build!r}"
        raise ValueError(msg)
    if chain_file is None:
        msg = (
            "GRCh37 input requires a chain file. Download UCSC's "
            "hg19ToHg38.over.chain.gz and pass --chain-file."
        )
        raise ValueError(msg)

    if engine == "pyliftover":
        return PyLiftover(chain_file, chain_label="hg19_to_hg38")
    if engine == "bcftools":
        return BcftoolsLiftover(chain_file, chain_label="hg19_to_hg38")
    if engine == "auto":
        if shutil.which("bcftools") is not None:
            return BcftoolsLiftover(chain_file, chain_label="hg19_to_hg38")
        logger.warning(
            "make_liftover.bcftools_missing_falling_back",
            engine="pyliftover",
        )
        return PyLiftover(chain_file, chain_label="hg19_to_hg38")
    msg = f"unsupported liftover engine: {engine!r}"
    raise ValueError(msg)

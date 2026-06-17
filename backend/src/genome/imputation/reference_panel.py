"""Local Beagle reference-panel management.

This module owns the on-disk layout for the artifacts the Phase 4 Beagle
runner depends on: the Beagle JAR, the PLINK GRCh38 genetic map, and the
1000 Genomes Phase 3 reference panel (one VCF per chromosome).

Default layout under ``<panel_root>/`` (``panel_root`` defaults to
``~/.cache/genome/imputation/``)::

    <panel_root>/
        beagle.jar                       # symlink-like name; the underlying
                                         # downloaded file keeps its dated
                                         # filename so version is visible
        genetic_maps/                    # extracted from plink.GRCh38.map.zip
            plink.chr1.GRCh38.map
            plink.chr2.GRCh38.map
            ...
            plink.chrX.GRCh38.map
        panel/                           # per-chromosome 1000G Phase 3 VCFs
            chr1.vcf.gz
            chr2.vcf.gz
            ...
            chr22.vcf.gz
            chrX.vcf.gz
            (chrY: not present — see below)

Notes:

* ``panel_root`` lives outside ``data/`` deliberately so the panel survives
  ``rm -rf data/`` rebuilds (per the CLAUDE.md schema-change convention).
* Beagle 5.5 accepts both ``.bref3`` and ``.vcf.gz`` for its ``ref=`` argument.
  The Beagle authors only host pre-built bref3 reference panels for **b37**
  (GRCh37) at ``bochet.gcc.biostat.washington.edu/beagle/1000_Genomes_phase3_v5a/b37.bref3/``.
  For GRCh38 we fetch the 1000 Genomes high-coverage Phase 3 phased VCFs
  from EBI's FTP and feed them to Beagle as VCFs. Conversion to bref3 (via
  the bref3 utility JAR) is a future optimization owned by the Beagle runner.
* The high-coverage phased release does not include chrY. The prepare step
  may still produce a chrY VCF from the user's data, but the run step is
  expected to skip chrY with a clear log message. ``panel_for_chrom('Y')``
  returns ``None`` to make this state explicit.
* Every download flows through :class:`genome.privacy.external_client.ExternalClient`,
  so each fetch is enable-checked and audit-logged. Files land with
  ``0600`` permissions; directories with ``0700``.
"""

from __future__ import annotations

import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import structlog

from genome.config import get_settings
from genome.privacy.external_client import ExternalClient

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Resolved download URLs (verified 2026-05-13).
#
# Sources:
#   * Beagle 5.5 JAR + bref3 utility:
#       https://faculty.washington.edu/browning/beagle/beagle.html
#       (build dated 27Feb25.75f, current as of late 2024 / early 2025).
#   * PLINK GRCh38 genetic map:
#       https://bochet.gcc.biostat.washington.edu/beagle/genetic_maps/
#       (file dated 2025-11-03; contains plink.chr{N}.GRCh38.map for
#       autosomes 1..22 and X).
#   * 1000 Genomes Phase 3 GRCh38 reference panel:
#       Beagle authors host pre-built bref3 panels only for b37. For
#       GRCh38 the canonical source is the 1000 Genomes high-coverage
#       phased release on EBI's FTP, which Beagle 5.5 accepts as VCF
#       ref= input. chrY is not part of this release.
# ---------------------------------------------------------------------------

URL_VERIFIED_DATE: Final[str] = "2026-05-13"

_BEAGLE_JAR_FILENAME: Final[str] = "beagle.27Feb25.75f.jar"
BEAGLE_JAR_URL: Final[str] = (
    f"https://faculty.washington.edu/browning/beagle/{_BEAGLE_JAR_FILENAME}"
)

_GENETIC_MAP_ARCHIVE_FILENAME: Final[str] = "plink.GRCh38.map.zip"
GENETIC_MAP_URL: Final[str] = (
    f"https://bochet.gcc.biostat.washington.edu/beagle/genetic_maps/{_GENETIC_MAP_ARCHIVE_FILENAME}"
)

_PANEL_BASE_URL: Final[str] = (
    "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/data_collections/"
    "1000G_2504_high_coverage/working/20201028_3202_phased"
)
_AUTOSOME_PANEL_FILENAME: Final[str] = (
    "CCDG_14151_B01_GRM_WGS_2020-08-05_chr{chrom}.filtered.shapeit2-duohmm-phased.vcf.gz"
)
_CHRX_PANEL_FILENAME: Final[str] = (
    "CCDG_14151_B01_GRM_WGS_2020-08-05_chrX.filtered.eagle2-phased.v2.vcf.gz"
)

EXTERNAL_ENDPOINT_LABEL: Final[str] = "beagle_panel"
"""Single endpoint label so all panel-related audit rows group together."""

PANEL_AUTOSOMES: Final[frozenset[str]] = frozenset(str(i) for i in range(1, 23))
PANEL_SEX_CHROMS: Final[frozenset[str]] = frozenset({"X"})
PANEL_CHROMOSOMES: Final[frozenset[str]] = PANEL_AUTOSOMES | PANEL_SEX_CHROMS
"""Chromosomes carried by the reference panel. chrY is intentionally absent."""

_OWNER_RW_ONLY: Final[int] = stat.S_IRUSR | stat.S_IWUSR
_OWNER_RWX_ONLY: Final[int] = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR

# PLINK's GRCh38 genetic maps label the sex chromosomes numerically in column 1
# (23 = X, 24 = Y); Beagle expects ``chrX`` / ``chrY``. The normalizer applies
# this mapping after stripping any (possibly doubled) ``chr`` prefix.
_PLINK_SEX_CHROM: Final[dict[str, str]] = {"23": "X", "24": "Y"}


def default_panel_root() -> Path:
    """Resolve the reference-panel root directory.

    Reads ``settings.imputation_panel_root`` first; falls back to
    ``~/.cache/genome/imputation/`` when the setting is unset. The fall-back
    location is chosen so the panel survives ``rm -rf data/`` rebuilds, which
    the project's schema-change convention requires periodically.
    """
    settings = get_settings()
    if settings.imputation_panel_root is not None:
        return Path(settings.imputation_panel_root)
    return Path.home() / ".cache" / "genome" / "imputation"


def _panel_url_for_chrom(chrom: str) -> str:
    """Build the upstream URL for one chromosome's panel VCF."""
    if chrom == "X":
        filename = _CHRX_PANEL_FILENAME
    else:
        filename = _AUTOSOME_PANEL_FILENAME.format(chrom=chrom)
    return f"{_PANEL_BASE_URL}/{filename}"


@dataclass(frozen=True, slots=True)
class ReferencePanel:
    """On-disk layout for the Beagle reference panel.

    Pure value object — constructing one does not check the filesystem or
    create directories. Use :meth:`resolve` for the canonical layout under
    a given root. Use :func:`validate_panel` to check whether the layout is
    fully populated, and :func:`install_panel` to fetch missing pieces.
    """

    root: Path
    beagle_jar: Path
    genetic_map_dir: Path
    per_chrom_panels: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def resolve(cls, root: Path | None = None) -> ReferencePanel:
        """Build a panel object for ``root`` (or :func:`default_panel_root`).

        The returned layout uses the dated Beagle JAR filename (so the
        version is visible from ``ls``) and a flat per-chromosome panel
        directory. No I/O — paths are computed, not checked.
        """
        actual_root = Path(root) if root is not None else default_panel_root()
        panel_dir = actual_root / "panel"
        per_chrom = {chrom: panel_dir / f"chr{chrom}.vcf.gz" for chrom in PANEL_CHROMOSOMES}
        return cls(
            root=actual_root,
            beagle_jar=actual_root / _BEAGLE_JAR_FILENAME,
            genetic_map_dir=actual_root / "genetic_maps",
            per_chrom_panels=per_chrom,
        )

    @property
    def panel_dir(self) -> Path:
        """Per-chromosome panel directory (``<root>/panel/``)."""
        return self.root / "panel"

    @property
    def chrx_par1_panel(self) -> Path:
        """Native PAR1 chrX panel subset (``<panel>/chrX.par1.vcf.gz``, PR 5a / M3).

        Produced by ``genome imputation panel prepare-chrx`` via ``bcftools view
        -r``; the runner points the PAR1 chrX ``ref=`` here. Un-diploidized — PAR
        is already diploid in both sexes. Computed, not checked.
        """
        return self.panel_dir / "chrX.par1.vcf.gz"

    @property
    def chrx_nonpar_panel(self) -> Path:
        """Native non-PAR chrX panel subset (``<panel>/chrX.nonpar.vcf.gz``, PR 5a / M3).

        The biologically-faithful subset: male haplotypes stay haploid, so Beagle
        imputes the non-PAR core against the real (un-doubled) reference (the
        field-standard approach M3-physical restores — see finding-029). Computed,
        not checked.
        """
        return self.panel_dir / "chrX.nonpar.vcf.gz"

    @property
    def chrx_par2_panel(self) -> Path:
        """Native PAR2 chrX panel subset (``<panel>/chrX.par2.vcf.gz``, PR 5a / M3)."""
        return self.panel_dir / "chrX.par2.vcf.gz"

    @property
    def genetic_map_archive(self) -> Path:
        """Path to the cached PLINK map zip (``<root>/plink.GRCh38.map.zip``).

        Kept on disk after extraction so a re-extract can run without a
        network round-trip and so :func:`validate_panel` can short-circuit
        the download check on full installs.
        """
        return self.root / _GENETIC_MAP_ARCHIVE_FILENAME

    def panel_for_chrom(self, chrom: str) -> Path | None:
        """Return the panel VCF for ``chrom``, or ``None`` if absent (chrY).

        The returned path is where the panel *should* live — it may not
        exist on disk yet. Callers that need a present file should pair
        this with :func:`validate_panel`.
        """
        return self.per_chrom_panels.get(chrom)

    def map_for_chrom(self, chrom: str) -> Path:
        """Return the per-chromosome PLINK map path inside ``genetic_map_dir``.

        Always returns a Path (the file may not exist yet). ``chrom='X'`` is
        translated to the ``plink.chrX.GRCh38.map`` filename inside the
        archive; autosomes use ``plink.chr<N>.GRCh38.map``.
        """
        return self.genetic_map_dir / f"plink.chr{chrom}.GRCh38.map"

    def ensure_layout(self) -> None:
        """Create the panel directory tree with ``0700`` permissions."""
        for d in (self.root, self.genetic_map_dir, self.panel_dir):
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(_OWNER_RWX_ONLY)


def validate_panel(panel: ReferencePanel) -> list[str]:
    """Return a list of human-readable problems with ``panel`` on disk.

    Empty list means everything is present. The list is intentionally a
    list of strings rather than a structured object so the CLI can echo it
    verbatim. Order: Beagle JAR first, then genetic-map files (sorted by
    chromosome), then panel VCFs (sorted by chromosome).
    """
    problems: list[str] = []
    if not panel.beagle_jar.is_file():
        problems.append(f"missing Beagle JAR: {panel.beagle_jar}")
    for chrom in sorted(PANEL_CHROMOSOMES, key=_chrom_sort_key):
        map_path = panel.map_for_chrom(chrom)
        if not map_path.is_file():
            problems.append(f"missing genetic map for chr{chrom}: {map_path}")
    for chrom in sorted(PANEL_CHROMOSOMES, key=_chrom_sort_key):
        panel_path = panel.panel_for_chrom(chrom)
        if panel_path is None:
            continue
        if not panel_path.is_file():
            problems.append(f"missing panel VCF for chr{chrom}: {panel_path}")
    chrx_problem = _chrx_map_col1_problem(panel)
    if chrx_problem is not None:
        problems.append(chrx_problem)
    return problems


def _chrx_map_col1_problem(panel: ReferencePanel) -> str | None:
    """Return a problem string if the chrX genetic map's column 1 isn't ``chrX``.

    Beagle matches chromosome labels by exact string, so a chrX map whose
    column 1 is the bare PLINK ``23`` (or a doubled ``chrchrX``) silently fails
    to line up with the ``chrX`` reference panel. :func:`normalize_map_chrom`
    fixes this at install time; this positively asserts the result so a stale or
    hand-mangled map is caught before a wasted Beagle run. Returns ``None`` when
    the map is absent (the missing-file check already reports that) or when its
    first data line is correct.
    """
    path = panel.map_for_chrom("X")
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return f"unreadable chrX genetic map: {path}"
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        col1 = line.split("\t", 1)[0]
        if col1 != "chrX":
            return (
                f"chrX genetic map column 1 is {col1!r}, expected 'chrX' "
                f"(Beagle matches chromosome labels by exact string): {path}"
            )
        return None
    return None


def install_panel(
    panel: ReferencePanel,
    *,
    force: bool = False,
    chromosomes: frozenset[str] | None = None,
) -> None:
    """Download any missing panel artifacts via the audited external client.

    Idempotent: artifacts that already exist on disk are left alone unless
    ``force=True``. When ``chromosomes`` is set, only that subset of
    per-chromosome panel VCFs is downloaded — the Beagle JAR and the
    PLINK genetic-map archive are left alone, which is the right behaviour
    for partial-install / recovery use.

    Every fetch goes through :class:`ExternalClient`, so a disabled master
    switch raises :class:`ExternalCallsDisabledError` (after writing an
    audit row), and each download produces an intent + result audit row
    pair with ``resource_type='reference_panel'`` and ``resource_id`` set
    to ``'jar'``, ``'map'``, or the chromosome label.
    """
    log = logger.bind(root=str(panel.root), force=force)
    panel.ensure_layout()

    install_all = chromosomes is None
    selected_chroms = PANEL_CHROMOSOMES if install_all else frozenset(chromosomes or ())
    unknown = selected_chroms - PANEL_CHROMOSOMES
    if unknown:
        msg = (
            f"unknown panel chromosome(s) {sorted(unknown)}; "
            f"valid chromosomes are {sorted(PANEL_CHROMOSOMES, key=_chrom_sort_key)}"
        )
        raise ValueError(msg)

    with ExternalClient(EXTERNAL_ENDPOINT_LABEL) as client:
        if install_all:
            _install_beagle_jar(client, panel, force=force, log=log)
            _install_genetic_map(client, panel, force=force, log=log)
        for chrom in sorted(selected_chroms, key=_chrom_sort_key):
            _install_panel_vcf(client, panel, chrom, force=force, log=log)

    log.info("reference_panel.install_complete", selected=sorted(selected_chroms))


def _install_beagle_jar(
    client: ExternalClient,
    panel: ReferencePanel,
    *,
    force: bool,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Download the Beagle JAR if missing (or always when ``force``)."""
    if panel.beagle_jar.is_file() and not force:
        log.debug("reference_panel.skip_existing", artifact="beagle_jar")
        return
    log.info("reference_panel.download.start", artifact="beagle_jar", url=BEAGLE_JAR_URL)
    client.download(
        BEAGLE_JAR_URL,
        str(panel.beagle_jar),
        resource_type="reference_panel",
        resource_id="jar",
    )
    panel.beagle_jar.chmod(_OWNER_RW_ONLY)


def _install_genetic_map(
    client: ExternalClient,
    panel: ReferencePanel,
    *,
    force: bool,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Download the PLINK genetic-map archive and extract its contents.

    The archive itself is also kept on disk so a future extraction can run
    without another network round-trip. Per-file permissions inside the
    extracted directory are tightened to ``0600``.

    After download/extract — and on the pure skip path too — the on-disk maps
    are normalized to Beagle's exact-string chromosome labels via
    :func:`_normalize_on_disk_maps`, and any stray doubled-prefix files are
    cleared. Both steps are idempotent, so an already-correct install is left
    byte-identical.
    """
    archive = panel.genetic_map_archive
    needs_download = force or not archive.is_file()
    needs_extract = force or _missing_map_files(panel)

    if needs_download:
        log.info(
            "reference_panel.download.start",
            artifact="genetic_map",
            url=GENETIC_MAP_URL,
        )
        client.download(
            GENETIC_MAP_URL,
            str(archive),
            resource_type="reference_panel",
            resource_id="map",
        )
        archive.chmod(_OWNER_RW_ONLY)

    if needs_extract:
        log.info("reference_panel.extract.start", artifact="genetic_map")
        panel.genetic_map_dir.mkdir(parents=True, exist_ok=True)
        panel.genetic_map_dir.chmod(_OWNER_RWX_ONLY)
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Flatten any internal directory structure — we want the .map
                # files to land directly in genetic_map_dir.
                member_name = Path(info.filename).name
                if not member_name.endswith(".map"):
                    continue
                dest = panel.genetic_map_dir / member_name
                with zf.open(info) as src, open(dest, "wb") as out:  # noqa: PTH123
                    out.writelines(iter(lambda: src.read(1 << 16), b""))
                dest.chmod(_OWNER_RW_ONLY)

    # Normalize column-1 labels (and clear any stray doubled-prefix files) on
    # every install, including the no-download skip path, so a previously bare
    # (`23`) or doubled (`chrchr22`) label is repaired without a re-download.
    if panel.genetic_map_dir.is_dir():
        _remove_doubled_map_files(panel, log)
        _normalize_on_disk_maps(panel, log)

    if not needs_download and not needs_extract:
        log.debug("reference_panel.skip_existing", artifact="genetic_map")


def _install_panel_vcf(
    client: ExternalClient,
    panel: ReferencePanel,
    chrom: str,
    *,
    force: bool,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Download one chromosome's panel VCF if missing."""
    dest = panel.panel_for_chrom(chrom)
    if dest is None:
        # PANEL_CHROMOSOMES guards this, but be defensive — the caller may
        # have passed a chromosome that simply isn't in our panel set.
        log.warning("reference_panel.skip_unknown_chrom", chrom=chrom)
        return
    if dest.is_file() and not force:
        log.debug("reference_panel.skip_existing", artifact="panel_vcf", chrom=chrom)
        return
    url = _panel_url_for_chrom(chrom)
    log.info("reference_panel.download.start", artifact="panel_vcf", chrom=chrom, url=url)
    client.download(
        url,
        str(dest),
        resource_type="reference_panel",
        resource_id=chrom,
    )
    dest.chmod(_OWNER_RW_ONLY)


def normalize_map_chrom(col1: str) -> str:
    """Canonicalize a genetic-map column-1 chromosome label to Beagle's form.

    Beagle 5.5 does exact-string chromosome matching between the genetic map,
    the reference panel, and the input VCF — all of which use a single-``chr``-
    prefixed label (``chr1`` … ``chr22``, ``chrX``). The Browning Lab's PLINK
    GRCh38 maps instead ship bare PLINK-numeric labels (``1`` … ``22``, ``23``
    for X), and a buggy earlier rewrite could leave doubled ``chrchr`` prefixes.

    The normalizer is total and idempotent:

    1. strip *every* leading ``chr`` (so ``chrchrX`` and ``chr23`` both reduce);
    2. map the PLINK sex-chromosome numbers ``23`` → ``X`` and ``24`` → ``Y``;
    3. re-emit exactly one ``chr`` prefix.

    Re-running it on its own output is a fixed point (``chrX`` → ``chrX``), which
    is what lets :func:`_install_genetic_map` apply it unconditionally — even on
    the no-download path — to repair any previously-doubled or bare label.
    """
    core = col1.strip()
    while core[:3].lower() == "chr":
        core = core[3:]
    core = _PLINK_SEX_CHROM.get(core, core)
    return f"chr{core}"


def _normalize_map_file(
    path: Path,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Rewrite ``path`` so column 1 of every data line is :func:`normalize_map_chrom`'d.

    Idempotent: a line whose column 1 already canonicalizes to itself is left
    untouched, so re-running on a normalized file is a no-op and the file stays
    byte-identical. Blank and comment (``#``-prefixed, defensive) lines pass
    through verbatim. The rewrite is atomic (write ``<path>.tmp`` then rename)
    and preserves the ``0600`` permission.

    Returns True when the file was rewritten, False when no rewrite was needed.
    """
    text = path.read_bytes().decode("ascii")
    out_chunks: list[str] = []
    changed = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            out_chunks.append(line)
            continue
        tab_idx = stripped.find("\t")
        col1 = stripped if tab_idx == -1 else stripped[:tab_idx]
        rest = "" if tab_idx == -1 else stripped[tab_idx:]
        ending = line[len(stripped) :]
        new_col1 = normalize_map_chrom(col1)
        if new_col1 == col1:
            out_chunks.append(line)
            continue
        out_chunks.append(f"{new_col1}{rest}{ending}")
        changed = True
    if not changed:
        return False
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes("".join(out_chunks).encode("ascii"))
    tmp.chmod(_OWNER_RW_ONLY)
    tmp.replace(path)
    log.info("reference_panel.genetic_map.normalized", path=str(path))
    return True


def _normalize_on_disk_maps(
    panel: ReferencePanel,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Normalize column 1 of every canonical per-chromosome map present on disk."""
    for chrom in sorted(PANEL_CHROMOSOMES, key=_chrom_sort_key):
        path = panel.map_for_chrom(chrom)
        if path.is_file():
            _normalize_map_file(path, log)


def _remove_doubled_map_files(
    panel: ReferencePanel,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Delete stray ``plink.chrchr*.GRCh38.map`` files from a buggy past rewrite.

    The canonical per-chromosome filenames are derived from canonical labels
    (:meth:`ReferencePanel.map_for_chrom`), so the live code never writes a
    doubled-``chr`` filename; this clears the residue an earlier version left
    behind. Idempotent — a clean directory yields no matches.
    """
    if not panel.genetic_map_dir.is_dir():
        return
    for stray in sorted(panel.genetic_map_dir.glob("plink.chrchr*.GRCh38.map")):
        stray.unlink()
        log.info("reference_panel.genetic_map.removed_doubled", path=str(stray))


def _missing_map_files(panel: ReferencePanel) -> bool:
    """Return True if any expected per-chromosome map file is missing."""
    return any(not panel.map_for_chrom(chrom).is_file() for chrom in PANEL_CHROMOSOMES)


def _chrom_sort_key(chrom: str) -> tuple[int, str]:
    """Sort autosomes numerically before sex chromosomes."""
    try:
        return (0, f"{int(chrom):02d}")
    except ValueError:
        return (1, chrom)


__all__ = [
    "BEAGLE_JAR_URL",
    "EXTERNAL_ENDPOINT_LABEL",
    "GENETIC_MAP_URL",
    "PANEL_AUTOSOMES",
    "PANEL_CHROMOSOMES",
    "PANEL_SEX_CHROMS",
    "URL_VERIFIED_DATE",
    "ReferencePanel",
    "default_panel_root",
    "install_panel",
    "normalize_map_chrom",
    "validate_panel",
]

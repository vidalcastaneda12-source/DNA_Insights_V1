"""Beagle 5.5 per-chromosome imputation runner.

The third Phase 4 step. ``prepare_run`` (existing) builds per-chromosome
upload VCFs from the merged consensus; this module pipes each of those
through Beagle 5.5 against the local 1000 Genomes Phase 3 reference panel
and lands one imputed VCF per chromosome under
``archive/imputation/run_<id>/result/``. The subsequent ``import_result``
step (existing, still labelled ``topmed_imputed`` until session 6) then
streams those imputed VCFs into ``genotype_calls`` and ``variants_master``.

Key design points:

* **Per-chromosome subprocess.** One ``java -jar beagle.jar`` invocation
  per chromosome. Keeps each invocation's memory bounded (~8 GB heap is
  the default; chr1/2 fit comfortably) and lets a failed chromosome be
  retried independently.
* **Resumability.** A chromosome whose output VCF already exists and
  parses cleanly with cyvcf2 is skipped — pass ``force=True`` to
  re-run anyway. Resumability matters here because a full run is wall-
  clock-hours on a personal laptop, and the cost of recomputing a
  cleanly-imputed chromosome is wasted CPU, not a correctness risk.
* **Partial failures are recoverable.** One Beagle failure does not abort
  the whole run. The runner logs the per-chrom failure, continues with
  the next chromosome, and the final :class:`BeagleRunResult` reports
  which chromosomes succeeded vs failed. The status moves to
  ``'completed'`` only when every attempted chromosome succeeded; any
  failures move the status to ``'failed'``.
* **chrY is intentionally skipped.** The 1000 Genomes high-coverage
  phased release does not include chrY, so
  :func:`ReferencePanel.panel_for_chrom('Y')` returns ``None``. The
  runner logs a warning and skips chrY's upload VCF if one was prepared.
* **Status updates.** ``'pending'`` → ``'processing'`` when the first
  chromosome starts; ``'completed'`` (with ``completed_at``) when all
  attempted chromosomes succeed; ``'failed'`` when every chromosome
  failed. ``'processing'`` remains if some succeeded and some failed
  (so the user can retry the failing chromosomes without losing the
  successful ones).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.imputation.archive import ImputationArchive, restrict_file
from genome.imputation.reference_panel import (
    PANEL_CHROMOSOMES,
    ReferencePanel,
    validate_panel,
)
from genome.imputation.runs import (
    ImputationRun,
    fetch_run,
    update_status,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)

BEAGLE_RUNNER_VERSION: Final[str] = "beagle_runner_v0.1.0"
"""Pipeline version stamped on log lines so runs are traceable."""

DEFAULT_NE: Final[int] = 1_000_000
"""Beagle's documented default effective population size for outbred humans."""

DEFAULT_MEMORY_GB: Final[int] = 8
"""Default heap size (``-Xmx``) passed to Beagle. User-overridable."""

_MIN_JAVA_MAJOR: Final[int] = 8
"""Beagle 5.5 requires Java 8 or newer."""

_LEGACY_JAVA_MAJOR: Final[int] = 1
"""Legacy Java versions reported as ``1.x`` (e.g. ``1.8.0_321`` → Java 8)."""

_CHROM_ORDER: Final[tuple[str, ...]] = (*(str(i) for i in range(1, 23)), "X", "Y")
"""Deterministic execution order across calls (autosomes, then X, then Y)."""

_JAVA_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r'(?:openjdk|java) version "(?P<v>[^"]+)"',
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BeagleRunResult:
    """Summary returned by :func:`run_imputation`."""

    imputation_id: int
    chromosomes_attempted: tuple[str, ...]
    chromosomes_completed: tuple[str, ...]
    chromosomes_failed: tuple[str, ...]
    chromosomes_skipped: tuple[str, ...]
    per_chrom_seconds: dict[str, float]


def default_threads() -> int:
    """Return ``max(1, os.cpu_count() - 1)``.

    Leaving one core for the OS / IDE / shell keeps a long-running Beagle
    invocation from rendering the host unusable. ``os.cpu_count()`` can
    return ``None`` on some platforms; default to 2 in that case so the
    result is still ``1``.
    """
    cpus = os.cpu_count() or 2
    return max(1, cpus - 1)


def check_java_available() -> str:
    """Run ``java -version`` and return the version string.

    Beagle 5.5 requires Java 8+. The version is read from stderr (Java's
    ``-version`` writes to stderr, not stdout — historic quirk). Raises
    :class:`RuntimeError` with an actionable message when Java is absent
    or older than 8.
    """
    if shutil.which("java") is None:
        msg = (
            "Java is required for Beagle but `java` was not found on PATH. "
            "Install a Java 8+ runtime (e.g. `apt install default-jre` on "
            "Debian/Ubuntu) and re-run."
        )
        raise RuntimeError(msg)

    try:
        proc = subprocess.run(
            ["java", "-version"],  # noqa: S607 — PATH-resolved via shutil.which above
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        msg = (
            "Java is required for Beagle but `java` was not found on PATH. "
            "Install a Java 8+ runtime and re-run."
        )
        raise RuntimeError(msg) from exc

    output = (proc.stderr or "") + (proc.stdout or "")
    match = _JAVA_VERSION_RE.search(output)
    if match is None:
        msg = f"Could not parse Java version from `java -version` output. Got: {output!r}"
        raise RuntimeError(msg)
    version_str = match.group("v")
    major = _major_version(version_str)
    if major < _MIN_JAVA_MAJOR:
        msg = (
            f"Beagle 5.5 requires Java {_MIN_JAVA_MAJOR}+; found Java "
            f"{version_str}. Install a newer JRE and re-run."
        )
        raise RuntimeError(msg)
    return version_str


def _major_version(version: str) -> int:
    """Return the major Java version from a string like ``'17.0.5'`` or ``'1.8.0_321'``.

    Java's old 1.x versioning collapses ``1.8`` → 8, ``1.7`` → 7. Modern
    JDKs report ``17.0.5`` → 17. Returns ``0`` on parse failure so the
    caller can decide what to do with it.
    """
    parts = version.split(".")
    if not parts:
        return 0
    try:
        first = int(parts[0])
    except ValueError:
        return 0
    has_minor = len(parts) >= 2  # noqa: PLR2004 — readable named flag
    if first == _LEGACY_JAVA_MAJOR and has_minor:
        try:
            return int(parts[1])
        except ValueError:
            return 0
    return first


def _chrom_from_upload_path(path: Path) -> str | None:
    """Extract the chromosome label from a ``chr<N>.vcf.gz`` upload filename."""
    name = path.name
    if not name.lower().startswith("chr"):
        return None
    rest = name[3:]
    token = rest.split(".", 1)[0].upper()
    if token in {*(str(i) for i in range(1, 23)), "X", "Y", "MT"}:
        return token
    return None


def _output_vcf_path(archive: ImputationArchive, chrom: str) -> Path:
    """Path Beagle's ``out=`` argument resolves to (Beagle appends .vcf.gz)."""
    return archive.result_dir / f"chr{chrom}.vcf.gz"


def _output_prefix(archive: ImputationArchive, chrom: str) -> Path:
    """``out=`` argument: the path WITHOUT the .vcf.gz suffix Beagle appends."""
    return archive.result_dir / f"chr{chrom}"


def _vcf_parses_cleanly(path: Path) -> bool:
    """Open ``path`` with cyvcf2 and confirm the header is readable.

    A "clean" parse here is intentionally cheap — header + a single record
    read. Beagle's output is bgzipped VCF, so a partial / truncated write
    will fail at the cyvcf2 open or first iteration with an exception.
    The function returns ``False`` on any parse failure so the caller can
    treat the file as missing and re-run.
    """
    if not path.is_file():
        return False
    try:
        import cyvcf2  # noqa: PLC0415 — import deferred so module loads without cyvcf2 at type-check time

        reader = cyvcf2.VCF(str(path))
    except Exception:  # noqa: BLE001 — any cyvcf2 error means "not clean"
        return False
    try:
        # Reading one record is enough to confirm the body is intact. An empty
        # body (no records) is acceptable — Beagle can produce one for a tiny
        # input. The header-only open above already passed.
        for _ in reader:
            break
    except Exception:  # noqa: BLE001
        return False
    finally:
        reader.close()
    return True


def _build_beagle_command(  # noqa: PLR0913 — Beagle's CLI is a flat keyword list
    *,
    beagle_jar: Path,
    panel_vcf: Path,
    map_file: Path,
    input_vcf: Path,
    output_prefix: Path,
    threads: int,
    memory_gb: int,
    ne: int,
) -> list[str]:
    """Build the per-chromosome Beagle command line.

    Beagle 5.5's CLI is a flat ``key=value`` list; we pass each argument as a
    single token because the values are filesystem paths and integers and
    therefore never contain whitespace.
    """
    return [
        "java",
        f"-Xmx{memory_gb}g",
        "-jar",
        str(beagle_jar),
        f"ref={panel_vcf}",
        f"map={map_file}",
        f"gt={input_vcf}",
        f"out={output_prefix}",
        f"nthreads={threads}",
        f"ne={ne}",
        "impute=true",
    ]


def _run_beagle_subprocess(
    cmd: list[str],
    *,
    chrom: str,
    imputation_id: int,
) -> int:
    """Run ``cmd`` and stream stderr line-by-line into the structlog logger.

    Returns the subprocess's exit code. Beagle writes progress to stderr,
    not stdout, so we capture stderr and let stdout flow through to the
    parent's stdout (Beagle's stdout is generally empty anyway).
    """
    log = logger.bind(chrom=chrom, imputation_id=imputation_id)
    log.info("imputation.beagle.subprocess.start", cmd=cmd)
    proc = subprocess.Popen(  # noqa: S603 — caller composes the argv; no shell
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stderr is not None  # noqa: S101 — stderr=PIPE guarantees non-None
    try:
        for line in proc.stderr:
            stripped = line.rstrip()
            if stripped:
                log.info("imputation.beagle.subprocess.stderr", line=stripped)
    finally:
        rc = proc.wait()
    log.info("imputation.beagle.subprocess.exit", returncode=rc)
    return rc


def _validate_run_state(run: ImputationRun, *, force: bool) -> None:
    """Raise unless the run is in a state where Beagle imputation makes sense.

    Default-permitted: ``pending``, ``processing`` (the latter handles
    re-entering a partially-completed run). With ``force=True`` we also
    accept ``completed`` and ``failed`` so the user can deliberately
    re-run a finished or failed imputation.
    """
    allowed: set[str] = {"pending", "processing"}
    if force:
        allowed |= {"completed", "failed"}
    if run.status not in allowed:
        msg = (
            f"imputation_id {run.imputation_id} is in status {run.status!r}; "
            f"expected one of {sorted(allowed)}"
        )
        if not force and run.status in {"completed", "failed"}:
            msg += " (pass force=True to re-run)"
        raise RuntimeError(msg)


def _validate_panel_or_raise(panel: ReferencePanel) -> None:
    """Run :func:`validate_panel` and turn missing-component lists into an error."""
    problems = validate_panel(panel)
    if not problems:
        return
    detail = "\n  - ".join(problems)
    msg = (
        f"Reference panel at {panel.root} is incomplete:\n  - {detail}\n"
        "Run `genome imputation panel install` to download missing artifacts."
    )
    raise RuntimeError(msg)


def _collect_upload_inputs(
    archive: ImputationArchive,
    chromosomes: frozenset[str] | None,
) -> list[tuple[str, Path]]:
    """Return ``(chrom, upload_vcf_path)`` pairs in deterministic chrom order.

    Pairs are filtered to ``chromosomes`` when supplied. Missing chromosome
    upload VCFs are skipped silently — :func:`prepare_run` only writes per-
    chromosome files for chromosomes that have at least one variant, so
    "missing" is the expected steady state for some chromosomes.
    """
    available: dict[str, Path] = {}
    for p in archive.list_upload_vcfs():
        c = _chrom_from_upload_path(p)
        if c is None:
            continue
        available[c] = p
    out: list[tuple[str, Path]] = []
    for c in _CHROM_ORDER:
        if c not in available:
            continue
        if chromosomes is not None and c not in chromosomes:
            continue
        out.append((c, available[c]))
    return out


def _stamp_status_after_run(  # noqa: PLR0913 — small flat keyword surface matches the call site
    *,
    duckdb_path: Path,
    imputation_id: int,
    initial_status: str,
    attempted: int,
    completed: int,
    failed: int,
) -> str:
    """Move the run's status to its terminal value and return the new status.

    Rules:
    * If nothing was attempted (everything skipped or filtered out), the
      status is unchanged.
    * If everything attempted succeeded → ``'completed'`` (stamps
      ``completed_at``).
    * If everything attempted failed → ``'failed'``.
    * Otherwise (mixed) → stays at ``'processing'`` so the user can retry
      the failures without losing the successful chromosomes.
    """
    if attempted == 0:
        return initial_status
    new_status: str
    set_completed = False
    if failed == 0 and completed == attempted:
        new_status = "completed"
        set_completed = True
    elif completed == 0:
        new_status = "failed"
    else:
        new_status = "processing"
    with duckdb_connection(duckdb_path) as conn:
        update_status(
            conn,
            imputation_id,
            status=new_status,  # type: ignore[arg-type]
            set_completed=set_completed,
        )
    return new_status


def _move_to_processing_if_pending(
    *,
    duckdb_path: Path,
    imputation_id: int,
    current_status: str,
) -> None:
    """Move ``pending`` → ``processing`` exactly once at the start of work."""
    if current_status != "pending":
        return
    with duckdb_connection(duckdb_path) as conn:
        update_status(conn, imputation_id, status="processing")


def _impute_one_chromosome(  # noqa: PLR0913 — per-call configuration is irreducible
    *,
    archive: ImputationArchive,
    panel: ReferencePanel,
    chrom: str,
    input_vcf: Path,
    threads: int,
    memory_gb: int,
    ne: int,
    imputation_id: int,
    force: bool,
) -> tuple[str, float]:
    """Run Beagle for one chromosome.

    Returns ``(status, seconds)`` where status is one of ``'completed'``,
    ``'failed'``, or ``'skipped'``. Skips chrY when the panel doesn't
    include it (the 1000 Genomes high-coverage release omits chrY).
    Skips a chromosome whose output VCF already exists and parses
    cleanly, unless ``force`` is set.
    """
    log = logger.bind(chrom=chrom, imputation_id=imputation_id)
    panel_vcf = panel.panel_for_chrom(chrom)
    if panel_vcf is None:
        log.warning("imputation.beagle.chrom.skip_no_panel", chrom=chrom)
        return "skipped", 0.0

    output_vcf = _output_vcf_path(archive, chrom)
    if not force and _vcf_parses_cleanly(output_vcf):
        log.info("imputation.beagle.chrom.skip_existing", output=str(output_vcf))
        return "skipped", 0.0

    if force and output_vcf.is_file():
        output_vcf.unlink()

    cmd = _build_beagle_command(
        beagle_jar=panel.beagle_jar,
        panel_vcf=panel_vcf,
        map_file=panel.map_for_chrom(chrom),
        input_vcf=input_vcf,
        output_prefix=_output_prefix(archive, chrom),
        threads=threads,
        memory_gb=memory_gb,
        ne=ne,
    )

    start = time.monotonic()
    try:
        rc = _run_beagle_subprocess(cmd, chrom=chrom, imputation_id=imputation_id)
    except Exception:
        elapsed = time.monotonic() - start
        log.exception("imputation.beagle.chrom.exception")
        return "failed", elapsed
    elapsed = time.monotonic() - start

    if rc != 0:
        log.error("imputation.beagle.chrom.failed", returncode=rc, elapsed=elapsed)
        return "failed", elapsed

    if not _vcf_parses_cleanly(output_vcf):
        log.error(
            "imputation.beagle.chrom.output_invalid",
            output=str(output_vcf),
            elapsed=elapsed,
        )
        return "failed", elapsed

    restrict_file(output_vcf)
    log.info("imputation.beagle.chrom.complete", elapsed=elapsed)
    return "completed", elapsed


def _validate_runner_options(
    *,
    chromosomes: frozenset[str] | None,
    threads: int,
    memory_gb: int,
    ne: int,
) -> None:
    """Range-check the operational knobs that come from the CLI."""
    if chromosomes is not None:
        unknown = chromosomes - set(_CHROM_ORDER)
        if unknown:
            msg = f"unknown chromosome(s) {sorted(unknown)}"
            raise ValueError(msg)
    if threads < 1:
        msg = f"threads must be >= 1, got {threads}"
        raise ValueError(msg)
    if memory_gb < 1:
        msg = f"memory_gb must be >= 1, got {memory_gb}"
        raise ValueError(msg)
    if ne < 1:
        msg = f"ne must be >= 1, got {ne}"
        raise ValueError(msg)


def _fetch_validated_run(
    duckdb_path: Path,
    imputation_id: int,
    *,
    force: bool,
) -> ImputationRun:
    """Read the run row, raise on missing / wrong status, return the row."""
    with duckdb_connection(duckdb_path) as conn:
        run = fetch_run(conn, imputation_id)
    if run is None:
        msg = f"imputation_id {imputation_id} not found"
        raise ValueError(msg)
    _validate_run_state(run, force=force)
    return run


def _resolve_upload_inputs(
    archive: ImputationArchive,
    chromosomes: frozenset[str] | None,
) -> list[tuple[str, Path]]:
    """Collect upload VCFs; raise with a CLI-friendly message when empty."""
    upload_inputs = _collect_upload_inputs(archive, chromosomes)
    if upload_inputs:
        return upload_inputs
    if chromosomes is not None:
        msg = (
            f"no upload VCFs found under {archive.upload_dir} matching "
            f"chromosomes {sorted(chromosomes)}"
        )
    else:
        msg = (
            f"no upload VCFs found under {archive.upload_dir}; "
            "run `genome imputation prepare` first"
        )
    raise RuntimeError(msg)


def run_imputation(  # noqa: PLR0913 — operational controls map 1:1 to the CLI surface
    imputation_id: int,
    *,
    chromosomes: frozenset[str] | None = None,
    threads: int | None = None,
    memory_gb: int = DEFAULT_MEMORY_GB,
    ne: int = DEFAULT_NE,
    force: bool = False,
    duckdb_path: Path | None = None,
    archive_root: Path | None = None,
    panel_root: Path | None = None,
) -> BeagleRunResult:
    """Run Beagle 5.5 against the prepared upload VCFs for ``imputation_id``.

    See the module docstring for the full flow. Pre-flight checks:

    1. The ``imputation_runs`` row exists and is in an acceptable status.
    2. Java 8+ is on PATH.
    3. The reference panel under ``panel_root`` is fully populated.

    Per-chromosome execution is wrapped in try/except so one chromosome's
    failure does not abort the rest of the run — the user can retry the
    failing chromosomes later. The terminal status reflects the aggregate:
    ``'completed'`` only when every attempted chromosome succeeded.
    """
    settings = get_settings()
    duckdb_path = duckdb_path or settings.genome_duckdb_path
    archive_root = archive_root or settings.archive_path

    log = logger.bind(imputation_id=imputation_id, runner_version=BEAGLE_RUNNER_VERSION)

    if threads is None:
        threads = default_threads()
    _validate_runner_options(
        chromosomes=chromosomes,
        threads=threads,
        memory_gb=memory_gb,
        ne=ne,
    )

    run = _fetch_validated_run(duckdb_path, imputation_id, force=force)
    initial_status = run.status

    java_version = check_java_available()
    log.info("imputation.beagle.java", version=java_version)

    panel = ReferencePanel.resolve(panel_root)
    _validate_panel_or_raise(panel)

    archive = ImputationArchive.for_run(archive_root, imputation_id)
    archive.ensure_layout()
    upload_inputs = _resolve_upload_inputs(archive, chromosomes)

    _move_to_processing_if_pending(
        duckdb_path=duckdb_path,
        imputation_id=imputation_id,
        current_status=initial_status,
    )

    attempted: list[str] = []
    completed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    per_chrom_seconds: dict[str, float] = {}

    for chrom, input_vcf in upload_inputs:
        if chrom == "Y" and panel.panel_for_chrom("Y") is None:
            log.warning("imputation.beagle.chrom.skip_no_panel", chrom=chrom)
            skipped.append(chrom)
            continue
        attempted.append(chrom)
        outcome, elapsed = _impute_one_chromosome(
            archive=archive,
            panel=panel,
            chrom=chrom,
            input_vcf=input_vcf,
            threads=threads,
            memory_gb=memory_gb,
            ne=ne,
            imputation_id=imputation_id,
            force=force,
        )
        per_chrom_seconds[chrom] = elapsed
        if outcome == "completed":
            completed.append(chrom)
        elif outcome == "failed":
            failed.append(chrom)
        elif outcome == "skipped":
            # A skip discovered inside _impute_one_chromosome (e.g. existing
            # output): retract from attempted, push to skipped.
            attempted.pop()
            skipped.append(chrom)

    new_status = _stamp_status_after_run(
        duckdb_path=duckdb_path,
        imputation_id=imputation_id,
        initial_status=initial_status,
        attempted=len(attempted),
        completed=len(completed),
        failed=len(failed),
    )

    log.info(
        "imputation.beagle.run.complete",
        new_status=new_status,
        attempted=attempted,
        completed=completed,
        failed=failed,
        skipped=skipped,
    )
    return BeagleRunResult(
        imputation_id=imputation_id,
        chromosomes_attempted=tuple(attempted),
        chromosomes_completed=tuple(completed),
        chromosomes_failed=tuple(failed),
        chromosomes_skipped=tuple(skipped),
        per_chrom_seconds=dict(per_chrom_seconds),
    )


__all__ = [
    "BEAGLE_RUNNER_VERSION",
    "DEFAULT_MEMORY_GB",
    "DEFAULT_NE",
    "PANEL_CHROMOSOMES",
    "BeagleRunResult",
    "check_java_available",
    "default_threads",
    "run_imputation",
]

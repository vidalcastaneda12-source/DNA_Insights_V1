"""Tests for :mod:`genome.imputation.beagle_runner` — the local Beagle runner.

Beagle is never actually invoked from these tests. ``subprocess.run`` (for
the Java version probe) and ``subprocess.Popen`` (for the Beagle
invocation itself) are patched. A fake Popen writes a minimal valid VCF
into the expected output path when the test wants the run to "succeed",
which exercises the cyvcf2 parse check and the post-run housekeeping.
"""

from __future__ import annotations

import gzip
import io
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from genome.db import duckdb_connection, init_databases
from genome.imputation.archive import ImputationArchive
from genome.imputation.beagle_runner import (
    BEAGLE_RUNNER_VERSION,
    DEFAULT_MEMORY_GB,
    DEFAULT_NE,
    BeagleRunResult,
    check_java_available,
    default_threads,
    run_imputation,
)
from genome.imputation.ingest import import_result
from genome.imputation.reference_panel import (
    PANEL_CHROMOSOMES,
    ReferencePanel,
)
from genome.imputation.runs import fetch_run, insert_run, update_status

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# Helpers — synthetic panel, fake subprocesses, minimal VCFs.
# ---------------------------------------------------------------------------


_MINIMAL_VCF_HEADER = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID=chr{chrom},length=248956422,assembly=GRCh38>\n"
    '##INFO=<ID=DR2,Number=1,Type=Float,Description="Dosage R-squared">\n'
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
)


def _write_minimal_vcf(dest: Path, chrom: str) -> None:
    """Write a tiny gzipped VCF cyvcf2 can open."""
    header = _MINIMAL_VCF_HEADER.format(chrom=chrom)
    body = f"chr{chrom}\t100\trs1\tA\tG\t.\tPASS\tDR2=0.95\tGT\t0|1\n"
    with gzip.open(dest, "wt", encoding="ascii") as out:
        out.write(header)
        out.write(body)


def _seed_panel(panel_root: Path) -> ReferencePanel:
    """Build a complete reference-panel layout on disk.

    Files have placeholder content — the runner doesn't read them; it
    passes their paths to Beagle, which we mock. ``validate_panel``
    only checks for presence, so any non-empty bytes satisfies it.
    """
    panel = ReferencePanel.resolve(panel_root)
    panel.ensure_layout()
    panel.beagle_jar.write_bytes(b"jar")
    for c in PANEL_CHROMOSOMES:
        panel.map_for_chrom(c).write_bytes(b"map")
        p = panel.panel_for_chrom(c)
        assert p is not None
        p.write_bytes(b"vcf")
    return panel


def _seed_run(
    *,
    archive_root: Path,
    chromosomes: tuple[str, ...] = ("1",),
    status: str = "pending",
) -> int:
    """Insert an ``imputation_runs`` row and stage upload VCFs for ``chromosomes``."""
    init_databases()
    with duckdb_connection() as conn:
        imp_id = insert_run(
            conn,
            input_run_ids=(1,),
            imputation_server="beagle",
            reference_panel="1000g_phase3",
            pipeline_version=BEAGLE_RUNNER_VERSION,
            variants_input=100,
        )
        if status != "pending":
            update_status(conn, imp_id, status=status)  # type: ignore[arg-type]
    archive = ImputationArchive.for_run(archive_root, imp_id)
    archive.ensure_layout()
    for c in chromosomes:
        _write_minimal_vcf(archive.upload_vcf_path(c), c)
    return imp_id


@dataclass
class _FakeProc:
    """Stand-in for ``subprocess.Popen`` instances used by the runner."""

    returncode_to_return: int = 0
    output_vcf_to_write: Path | None = None
    output_chrom: str = "1"
    raise_on_wait: BaseException | None = None
    stderr_lines: tuple[str, ...] = ("[beagle] starting", "[beagle] done")
    _returncode: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.stderr = io.StringIO("\n".join(self.stderr_lines))

    def wait(self) -> int:
        if self.raise_on_wait is not None:
            raise self.raise_on_wait
        if self.returncode_to_return == 0 and self.output_vcf_to_write is not None:
            _write_minimal_vcf(self.output_vcf_to_write, self.output_chrom)
        self._returncode = self.returncode_to_return
        return self.returncode_to_return


def _make_popen_factory(
    returncodes: dict[str, int] | None = None,
    *,
    truncate_for: frozenset[str] = frozenset(),
) -> tuple[mock.MagicMock, list[list[str]]]:
    """Build a ``Popen``-shaped mock plus a captured-argv list.

    ``returncodes`` maps a chromosome label to a per-chromosome exit code
    (0 by default). ``truncate_for`` chromosomes still get rc=0 but the
    fake writes a truncated/invalid VCF so the cyvcf2 parse check fails.
    """
    captured: list[list[str]] = []
    returncodes = returncodes or {}

    def _factory(cmd: list[str], *_args: object, **_kwargs: object) -> _FakeProc:
        captured.append(list(cmd))
        gt_arg = next(a for a in cmd if a.startswith("gt="))
        chrom = _chrom_from_upload_path_str(gt_arg[len("gt=") :])
        out_arg = next(a for a in cmd if a.startswith("out="))
        out_prefix = out_arg[len("out=") :]
        output_vcf = Path(f"{out_prefix}.vcf.gz")
        rc = returncodes.get(chrom or "?", 0)
        if chrom in truncate_for:
            # Simulate a corrupted output: write a single byte so the file
            # exists but cyvcf2 can't open it.
            output_vcf.write_bytes(b"not a vcf")
            return _FakeProc(
                returncode_to_return=0,
                output_vcf_to_write=None,  # already populated above
                output_chrom=chrom or "1",
            )
        return _FakeProc(
            returncode_to_return=rc,
            output_vcf_to_write=output_vcf if rc == 0 else None,
            output_chrom=chrom or "1",
        )

    popen_mock = mock.MagicMock(side_effect=_factory)
    return popen_mock, captured


def _chrom_from_upload_path_str(s: str) -> str | None:
    """Parse ``/...chr<N>.vcf.gz`` → ``<N>``. Mirrors the runner's helper."""
    name = s.rsplit("/", 1)[-1]
    if not name.lower().startswith("chr"):
        return None
    return name[3:].split(".", 1)[0].upper()


# ---------------------------------------------------------------------------
# Path fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def panel_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings: dict[str, str],  # noqa: ARG001 — sets up env + cached settings
) -> Iterator[Path]:
    root = tmp_path / "panel-root"
    monkeypatch.setenv("IMPUTATION_PANEL_ROOT", str(root))
    from genome.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    try:
        yield root
    finally:
        get_settings.cache_clear()


def _archive_root(env: dict[str, str]) -> Path:
    from pathlib import Path as _Path  # noqa: PLC0415

    return _Path(env["ARCHIVE_PATH"])


@pytest.fixture
def stubbed_java(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``check_java_available`` succeed without spawning java."""
    monkeypatch.setattr(
        "genome.imputation.beagle_runner.check_java_available",
        lambda: "11.0.5",
    )


# ---------------------------------------------------------------------------
# default_threads
# ---------------------------------------------------------------------------


def test_default_threads_cpu_count_8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    assert default_threads() == 7


def test_default_threads_cpu_count_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 2)
    assert default_threads() == 1


def test_default_threads_cpu_count_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 1)
    # max(1, 0) — never drops below 1, even on a single-core host.
    assert default_threads() == 1


def test_default_threads_cpu_count_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: None)
    # None → fall back to 2, then minus 1 → 1.
    assert default_threads() == 1


# ---------------------------------------------------------------------------
# check_java_available
# ---------------------------------------------------------------------------


def _java_version_proc(version_string: str, *, on_stderr: bool = True) -> mock.MagicMock:
    """Build a ``subprocess.run`` return value that mimics ``java -version``."""
    output = f'openjdk version "{version_string}" 2023-10-17\n'
    proc = mock.MagicMock()
    proc.stderr = output if on_stderr else ""
    proc.stdout = "" if on_stderr else output
    proc.returncode = 0
    return proc


def test_check_java_available_returns_version_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/java")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_kw: _java_version_proc("17.0.5"),
    )
    assert check_java_available() == "17.0.5"


def test_check_java_available_accepts_legacy_1_dot_8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``1.8.0_321`` → major 8, which is the minimum supported version."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/java")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_kw: _java_version_proc("1.8.0_321"),
    )
    assert check_java_available() == "1.8.0_321"


def test_check_java_available_raises_when_java_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="java"):
        check_java_available()


def test_check_java_available_raises_on_too_old_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/java")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_kw: _java_version_proc("1.6.0_27"),
    )
    with pytest.raises(RuntimeError, match="requires Java"):
        check_java_available()


def test_check_java_available_raises_on_malformed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/java")
    proc = mock.MagicMock()
    proc.stderr = "garbage that doesn't include a version string\n"
    proc.stdout = ""
    proc.returncode = 0
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: proc)
    with pytest.raises(RuntimeError, match="parse Java version"):
        check_java_available()


def test_check_java_available_raises_when_subprocess_filenotfound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shutil.which`` may resolve but the exec can still fail (rare)."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/java")

    msg = "java"

    def _raise(*_a: object, **_kw: object) -> mock.MagicMock:
        raise FileNotFoundError(msg)

    monkeypatch.setattr(subprocess, "run", _raise)
    with pytest.raises(RuntimeError, match="java"):
        check_java_available()


# ---------------------------------------------------------------------------
# run_imputation — happy path / skip / force / failure handling
# ---------------------------------------------------------------------------


def test_run_imputation_completes_when_all_chromosomes_succeed(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1", "2"))

    popen_mock, captured = _make_popen_factory()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    result = run_imputation(imp_id)

    assert isinstance(result, BeagleRunResult)
    assert result.imputation_id == imp_id
    assert set(result.chromosomes_attempted) == {"1", "2"}
    assert set(result.chromosomes_completed) == {"1", "2"}
    assert result.chromosomes_failed == ()
    # Output VCFs landed at the path archive.list_result_vcfs() finds.
    archive = ImputationArchive.for_run(archive_root, imp_id)
    result_files = {p.name for p in archive.list_result_vcfs()}
    assert result_files == {"chr1.vcf.gz", "chr2.vcf.gz"}
    # The run row moved to 'completed' with completed_at populated.
    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.status == "completed"
    assert run.completed_at is not None
    # One Popen call per chromosome.
    assert len(captured) == 2


def test_run_imputation_skips_chromosomes_with_existing_clean_output(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1", "2"))
    archive = ImputationArchive.for_run(archive_root, imp_id)
    # Pre-populate chr1's result so the runner should skip it.
    _write_minimal_vcf(archive.result_dir / "chr1.vcf.gz", "1")

    popen_mock, captured = _make_popen_factory()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    result = run_imputation(imp_id)

    assert "1" in result.chromosomes_skipped
    assert "2" in result.chromosomes_completed
    # Only chr2 should have hit Popen.
    assert len(captured) == 1
    assert any(a.endswith("chr2.vcf.gz") for a in captured[0] if a.startswith("gt="))


def test_run_imputation_force_reruns_chromosome_with_existing_output(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(
        archive_root=archive_root,
        chromosomes=("1",),
        status="completed",
    )
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_minimal_vcf(archive.result_dir / "chr1.vcf.gz", "1")

    popen_mock, captured = _make_popen_factory()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    result = run_imputation(imp_id, force=True)

    assert result.chromosomes_completed == ("1",)
    assert result.chromosomes_skipped == ()
    assert len(captured) == 1


def test_run_imputation_raises_when_panel_missing(
    isolated_settings: dict[str, str],
    panel_root: Path,  # noqa: ARG001 — fixture sets settings override; we leave the panel empty
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    archive_root = _archive_root(isolated_settings)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1",))
    # No panel seed — every component is missing.
    with pytest.raises(RuntimeError, match="genome imputation panel install"):
        run_imputation(imp_id)


def test_run_imputation_continues_past_failed_chromosome(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1", "2", "3"))

    popen_mock, captured = _make_popen_factory(returncodes={"2": 1})
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    result = run_imputation(imp_id)

    assert set(result.chromosomes_attempted) == {"1", "2", "3"}
    assert set(result.chromosomes_completed) == {"1", "3"}
    assert set(result.chromosomes_failed) == {"2"}
    # Status reflects mixed success: stays at 'processing'.
    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.status == "processing"
    # All three chromosomes were attempted.
    assert len(captured) == 3


def test_run_imputation_corrupt_output_counts_as_failure(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    """A subprocess that returns 0 but produces an unparseable VCF must fail."""
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1",))

    popen_mock, _ = _make_popen_factory(truncate_for=frozenset({"1"}))
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    result = run_imputation(imp_id)
    assert result.chromosomes_failed == ("1",)


# ---------------------------------------------------------------------------
# Status transitions.
# ---------------------------------------------------------------------------


def test_run_imputation_pending_to_processing_to_completed(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1",), status="pending")

    statuses_seen: list[str] = []

    real_popen, _captured = _make_popen_factory()

    def _capture_status(cmd: list[str], *args: object, **kwargs: object) -> _FakeProc:
        # The runner should have moved status to 'processing' before the
        # first Popen call.
        with duckdb_connection() as conn:
            run = fetch_run(conn, imp_id)
        if run is not None:
            statuses_seen.append(run.status)
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _capture_status)
    run_imputation(imp_id)

    assert statuses_seen == ["processing"]
    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.status == "completed"
    assert run.completed_at is not None


def test_run_imputation_pending_to_processing_to_failed(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1",), status="pending")

    popen_mock, _ = _make_popen_factory(returncodes={"1": 1})
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    result = run_imputation(imp_id)

    assert result.chromosomes_failed == ("1",)
    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.status == "failed"
    # completed_at must remain NULL on a failed run.
    assert run.completed_at is None


def test_run_imputation_rejects_unknown_id(
    isolated_settings: dict[str, str],  # noqa: ARG001 — fixture forces tmp-scoped settings
    panel_root: Path,  # noqa: ARG001 — fixture sets panel-root override
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    init_databases()
    with pytest.raises(ValueError, match="not found"):
        run_imputation(999)


def test_run_imputation_rejects_completed_run_without_force(
    isolated_settings: dict[str, str],
    panel_root: Path,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(
        archive_root=archive_root,
        chromosomes=("1",),
        status="completed",
    )
    with pytest.raises(RuntimeError, match="force=True"):
        run_imputation(imp_id)


def test_run_imputation_uses_default_ne_and_memory(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    """The default arguments are passed through to the Beagle command line."""
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1",))

    popen_mock, captured = _make_popen_factory()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    run_imputation(imp_id)
    cmd = captured[0]
    assert f"-Xmx{DEFAULT_MEMORY_GB}g" in cmd
    assert f"ne={DEFAULT_NE}" in cmd
    # impute=true is locked.
    assert "impute=true" in cmd


def test_run_imputation_passes_user_threads_memory_and_ne(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1",))

    popen_mock, captured = _make_popen_factory()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    run_imputation(imp_id, threads=3, memory_gb=4, ne=2000)
    cmd = captured[0]
    assert "-Xmx4g" in cmd
    assert "nthreads=3" in cmd
    assert "ne=2000" in cmd


def test_run_imputation_skips_chry_when_panel_lacks_it(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    """chrY in the upload set is logged + skipped (1000G panel has no Y)."""
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1", "Y"))

    popen_mock, captured = _make_popen_factory()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    result = run_imputation(imp_id)
    assert "Y" in result.chromosomes_skipped
    assert "1" in result.chromosomes_completed
    # Only chr1 invoked Beagle.
    assert len(captured) == 1


def test_run_imputation_chromosomes_filter_limits_attempts(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1", "2", "3"))

    popen_mock, captured = _make_popen_factory()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    result = run_imputation(imp_id, chromosomes=frozenset({"2"}))
    assert result.chromosomes_attempted == ("2",)
    assert result.chromosomes_completed == ("2",)
    assert len(captured) == 1


def test_run_imputation_no_upload_vcfs_raises(
    isolated_settings: dict[str, str],
    panel_root: Path,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    # Seed a run but DON'T write any upload VCFs.
    imp_id = _seed_run(archive_root=archive_root, chromosomes=())
    with pytest.raises(RuntimeError, match="no upload VCFs"):
        run_imputation(imp_id)


def test_run_imputation_output_vcf_is_owner_read_write_only(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    """Result VCFs land with 0600 perms (same posture as the upload VCFs)."""
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1",))

    popen_mock, _ = _make_popen_factory()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    run_imputation(imp_id)

    archive = ImputationArchive.for_run(archive_root, imp_id)
    out = archive.result_dir / "chr1.vcf.gz"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# htslib contig-warning suppression.
# ---------------------------------------------------------------------------


_HEADERLESS_VCF_HEADER = (
    "##fileformat=VCFv4.2\n"
    '##INFO=<ID=DR2,Number=1,Type=Float,Description="Dosage R-squared">\n'
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
)


def _write_vcf_without_contig_header(dest: Path, chrom: str) -> None:
    """Write a tiny VCF with NO ``##contig`` line, mimicking Beagle output.

    htslib emits ``[W::vcf_parse] Contig 'chr<N>' is not defined in the
    header`` once per record when the contig isn't declared. The cyvcf2
    parse itself succeeds.
    """
    body = (
        f"chr{chrom}\t100\trs1\tA\tG\t.\tPASS\tDR2=0.95\tGT\t0|1\n"
        f"chr{chrom}\t200\trs2\tA\tG\t.\tPASS\tDR2=0.99\tGT\t1|1\n"
    )
    with gzip.open(dest, "wt", encoding="ascii") as out:
        out.write(_HEADERLESS_VCF_HEADER)
        out.write(body)


def test_vcf_parses_cleanly_returns_true_on_beagle_style_output(
    tmp_path: Path,
) -> None:
    """Regression: a Beagle-style VCF (no ##contig header) is accepted.

    Real-data verification surfaced a regression where wrapping the
    validator's open + iterate in ``silence_htslib_contig_warnings``
    caused it to return False on a parsable 1M-record Beagle output.
    We can't reproduce the exact failure with synthetic data, but the
    fix is to keep the validator wrapper-free (the warning fires at
    most once per call here, since the function reads a single record)
    and scope the suppression to the per-record streaming reads in
    :mod:`genome.imputation.ingest` instead.

    This test guards against re-introducing the regression by verifying
    that ``_vcf_parses_cleanly`` returns True on a header-less Beagle-
    shaped VCF — the same provocation that exposed the failure in real
    use.
    """
    from genome.imputation.beagle_runner import (  # noqa: PLC0415 — late import keeps top-level minimal
        _vcf_parses_cleanly,
    )

    vcf_path = tmp_path / "headerless.vcf.gz"
    _write_vcf_without_contig_header(vcf_path, "22")

    assert _vcf_parses_cleanly(vcf_path) is True


def test_vcf_parses_cleanly_returns_false_on_truncated_file(
    tmp_path: Path,
) -> None:
    """Real parse errors (truncated body) surface as False.

    ``_vcf_parses_cleanly`` returns False for any cyvcf2 exception so
    the caller treats a truncated output as a failed re-run.
    """
    from genome.imputation.beagle_runner import (  # noqa: PLC0415
        _vcf_parses_cleanly,
    )

    bad = tmp_path / "truncated.vcf.gz"
    bad.write_bytes(b"not a vcf at all")
    assert _vcf_parses_cleanly(bad) is False


def test_silence_htslib_contig_warnings_restores_default_on_exit(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """After our context manager exits, htslib's log level is back to default.

    Important so that other cyvcf2 readers elsewhere in the process see
    htslib's normal warning verbosity. We verify by opening a headerless
    VCF AFTER the suppressed block and confirming the warning fires.
    """
    import cyvcf2  # noqa: PLC0415

    from genome.imputation._htslib import (  # noqa: PLC0415
        silence_htslib_contig_warnings,
    )

    vcf_path = tmp_path / "headerless.vcf.gz"
    _write_vcf_without_contig_header(vcf_path, "22")

    # First: inside the suppression block, no warning.
    with silence_htslib_contig_warnings():
        reader = cyvcf2.VCF(str(vcf_path))
        for _ in reader:
            pass
        reader.close()
    inside = capfd.readouterr()
    assert "Contig" not in inside.err

    # Second: AFTER the block, htslib is back to default — warning fires.
    reader = cyvcf2.VCF(str(vcf_path))
    for _ in reader:
        pass
    reader.close()
    after = capfd.readouterr()
    assert "Contig 'chr22'" in after.err


def test_silence_htslib_contig_warnings_restores_default_on_exception(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A body that raises still triggers the level restore on context exit."""
    import cyvcf2  # noqa: PLC0415

    from genome.imputation._htslib import (  # noqa: PLC0415
        silence_htslib_contig_warnings,
    )

    vcf_path = tmp_path / "headerless.vcf.gz"
    _write_vcf_without_contig_header(vcf_path, "22")

    boom = RuntimeError("synthetic")
    with pytest.raises(RuntimeError, match="synthetic"), silence_htslib_contig_warnings():
        raise boom
    # Now confirm the level is back to default by opening a headerless VCF
    # and asserting the warning still fires.
    reader = cyvcf2.VCF(str(vcf_path))
    for _ in reader:
        pass
    reader.close()
    captured = capfd.readouterr()
    assert "Contig 'chr22'" in captured.err


# ---------------------------------------------------------------------------
# submitted_at / completed_at stamping (Phase 4 cleanup, session A).
# ---------------------------------------------------------------------------


def test_run_imputation_fresh_run_stamps_submitted_and_completed(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    """Fresh ``pending`` → ``completed`` path stamps both timestamps.

    Invariant: every transition out of pending stamps ``submitted_at``;
    every transition to completed stamps ``completed_at``. A fresh full
    run crosses both transitions.
    """
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1",), status="pending")

    popen_mock, _ = _make_popen_factory()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    run_imputation(imp_id)

    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.status == "completed"
    assert run.submitted_at is not None
    assert run.completed_at is not None


def test_run_imputation_force_rerun_preserves_existing_timestamps(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    """A ``--force`` re-run of a stamped row leaves both timestamps unchanged.

    ``update_status`` uses ``COALESCE(..., CURRENT_TIMESTAMP)`` so any
    re-stamp is a no-op when the column was already set.
    """
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(
        archive_root=archive_root,
        chromosomes=("1",),
        status="completed",
    )
    # Stamp submitted_at + completed_at via the helper so we have a
    # known-good baseline. Re-running with force=True must preserve these.
    with duckdb_connection() as conn:
        update_status(conn, imp_id, status="completed", set_submitted=True, set_completed=True)
        before = fetch_run(conn, imp_id)
    assert before is not None
    assert before.submitted_at is not None
    assert before.completed_at is not None

    popen_mock, _ = _make_popen_factory()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    run_imputation(imp_id, force=True)

    with duckdb_connection() as conn:
        after = fetch_run(conn, imp_id)
    assert after is not None
    assert after.status == "completed"
    assert after.submitted_at == before.submitted_at
    assert after.completed_at == before.completed_at


def test_run_imputation_partial_failure_stamps_submitted_not_completed(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    """Mixed success/failure leaves status=processing, stamps submitted only.

    ``completed_at`` must remain NULL until every attempted chromosome
    succeeded — partial success is recoverable, so a stale stamp would
    confuse downstream callers.
    """
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(
        archive_root=archive_root,
        chromosomes=("1", "2"),
        status="pending",
    )

    popen_mock, _ = _make_popen_factory(returncodes={"2": 1})
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    result = run_imputation(imp_id)
    assert set(result.chromosomes_completed) == {"1"}
    assert set(result.chromosomes_failed) == {"2"}

    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.status == "processing"
    # Transition pending → processing stamped submitted_at.
    assert run.submitted_at is not None
    # Mixed outcome means we never transition to completed; completed_at NULL.
    assert run.completed_at is None


def test_run_imputation_all_skip_path_does_not_stamp_completed(
    isolated_settings: dict[str, str],
    panel_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_java: None,  # noqa: ARG001 — fixture stubs check_java_available
) -> None:
    """All-skip re-entry from ``pending`` stamps submitted but not completed.

    When every chrom's output VCF already parses cleanly, the runner
    skips them all (``attempted == 0``) and ``_stamp_status_after_run``
    is a no-op. The pending → processing transition still fires (we
    moved past pending before discovering the skips), so ``submitted_at``
    is stamped — but ``completed_at`` must remain NULL because we never
    reached the ``completed`` state.
    """
    archive_root = _archive_root(isolated_settings)
    _seed_panel(panel_root)
    imp_id = _seed_run(archive_root=archive_root, chromosomes=("1",), status="pending")
    archive = ImputationArchive.for_run(archive_root, imp_id)
    # Pre-populate the result so the chrom is skipped.
    _write_minimal_vcf(archive.result_dir / "chr1.vcf.gz", "1")

    popen_mock, captured = _make_popen_factory()
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    result = run_imputation(imp_id)
    assert result.chromosomes_skipped == ("1",)
    assert result.chromosomes_attempted == ()
    # Popen must not have been called — every chrom was skipped.
    assert len(captured) == 0

    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    # status: the pending→processing transition fires before the skip
    # discovery, so the DB row sits at 'processing'.
    assert run.status == "processing"
    assert run.submitted_at is not None
    assert run.completed_at is None


def test_import_result_completed_path_stamps_completed_at(
    isolated_settings: dict[str, str],
) -> None:
    """The import step transitions ``processing`` → ``completed`` and stamps.

    Importer reaches `_execute_import`'s update_status call and must
    pass ``set_completed=True`` so a run finishing import has its
    ``completed_at`` populated. The Beagle runner ordinarily stamps
    completed_at when the run finishes; the import re-stamp here is
    idempotent via COALESCE.
    """
    init_databases()
    archive_root = Path(isolated_settings["ARCHIVE_PATH"])

    # Hand-seed a run in 'processing' (the state import_result accepts) without
    # going through the Beagle runner — this isolates the test to the import
    # path's update_status invariants.
    with duckdb_connection() as conn:
        imp_id = insert_run(
            conn,
            input_run_ids=(1,),
            imputation_server="beagle",
            reference_panel="1000g_phase3_grch38",
            pipeline_version="imputation_prepare_v0.1.0",
            variants_input=10,
        )
        update_status(conn, imp_id, status="processing", set_submitted=True)

    archive = ImputationArchive.for_run(archive_root, imp_id)
    archive.ensure_layout()
    # Write a minimal valid imputed VCF so the ingest path has something
    # to stream.
    vcf_path = archive.result_dir / "chr22.dose.vcf.gz"
    with gzip.open(vcf_path, "wt", encoding="ascii") as out:
        out.write(
            "##fileformat=VCFv4.2\n"
            "##contig=<ID=chr22,length=50818468,assembly=GRCh38>\n"
            '##INFO=<ID=DR2,Number=1,Type=Float,Description="Dosage R-squared">\n'
            '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
            "chr22\t100\trs1\tA\tG\t.\tPASS\tDR2=0.95\tGT\t0|1\n",
        )

    import_result(imp_id, archive_root=archive_root)

    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.status == "completed"
    assert run.submitted_at is not None  # set by our seed update_status
    assert run.completed_at is not None  # set by import_result's update_status

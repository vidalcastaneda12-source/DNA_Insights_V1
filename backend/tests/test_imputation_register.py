"""Tests for :func:`genome.imputation.register_existing_result` — the JVM-free
"validate a preserved Beagle result tree and flip the run to ``completed``" fast
path (RM-7fba363 / PR 11, finding-008).

Authored blind to the implementation diff (Stage-2 test-author, independent
oracle): every assertion is written from the approved plan §5 (tests) / §6
(verification) and the *frozen* interface — ``register_existing_result`` /
``RegisterResult`` / ``RegisterError`` and the archive layout — never from the
function body (currently a ``NotImplementedError`` stub). The core behavior tests
are therefore expected to start **red** (they hit the stub); the ``implementer``
drives them green.

Fixture discipline (plan + finding-013): result VCFs are **real BGZF** (bgzip via
htslib's ``cyvcf2.Writer(mode="wz")``), never ``gzip.open`` — only a real BGZF can
exercise the truncation / silently-empty guards (finding-008 #2). The truncation
fixture is a valid BGZF with the trailing 28-byte EOF marker dropped. Result files
land at ``result/chr<c>.vcf.gz`` (register's ``_output_vcf_path``), *not* the legacy
``.dose.vcf.gz`` naming the import tests use.
"""

from __future__ import annotations

import gzip
import json
from typing import TYPE_CHECKING

import pytest

from genome.db import duckdb_connection, init_databases
from genome.imputation import (
    RegisterError,
    RegisterResult,
    register_existing_result,
)
from genome.imputation.archive import ImputationArchive
from genome.imputation.runs import fetch_run, insert_run, update_status

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixture helpers — kept local so this file is self-contained (the suite's
# convention; cf. ``test_cli_phase4._seed_one_consensus_variant``). They mirror
# the proven realistic-fixture patterns in ``test_imputation_ingest`` /
# ``test_imputation_bgzf`` (real BGZF + prepare MANIFEST.json).
# ---------------------------------------------------------------------------


def _archive_root(env: dict[str, str]) -> Path:
    from pathlib import Path  # noqa: PLC0415 — keep Path out of the typing-only block

    return Path(env["ARCHIVE_PATH"])


def _seed_pending_run(archive_root: Path) -> int:
    """Insert a fresh ``pending`` imputation run and lay out its archive tree.

    ``insert_run`` writes ``status='pending'`` with ``submitted_at`` / ``completed_at``
    NULL — the exact post-schema-rebuild shape register is meant to advance.
    """
    with duckdb_connection() as conn:
        imp_id = insert_run(
            conn,
            input_run_ids=(1,),
            imputation_server="beagle",
            reference_panel="1000g_phase3_grch38",
            pipeline_version="imputation_prepare_v0.1.0",
            variants_input=100,
        )
    ImputationArchive.for_run(archive_root, imp_id).ensure_layout()
    return imp_id


def _result_vcf(archive: ImputationArchive, chrom: str) -> Path:
    """Top-level per-chromosome result path register validates (``result/chr<c>.vcf.gz``).

    Mirrors ``beagle_runner._output_vcf_path``; chrX resolves to the top-level
    concat ``result/chrX.vcf.gz``, never the ``result/chrX_regions/`` subdir.
    """
    return archive.result_dir / f"chr{chrom}.vcf.gz"


def _write_plain_vcf(dest: Path, *, chrom: str, n_variants: int) -> None:
    """Write a plain-gzip VCF: header + ``n_variants`` trivial SNV records."""
    header = (
        "##fileformat=VCFv4.2\n"
        f"##contig=<ID={chrom},length=248956422,assembly=GRCh38>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
    )
    lines = [header]
    lines.extend(
        f"{chrom}\t{100 + i}\trs{1000 + i}\tA\tG\t.\tPASS\t.\tGT\t0|1\n" for i in range(n_variants)
    )
    with gzip.open(dest, "wt", encoding="ascii") as out:
        out.writelines(lines)


def _write_bgzf_vcf(dest: Path, *, chrom: str = "chr1", n_variants: int = 3) -> None:
    """Write a **real BGZF** VCF (htslib 28-byte EOF marker included).

    Build the records as plain gzip then transcode through ``cyvcf2.Writer(mode="wz")``
    so htslib appends the canonical BGZF EOF marker (mirrors
    ``test_imputation_ingest._write_bgzf_vcf``). ``n_variants=0`` yields a valid,
    cleanly-closed, header-only BGZF — the E2 "silently-empty" fixture that a bare
    completeness check would wrongly accept.
    """
    import cyvcf2  # noqa: PLC0415 — deferred; mirrors the production import site

    plain = dest.parent / f"{dest.name}.plain.vcf.gz"
    _write_plain_vcf(plain, chrom=chrom, n_variants=n_variants)
    reader = cyvcf2.VCF(str(plain))
    writer = cyvcf2.Writer(str(dest), reader, mode="wz")
    try:
        for record in reader:
            writer.write_record(record)
    finally:
        writer.close()
        reader.close()
    plain.unlink()


def _truncate_bgzf(path: Path) -> None:
    """Drop the trailing 28-byte BGZF EOF marker (finding-008 #2 truncation shape)."""
    path.write_bytes(path.read_bytes()[:-28])


def _write_manifest(archive: ImputationArchive, variants_per_chrom: dict[str, int]) -> None:
    """Write a minimal prepare ``MANIFEST.json`` carrying ``variants_per_chrom``."""
    archive.upload_dir.mkdir(parents=True, exist_ok=True)
    archive.upload_manifest.write_text(
        json.dumps({"variants_per_chrom": variants_per_chrom}),
        encoding="utf-8",
    )


def _assert_pending_and_unstamped(imp_id: int) -> None:
    """A fail-closed refusal leaves a pending run byte-unchanged: pending, no timestamps."""
    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.status == "pending"
    assert run.submitted_at is None
    assert run.completed_at is None


# ---------------------------------------------------------------------------
# Refusal-scenario factories: each seeds DB + archive so a spec-conformant
# ``register_existing_result`` refuses, and returns the imputation_id. Reused by
# the individual refusal tests and by the parametrized torn-state guard.
# ---------------------------------------------------------------------------


def _scn_missing_vcf(root: Path) -> int:
    """Manifest expects chr1, but there is no ``result/chr1.vcf.gz`` on disk."""
    imp_id = _seed_pending_run(root)
    _write_manifest(ImputationArchive.for_run(root, imp_id), {"1": 5})
    return imp_id


def _scn_truncated_vcf(root: Path) -> int:
    """A real BGZF ``result/chr1.vcf.gz`` with its 28-byte EOF marker stripped."""
    imp_id = _seed_pending_run(root)
    archive = ImputationArchive.for_run(root, imp_id)
    _write_manifest(archive, {"1": 3})
    chr1 = _result_vcf(archive, "1")
    _write_bgzf_vcf(chr1, chrom="chr1", n_variants=3)
    _truncate_bgzf(chr1)
    return imp_id


def _scn_empty_vcf(root: Path) -> int:
    """A valid, cleanly-closed, header-only BGZF (0 records) with manifest count>0."""
    imp_id = _seed_pending_run(root)
    archive = ImputationArchive.for_run(root, imp_id)
    _write_manifest(archive, {"1": 5})
    _write_bgzf_vcf(_result_vcf(archive, "1"), chrom="chr1", n_variants=0)
    return imp_id


def _scn_absent_manifest(root: Path) -> int:
    """A complete result exists but the prepare ``MANIFEST.json`` is gone."""
    imp_id = _seed_pending_run(root)
    archive = ImputationArchive.for_run(root, imp_id)
    _write_bgzf_vcf(_result_vcf(archive, "1"), chrom="chr1", n_variants=3)
    return imp_id


def _scn_unparseable_manifest(root: Path) -> int:
    """The ``MANIFEST.json`` is present but not valid JSON."""
    imp_id = _seed_pending_run(root)
    archive = ImputationArchive.for_run(root, imp_id)
    archive.upload_dir.mkdir(parents=True, exist_ok=True)
    archive.upload_manifest.write_text("{ this is not valid json", encoding="utf-8")
    _write_bgzf_vcf(_result_vcf(archive, "1"), chrom="chr1", n_variants=3)
    return imp_id


def _scn_extra_unmanifested(root: Path) -> int:
    """Manifest expects only chr1, but ``result/chr2.vcf.gz`` is also on disk (arch-1)."""
    imp_id = _seed_pending_run(root)
    archive = ImputationArchive.for_run(root, imp_id)
    _write_manifest(archive, {"1": 4})
    _write_bgzf_vcf(_result_vcf(archive, "1"), chrom="chr1", n_variants=4)
    _write_bgzf_vcf(_result_vcf(archive, "2"), chrom="chr2", n_variants=3)
    return imp_id


def _scn_empty_expected(root: Path) -> int:
    """Manifest keys are all off-panel ('Y'/'MT') → empty expected set after ∩ panel."""
    imp_id = _seed_pending_run(root)
    _write_manifest(ImputationArchive.for_run(root, imp_id), {"Y": 9, "MT": 3})
    return imp_id


def _scn_failed_run(root: Path) -> int:
    """A genuinely-complete tree under ``status='failed'`` (must not be laundered)."""
    imp_id = _seed_pending_run(root)
    with duckdb_connection() as conn:
        update_status(conn, imp_id, status="failed")
    archive = ImputationArchive.for_run(root, imp_id)
    _write_manifest(archive, {"1": 3})
    _write_bgzf_vcf(_result_vcf(archive, "1"), chrom="chr1", n_variants=3)
    return imp_id


def _scn_already_completed(root: Path) -> int:
    """A complete tree under a run already ``completed`` with both timestamps stamped."""
    imp_id = _seed_pending_run(root)
    with duckdb_connection() as conn:
        update_status(conn, imp_id, status="completed", set_submitted=True, set_completed=True)
    archive = ImputationArchive.for_run(root, imp_id)
    _write_manifest(archive, {"1": 3})
    _write_bgzf_vcf(_result_vcf(archive, "1"), chrom="chr1", n_variants=3)
    return imp_id


# ---------------------------------------------------------------------------
# Success behaviors
# ---------------------------------------------------------------------------


def test_flips_pending_to_completed_and_stamps_timestamps(
    isolated_settings: dict[str, str],
) -> None:
    """from: plan §5 'flips_pending_to_completed_and_stamps_timestamps'.

    §6 expected: pending→completed, both timestamps stamped (finding-007 Fix3),
    ``chromosomes_validated`` == the expected set, and the negative control —
    status-only, so NO genotype_calls are written and ``variants_output`` stays NULL.
    """
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _seed_pending_run(root)
    archive = ImputationArchive.for_run(root, imp_id)
    _write_manifest(archive, {"1": 4, "2": 3, "X": 2})
    _write_bgzf_vcf(_result_vcf(archive, "1"), chrom="chr1", n_variants=4)
    _write_bgzf_vcf(_result_vcf(archive, "2"), chrom="chr2", n_variants=3)
    _write_bgzf_vcf(_result_vcf(archive, "X"), chrom="chrX", n_variants=2)

    result = register_existing_result(imp_id, archive_root=root)

    assert isinstance(result, RegisterResult)
    assert result.status_before == "pending"
    assert result.status_after == "completed"
    assert result.chromosomes_expected == ("1", "2", "X")
    assert result.chromosomes_validated == ("1", "2", "X")
    assert result.submitted_at is not None
    assert result.completed_at is not None

    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
        genotype_calls = conn.execute("SELECT COUNT(*) FROM genotype_calls").fetchone()[0]
        master = conn.execute("SELECT COUNT(*) FROM variants_master").fetchone()[0]
    assert run is not None
    assert run.status == "completed"
    assert run.submitted_at is not None  # finding-007 Fix3
    assert run.completed_at is not None  # finding-007 Fix3
    assert run.variants_output is None  # status-only; import has not run
    assert genotype_calls == 0  # negative control: register writes no genotype_calls
    assert master == 0


def test_tolerates_chry_in_manifest_without_result_vcf(
    isolated_settings: dict[str, str],
) -> None:
    """from: plan §5 'tolerates_chry_in_manifest_without_result_vcf' — the E1 load-bearing guard.

    §6 arm3: expected set = ``variants_per_chrom`` ∩ ``PANEL_CHROMOSOMES``. The user's
    real run_0002/0003 manifests carry a 'Y' key with no ``result/chrY.vcf.gz`` (chrY is
    intentionally absent from the panel), so register must SUCCEED with chrY excluded — a
    literal manifest-key iteration would resurrect the chrY dead-gate and wrongly refuse.
    """
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _seed_pending_run(root)
    archive = ImputationArchive.for_run(root, imp_id)
    _write_manifest(archive, {"1": 4, "Y": 9})
    _write_bgzf_vcf(_result_vcf(archive, "1"), chrom="chr1", n_variants=4)
    # No result/chrY.vcf.gz on disk — chrY is dropped by the panel intersection.

    result = register_existing_result(imp_id, archive_root=root)

    assert isinstance(result, RegisterResult)
    assert result.status_after == "completed"
    assert result.chromosomes_expected == ("1",)
    assert result.chromosomes_validated == ("1",)
    assert result.submitted_at is not None
    assert result.completed_at is not None


def test_accepts_processing_pre_state(isolated_settings: dict[str, str]) -> None:
    """from: plan §4 step 3c — the pre-state gate accepts {pending, processing}, not pending only.

    A run left in ``processing`` (Beagle started, ``submitted_at`` already stamped) with a
    complete tree advances to ``completed`` exactly like the pending case; the §6 success
    output applies with ``status_before == 'processing'``.
    """
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _seed_pending_run(root)
    with duckdb_connection() as conn:
        update_status(conn, imp_id, status="processing", set_submitted=True)
    archive = ImputationArchive.for_run(root, imp_id)
    _write_manifest(archive, {"1": 3})
    _write_bgzf_vcf(_result_vcf(archive, "1"), chrom="chr1", n_variants=3)

    result = register_existing_result(imp_id, archive_root=root)

    assert isinstance(result, RegisterResult)
    assert result.status_before == "processing"
    assert result.status_after == "completed"
    assert result.chromosomes_validated == ("1",)
    assert result.submitted_at is not None
    assert result.completed_at is not None


def test_validates_chrx_top_level_concat_not_regions(
    isolated_settings: dict[str, str],
) -> None:
    """from: plan §5 'validates_chrx_top_level_concat_not_regions' (BOTH faces).

    §6: chrX is validated via the top-level concat ``result/chrX.vcf.gz`` (finding-029),
    never the ``result/chrX_regions/`` subdir. (a) top-level present + a region file present
    → success; (b) top-level missing while ``chrX_regions/`` is populated → refuse.
    """
    init_databases()
    root = _archive_root(isolated_settings)

    # (a) top-level result/chrX.vcf.gz present, plus a chrX_regions/ file that the
    #     top-level result glob must ignore → success.
    imp_ok = _seed_pending_run(root)
    arch_ok = ImputationArchive.for_run(root, imp_ok)
    _write_manifest(arch_ok, {"X": 5})
    _write_bgzf_vcf(_result_vcf(arch_ok, "X"), chrom="chrX", n_variants=3)
    _write_bgzf_vcf(arch_ok.chrx_region_result_path("nonpar"), chrom="chrX", n_variants=2)

    result = register_existing_result(imp_ok, archive_root=root)
    assert isinstance(result, RegisterResult)
    assert result.status_after == "completed"
    assert result.chromosomes_validated == ("X",)

    # (b) top-level result/chrX.vcf.gz MISSING but chrX_regions/ populated → refuse; register
    #     must not fall back to the region files.
    imp_bad = _seed_pending_run(root)
    arch_bad = ImputationArchive.for_run(root, imp_bad)
    _write_manifest(arch_bad, {"X": 5})
    _write_bgzf_vcf(arch_bad.chrx_region_result_path("nonpar"), chrom="chrX", n_variants=2)

    with pytest.raises(RegisterError):
        register_existing_result(imp_bad, archive_root=root)
    _assert_pending_and_unstamped(imp_bad)


# ---------------------------------------------------------------------------
# Fail-closed refusals — each raises RegisterError and leaves status unchanged.
# ---------------------------------------------------------------------------


def test_refuses_missing_expected_result_vcf(isolated_settings: dict[str, str]) -> None:
    """from: plan §5 'refuses_missing_expected_result_vcf' — status pending, timestamps NULL."""
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _scn_missing_vcf(root)

    with pytest.raises(RegisterError):
        register_existing_result(imp_id, archive_root=root)
    _assert_pending_and_unstamped(imp_id)


def test_refuses_truncated_result_vcf(isolated_settings: dict[str, str]) -> None:
    """from: plan §5 'refuses_truncated_result_vcf' (finding-008 #2).

    A real BGZF with the trailing 28-byte EOF marker dropped reads as zero records
    without raising on a bare open; the truncation-aware guard must refuse it loudly.
    """
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _scn_truncated_vcf(root)

    with pytest.raises(RegisterError) as exc:
        register_existing_result(imp_id, archive_root=root)
    # Pin WHICH reason fired (self-verifying vs fixture drift, review test-1): a
    # "cleanly-empty" fixture regressing into a truncated file must not green here.
    assert "truncated/unparseable" in str(exc.value)
    _assert_pending_and_unstamped(imp_id)


def test_refuses_cleanly_empty_result_vcf(isolated_settings: dict[str, str]) -> None:
    """from: plan §5 'refuses_cleanly_empty_result_vcf' — E2, COMMITTED (not contingent).

    A valid, cleanly-closed, header-only BGZF (0 records) with a manifest count > 0 is a
    silent Beagle failure. It passes a bare completeness check (which accepts 0 records),
    so register must additionally require ≥1 record when the manifest expected input.
    """
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _scn_empty_vcf(root)

    with pytest.raises(RegisterError) as exc:
        register_existing_result(imp_id, archive_root=root)
    # Pin WHICH reason fired (self-verifying vs fixture drift, review test-1): the E2
    # silently-empty path, not a truncation, must be the one that refused.
    assert "silently-empty" in str(exc.value)
    _assert_pending_and_unstamped(imp_id)


def test_refuses_absent_manifest(isolated_settings: dict[str, str]) -> None:
    """from: plan §5 'refuses_absent_manifest' — fail-closed; no manifest → no blessed set."""
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _scn_absent_manifest(root)

    with pytest.raises(RegisterError):
        register_existing_result(imp_id, archive_root=root)
    _assert_pending_and_unstamped(imp_id)


def test_refuses_unparseable_manifest(isolated_settings: dict[str, str]) -> None:
    """from: plan §5 'refuses_unparseable_manifest' — fail-closed on a corrupt MANIFEST.json."""
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _scn_unparseable_manifest(root)

    with pytest.raises(RegisterError):
        register_existing_result(imp_id, archive_root=root)
    _assert_pending_and_unstamped(imp_id)


def test_refuses_unknown_id(isolated_settings: dict[str, str]) -> None:
    """from: plan §5 'refuses_unknown_id' — an unknown id raises RegisterError."""
    init_databases()
    root = _archive_root(isolated_settings)

    with pytest.raises(RegisterError):
        register_existing_result(999, archive_root=root)


def test_refuses_failed_run(isolated_settings: dict[str, str]) -> None:
    """from: plan §5 'refuses_failed_run' — DEDICATED no-launder guard.

    §6 arm2: a genuinely-complete tree under ``status='failed'`` is still refused ('refusing
    to launder') and the status is NOT advanced to completed.
    """
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _scn_failed_run(root)

    with pytest.raises(RegisterError):
        register_existing_result(imp_id, archive_root=root)

    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.status == "failed"  # not laundered to completed


def test_refuses_already_completed_run(isolated_settings: dict[str, str]) -> None:
    """from: plan §5 'refuses_already_completed_run' — idempotent refuse; no re-stamp."""
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _scn_already_completed(root)
    with duckdb_connection() as conn:
        before = fetch_run(conn, imp_id)
    assert before is not None

    with pytest.raises(RegisterError):
        register_existing_result(imp_id, archive_root=root)

    with duckdb_connection() as conn:
        after = fetch_run(conn, imp_id)
    assert after is not None
    assert after.status == "completed"
    assert after.submitted_at == before.submitted_at  # no COALESCE re-stamp
    assert after.completed_at == before.completed_at


def test_refuses_extra_unmanifested_result_vcf(isolated_settings: dict[str, str]) -> None:
    """from: plan audit_amendments.adopted 'arch1_glob_reconciliation'.

    Register reconciles the manifest ∩ panel expected set against
    ``archive.list_result_vcfs()`` (the exact set import globs) and refuses on
    symmetric-difference. Here ``result/chr2.vcf.gz`` is on disk but absent from the
    manifest, so 'register blessed complete' would disagree with what import loads.
    """
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _scn_extra_unmanifested(root)

    with pytest.raises(RegisterError):
        register_existing_result(imp_id, archive_root=root)
    _assert_pending_and_unstamped(imp_id)


def test_refuses_empty_expected_set_after_panel_intersection(
    isolated_settings: dict[str, str],
) -> None:
    """from: plan audit_amendments.adopted 'F5_empty_expected_test'.

    A manifest whose only keys are off-panel ('Y'/'MT') intersects PANEL_CHROMOSOMES to the
    empty set; register refuses rather than trivially 'succeeding' on zero chromosomes.
    """
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _scn_empty_expected(root)

    with pytest.raises(RegisterError):
        register_existing_result(imp_id, archive_root=root)
    _assert_pending_and_unstamped(imp_id)


# ---------------------------------------------------------------------------
# Multi-chromosome ALL-OR-NOTHING completeness (review ptest-1). Every prior
# refusal that reaches the per-chromosome loop uses a single-element expected
# set, and the only multi-chrom test ({1,2,X}) is fully complete — so a
# regression flipping "all must validate" -> "at least one validates" (or a
# first-failure short-circuit) would survive. These pin the invariant at a >1
# expected set with a genuinely mixed result tree.
# ---------------------------------------------------------------------------


def test_refuses_multi_chrom_partial_failure(isolated_settings: dict[str, str]) -> None:
    """from: review ptest-1 (RM-7fba363) — ALL-OR-NOTHING at a multi-chromosome expected set.

    A manifest with a >1 expected set where a strict subset ({1}) is complete but the rest
    ({2}) is NOT must still refuse: register requires EVERY expected chromosome to validate,
    not just one. A regression flipping all->any would let chr1's valid VCF mask chr2's
    truncated one and wrongly succeed. The refusal names the FAILING chrom (chr2) with its
    reason (message format ``chr{chrom} ({reason})``) and does not name the passing chr1.
    """
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _seed_pending_run(root)
    archive = ImputationArchive.for_run(root, imp_id)
    _write_manifest(archive, {"1": 4, "2": 3})
    _write_bgzf_vcf(_result_vcf(archive, "1"), chrom="chr1", n_variants=4)  # chr1 complete
    chr2 = _result_vcf(archive, "2")
    _write_bgzf_vcf(chr2, chrom="chr2", n_variants=3)
    _truncate_bgzf(chr2)  # chr2 fails — its 28-byte BGZF EOF marker is stripped

    with pytest.raises(RegisterError) as exc:
        register_existing_result(imp_id, archive_root=root)

    msg = str(exc.value)
    assert "chr2" in msg  # the failing chrom is named ...
    assert "truncated/unparseable" in msg  # ... with its reason
    assert "chr1" not in msg  # the complete chrom is NOT reported as a failure
    _assert_pending_and_unstamped(imp_id)


def test_refuses_multi_chrom_names_all_failures(isolated_settings: dict[str, str]) -> None:
    """from: review ptest-1 (RM-7fba363), multi-failure strengthening — 'collect all, name all'.

    When more than one expected chromosome fails, the refusal names EVERY failing chrom with
    its own reason — the completeness loop collects all failures rather than short-circuiting
    on the first. chr1 is missing and chr2 is a cleanly-empty BGZF (two distinct reasons); a
    first-failure short-circuit would drop chr2 (and its reason) from the message.
    """
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = _seed_pending_run(root)
    archive = ImputationArchive.for_run(root, imp_id)
    _write_manifest(archive, {"1": 4, "2": 3})
    # chr1: no result/chr1.vcf.gz on disk -> 'missing'.
    # chr2: valid header-only BGZF (0 records) with manifest count > 0 -> 'silently-empty'.
    _write_bgzf_vcf(_result_vcf(archive, "2"), chrom="chr2", n_variants=0)

    with pytest.raises(RegisterError) as exc:
        register_existing_result(imp_id, archive_root=root)

    msg = str(exc.value)
    assert "chr1" in msg  # first failure named ...
    assert "missing" in msg  # ... with its reason
    assert "chr2" in msg  # second failure named too — no first-failure short-circuit ...
    assert "silently-empty" in msg  # ... with its distinct reason
    _assert_pending_and_unstamped(imp_id)


# ---------------------------------------------------------------------------
# Torn-state invariant — no refusal ever advances the run (the single
# update_status flip fires only after every read-only validation passes).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(_scn_missing_vcf, id="missing_vcf"),
        pytest.param(_scn_truncated_vcf, id="truncated_vcf"),
        pytest.param(_scn_empty_vcf, id="cleanly_empty_vcf"),
        pytest.param(_scn_absent_manifest, id="absent_manifest"),
        pytest.param(_scn_unparseable_manifest, id="unparseable_manifest"),
        pytest.param(_scn_extra_unmanifested, id="extra_unmanifested_vcf"),
        pytest.param(_scn_empty_expected, id="empty_expected_set"),
        pytest.param(_scn_failed_run, id="failed_pre_state"),
        pytest.param(_scn_already_completed, id="already_completed_pre_state"),
    ],
)
def test_status_unchanged_on_every_refusal(
    isolated_settings: dict[str, str],
    scenario: Callable[[Path], int],
) -> None:
    """from: plan §5 'status_unchanged_on_every_refusal' (parametrized) + §6 ordering invariant.

    Every refusal mode must leave the run byte-identical — status and both timestamps
    unchanged — proving the single ``update_status`` flip fires strictly after all
    read-only validation, so no torn state is ever visible.
    """
    init_databases()
    root = _archive_root(isolated_settings)
    imp_id = scenario(root)
    with duckdb_connection() as conn:
        before = fetch_run(conn, imp_id)
    assert before is not None

    with pytest.raises(RegisterError):
        register_existing_result(imp_id, archive_root=root)

    with duckdb_connection() as conn:
        after = fetch_run(conn, imp_id)
    assert after is not None
    assert after.status == before.status
    assert after.submitted_at == before.submitted_at
    assert after.completed_at == before.completed_at

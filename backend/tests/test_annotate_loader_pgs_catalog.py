"""Tests for :mod:`genome.annotate.loaders.pgs_catalog`.

Covers the per-field coercions (empty / NA / NR / dash → NULL,
``Number of Variants`` integer parse, ``Publication Date`` year
extraction, leading-number extraction from
``"<estimate> [<lower>,<upper>]"`` performance cells, multi-EFO
truncation in ``Mapped Trait(s) (EFO ID)``), the four per-file
parsers, the in-memory join + max-across-cohorts performance
reduction, the gzipped-TAR bundle helper (happy path, missing
entry, directory entry skipped), the version-resolution path
(release-current endpoint ``{"date": "YYYY-MM-DD"}`` → ``YYYY_MM_DD``,
defensive ``release_date`` / ``releasedate`` aliases, malformed-JSON
loud-fail, HTTP-5xx propagation), the end-to-end ``refresh`` flow
against the checked-in 10-score fixture (wrapped in a TAR.GZ to
match the upstream distribution shape), the supersession transaction
(new version vs same-version ``--force``), the audited refusal path
with ``external_calls_enabled=false``, a 5K-row benchmark guard
against the locked < 30 s ceiling, and the CLI smoke against
``genome annotate refresh --source pgs_catalog``.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from typer.testing import CliRunner

from genome.annotate import downloads as annotate_downloads
from genome.annotate.loaders import pgs_catalog as pgs_loader
from genome.annotate.loaders.pgs_catalog import (
    _CACHE_FILENAME,
    _CHUNK_SIZE,
    _EFO_TRAITS_MEMBER,
    _PERFORMANCE_MEMBER,
    _PUBLICATIONS_MEMBER,
    _SCORES_MEMBER,
    _TRAIT_CATEGORY_CACHE_FILENAME,
    _empty_to_none,
    _first_efo_id,
    _format_version,
    _insert_chunk,
    _iter_chunks,
    _join_metadata,
    _open_csv_from_bundle,
    _parse_int,
    _parse_leading_number,
    _parse_performance,
    _parse_publications,
    _parse_release_payload,
    _parse_scores,
    _parse_trait_categories,
    _parse_traits,
    _parse_year,
    _ParsedRow,
    _ParseStats,
    _RawPerformanceRow,
    _reduce_performance,
    _resolve_version_via_release_latest,
    _stream_bulk_insert,
)
from genome.annotate.registry import get_loader
from genome.cli import app
from genome.db import duckdb_connection, init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.privacy.external_client import (
    ExternalCallError,
    ExternalCallsDisabledError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pgs_catalog"


# ---------------------------------------------------------------------------
# Per-test isolation (mirror the GWAS Catalog / ClinVar / PharmGKB pattern).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ensure_pgs_registered() -> Iterator[None]:
    """Re-register the loader at test start.

    Other annotate test files install autouse fixtures that wipe the
    registry via ``_clear_loaders_for_testing()`` to keep their cases
    hermetic. Re-registering here makes our tests order-independent.
    We pop again on teardown so we don't leak the registration into
    the next test file.
    """
    from genome.annotate.registry import _LOADERS, register_loader  # noqa: PLC0415

    _LOADERS.pop("pgs_catalog", None)
    register_loader("pgs_catalog", pgs_loader.refresh)
    try:
        yield
    finally:
        _LOADERS.pop("pgs_catalog", None)


@pytest.fixture(autouse=True)
def _isolated(
    isolated_settings: dict[str, str],  # noqa: ARG001 — required by the fixture chain
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Per-test isolation: tmp dirs, annotations root, fresh DBs."""
    annotations_root = tmp_path / "annotations-root"
    monkeypatch.setenv("ANNOTATIONS_DOWNLOAD_ROOT", str(annotations_root))
    from genome.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    try:
        yield annotations_root
    finally:
        get_settings.cache_clear()


def _build_bundle(tmp_path: Path, fixture_dir: Path = _FIXTURE_DIR) -> Path:
    """Pack the four fixture CSVs into a ``.tar.gz`` at the upstream member names.

    The upstream packaging uses absolute paths, so the TAR member
    names include a leading ``/`` (e.g. ``/pgs_all_metadata_scores.csv``).
    We mirror that shape so the test exercises the loader's
    ``endswith``-based matching.
    """
    bundle_path = tmp_path / "pgs_all_metadata.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as tf:
        for member in (
            _SCORES_MEMBER,
            _PUBLICATIONS_MEMBER,
            _EFO_TRAITS_MEMBER,
            _PERFORMANCE_MEMBER,
        ):
            tf.add(fixture_dir / member, arcname=f"/{member}")
    return bundle_path


def _patch_download_to_cache(
    monkeypatch: pytest.MonkeyPatch,
    bundle_path: Path,
    trait_categories_path: Path | None = None,
) -> dict[str, int]:
    """Replace ``download_to_cache`` with a filename-aware stub.

    The loader issues two audited downloads per refresh: the metadata
    bundle (``pgs_all_metadata.tar.gz``) and the trait-category REST
    payload (``trait_categories.json``). The stub dispatches on the
    caller's ``filename`` argument and returns the appropriate path
    + hash for each. ``trait_categories_path`` defaults to the
    checked-in fixture file when omitted.
    """
    import hashlib  # noqa: PLC0415

    if trait_categories_path is None:
        trait_categories_path = _FIXTURE_DIR / "trait_categories.json"

    counter: dict[str, int] = {"calls": 0, "bundle_calls": 0, "trait_cat_calls": 0}
    bundle_digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    bundle_size = bundle_path.stat().st_size
    cat_digest = hashlib.sha256(trait_categories_path.read_bytes()).hexdigest()
    cat_size = trait_categories_path.stat().st_size

    def _stub(
        source_db: str,  # noqa: ARG001
        url: str,  # noqa: ARG001
        filename: str,
        *,
        resource_id: str,  # noqa: ARG001
        force: bool = False,  # noqa: ARG001
    ) -> annotate_downloads.DownloadResult:
        counter["calls"] += 1
        if filename == _TRAIT_CATEGORY_CACHE_FILENAME:
            counter["trait_cat_calls"] += 1
            return annotate_downloads.DownloadResult(
                path=trait_categories_path,
                sha256=cat_digest,
                size_bytes=cat_size,
            )
        if filename == _CACHE_FILENAME:
            counter["bundle_calls"] += 1
            return annotate_downloads.DownloadResult(
                path=bundle_path,
                sha256=bundle_digest,
                size_bytes=bundle_size,
            )
        msg = f"unexpected download filename in stub: {filename!r}"
        raise AssertionError(msg)

    monkeypatch.setattr(pgs_loader, "download_to_cache", _stub)
    return counter


def _patch_resolve_version(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> dict[str, int]:
    """Replace ``_resolve_version_via_release_latest`` with a stub."""
    counter: dict[str, int] = {"calls": 0}

    def _stub() -> str:
        counter["calls"] += 1
        return version

    monkeypatch.setattr(pgs_loader, "_resolve_version_via_release_latest", _stub)
    return counter


def _audit_rows() -> list[tuple[object, ...]]:
    with sqlcipher_connection() as conn:
        return conn.execute(
            "SELECT action_type, resource_type, resource_id, operation_details,"
            " external_call, external_endpoint, external_payload_hash"
            " FROM audit_log ORDER BY log_id",
        ).fetchall()


# ---------------------------------------------------------------------------
# Field-level coercions.
# ---------------------------------------------------------------------------


def test_empty_to_none_covers_pgs_missing_tokens() -> None:
    """PGS Catalog uses empty / NA / NR / dash interchangeably for missing."""
    assert _empty_to_none("") is None
    assert _empty_to_none("  ") is None
    assert _empty_to_none("NA") is None
    assert _empty_to_none("NR") is None
    assert _empty_to_none("-") is None
    assert _empty_to_none("0.42") == "0.42"
    assert _empty_to_none(" Body mass index ") == "Body mass index"


def test_parse_int_handles_missing_and_valid() -> None:
    assert _parse_int("") is None
    assert _parse_int("NR") is None
    assert _parse_int("not-a-number") is None
    expected = 1200
    assert _parse_int("1200") == expected


def test_parse_year_extracts_from_iso_date() -> None:
    """``Publication Date`` ships as ``YYYY-MM-DD``."""
    expected_year = 2015
    assert _parse_year("2015-04-08") == expected_year
    expected_year_2 = 2024
    assert _parse_year("2024-09-30") == expected_year_2


def test_parse_year_handles_slash_separator() -> None:
    """Defensive: a ``YYYY/MM/DD`` form is also accepted."""
    expected_year = 2020
    assert _parse_year("2020/01/15") == expected_year


def test_parse_year_returns_none_for_freeform_text() -> None:
    """Non-date text returns ``None`` rather than silently truncating."""
    assert _parse_year("April 27, 2026") is None
    assert _parse_year("") is None
    assert _parse_year("NR") is None


def test_parse_leading_number_extracts_point_estimate() -> None:
    """The OR / AUROC cells ship as ``"<estimate> [<lower>,<upper>]"``."""
    assert _parse_leading_number("0.622 [0.619,0.627]") == pytest.approx(0.622)
    assert _parse_leading_number("1.55 [1.52,1.58]") == pytest.approx(1.55)
    assert _parse_leading_number("1.80") == pytest.approx(1.80)


def test_parse_leading_number_handles_scientific_notation() -> None:
    """A scientific-notation point estimate parses cleanly."""
    assert _parse_leading_number("3.4e-2 [1.0e-2,5.0e-2]") == pytest.approx(3.4e-2)


def test_parse_leading_number_returns_none_for_missing_and_text() -> None:
    """Empty / NR / pure-text cells return None."""
    assert _parse_leading_number("") is None
    assert _parse_leading_number("NR") is None
    assert _parse_leading_number("[NR]") is None
    assert _parse_leading_number("no estimate reported") is None


def test_first_efo_id_single_value() -> None:
    """The common case: one EFO ID, no truncation."""
    efo, truncated = _first_efo_id("EFO_0001065")
    assert efo == "EFO_0001065"
    assert truncated is False


def test_first_efo_id_multi_value_keeps_first_logs_truncation() -> None:
    """Multi-valued EFO → keep first; truncation flag is True."""
    efo, truncated = _first_efo_id("EFO_0001065,EFO_0000384")
    assert efo == "EFO_0001065"
    assert truncated is True


def test_first_efo_id_empty_returns_none() -> None:
    efo, truncated = _first_efo_id("")
    assert efo is None
    assert truncated is False
    efo, truncated = _first_efo_id("NR")
    assert efo is None
    assert truncated is False


# ---------------------------------------------------------------------------
# Release-current payload parsing.
# ---------------------------------------------------------------------------


def test_parse_release_payload_canonical_shape() -> None:
    """The live PGS Catalog shape: ``{"date": "YYYY-MM-DD", ...}``."""
    assert _parse_release_payload(
        {"date": "2026-05-07", "score_count": 5},
    ) == date(2026, 5, 7)


def test_parse_release_payload_release_date_alias() -> None:
    """Defensive accept of ``release_date``."""
    assert _parse_release_payload(
        {"release_date": "2025-08-12"},
    ) == date(2025, 8, 12)


def test_parse_release_payload_releasedate_alias() -> None:
    """Defensive accept of ``releasedate``."""
    assert _parse_release_payload(
        {"releasedate": "2024-12-01"},
    ) == date(2024, 12, 1)


def test_parse_release_payload_prefers_date_over_aliases() -> None:
    """When ``date`` is present it always wins."""
    assert _parse_release_payload(
        {"date": "2026-05-07", "release_date": "1999-12-31"},
    ) == date(2026, 5, 7)


def test_parse_release_payload_missing_field_raises() -> None:
    with pytest.raises(ValueError, match="missing a 'date'"):
        _parse_release_payload({"score_count": 5})


def test_parse_release_payload_non_string_value_raises() -> None:
    with pytest.raises(ValueError, match="missing a 'date'"):
        _parse_release_payload({"date": 20260507})


def test_parse_release_payload_bad_format_raises() -> None:
    with pytest.raises(ValueError, match="does not match YYYY-MM-DD"):
        _parse_release_payload({"date": "May 7, 2026"})


def test_parse_release_payload_non_object_raises() -> None:
    with pytest.raises(ValueError, match="expected a JSON object"):
        _parse_release_payload("2026-05-07")


def test_format_version_renders_yyyy_mm_dd_with_underscores() -> None:
    """Matches the ClinVar / GWAS Catalog loader convention."""
    assert _format_version(date(2026, 5, 7)) == "2026_05_07"
    assert _format_version(date(2024, 1, 5)) == "2024_01_05"


# ---------------------------------------------------------------------------
# Version-resolution audited GET (httpx mock-transport).
# ---------------------------------------------------------------------------


def _enable_external_calls() -> None:
    with sqlcipher_connection() as conn:
        conn.execute(
            "UPDATE user_preferences SET pref_value='true' WHERE pref_key='external_calls_enabled'",
        )
        conn.commit()


def _patched_external_client_with_handler(
    monkeypatch: pytest.MonkeyPatch,
    handler,  # type: ignore[no-untyped-def]
) -> None:
    """Force ``_resolve_version_via_release_latest`` to use a MockTransport-backed httpx."""
    real_client_cls = httpx.Client

    def _factory(
        *_args: object,
        timeout: float = 30.0,  # noqa: ARG001
        follow_redirects: bool = False,  # noqa: ARG001
        **_kwargs: object,
    ) -> httpx.Client:
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(pgs_loader.httpx, "Client", _factory)


def test_resolve_version_extracts_date_from_release_payload() -> None:
    """The live shape: ``{"date": "YYYY-MM-DD", ...}`` → ``YYYY_MM_DD``."""
    init_databases()
    _enable_external_calls()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == pgs_loader.PGS_RELEASE_LATEST_URL
        return httpx.Response(
            200,
            json={
                "date": "2026-05-07",
                "score_count": 5,
                "performance_count": 30,
                "publication_count": 9,
                "efotrait_count": 1,
            },
        )

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        version = _resolve_version_via_release_latest()
    finally:
        monkeypatch.undo()
    assert version == "2026_05_07"


def test_resolve_version_accepts_release_date_alias() -> None:
    """Defensive: a payload using ``release_date`` still resolves."""
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"release_date": "2025-08-12"})

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        version = _resolve_version_via_release_latest()
    finally:
        monkeypatch.undo()
    assert version == "2025_08_12"


def test_resolve_version_raises_on_malformed_payload() -> None:
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"score_count": 5})

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        with pytest.raises(ValueError, match="missing a 'date'"):
            _resolve_version_via_release_latest()
    finally:
        monkeypatch.undo()


def test_resolve_version_raises_on_non_json_body() -> None:
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>EBI gateway error</html>")

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        with pytest.raises(ValueError, match="not valid JSON"):
            _resolve_version_via_release_latest()
    finally:
        monkeypatch.undo()


def test_resolve_version_propagates_http_5xx() -> None:
    init_databases()
    _enable_external_calls()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    monkeypatch = pytest.MonkeyPatch()
    try:
        _patched_external_client_with_handler(monkeypatch, handler)
        with pytest.raises(ExternalCallError, match="HTTP 503"):
            _resolve_version_via_release_latest()
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# Per-file parser tests.
# ---------------------------------------------------------------------------


def test_parse_scores_loads_expected_pgs_ids() -> None:
    """All 10 fixture scores parse cleanly into the dict."""
    stats = _ParseStats()
    with (_FIXTURE_DIR / _SCORES_MEMBER).open(encoding="utf-8", newline="") as fh:
        scores = _parse_scores(fh, stats)
    expected_total = 10
    assert len(scores) == expected_total
    assert stats.rows_read_scores == expected_total
    expected_ids = {f"PGS{i:06d}" for i in range(1, 11)}
    assert set(scores) == expected_ids


def test_parse_scores_truncates_multi_efo() -> None:
    """PGS000003 has ``EFO_0001065,EFO_0000384``: keeps first, counts truncation."""
    stats = _ParseStats()
    with (_FIXTURE_DIR / _SCORES_MEMBER).open(encoding="utf-8", newline="") as fh:
        scores = _parse_scores(fh, stats)
    assert scores["PGS000003"].trait_efo == "EFO_0001065"
    assert stats.truncated_trait_efo == 1


def test_parse_scores_handles_nr_variants_total() -> None:
    """PGS000008's ``Number of Variants`` is ``NR`` → variants_total=None."""
    stats = _ParseStats()
    with (_FIXTURE_DIR / _SCORES_MEMBER).open(encoding="utf-8", newline="") as fh:
        scores = _parse_scores(fh, stats)
    assert scores["PGS000008"].variants_total is None
    expected_pgs1_variants = 77
    assert scores["PGS000001"].variants_total == expected_pgs1_variants


def test_parse_scores_captures_ancestry_columns() -> None:
    """The two ancestry columns map to distinct destination fields."""
    stats = _ParseStats()
    with (_FIXTURE_DIR / _SCORES_MEMBER).open(encoding="utf-8", newline="") as fh:
        scores = _parse_scores(fh, stats)
    pgs1 = scores["PGS000001"]
    assert pgs1.ancestry_distribution == "European:100"
    assert pgs1.reference_population == "European:80|Not Reported:20"


def test_parse_publications_extracts_year() -> None:
    """Publications parse PMID + DOI + year from ``Publication Date``."""
    stats = _ParseStats()
    with (_FIXTURE_DIR / _PUBLICATIONS_MEMBER).open(encoding="utf-8", newline="") as fh:
        publications = _parse_publications(fh, stats)
    expected_total = 9
    assert len(publications) == expected_total
    pgp1 = publications["PGP000001"]
    expected_year = 2015
    assert pgp1.publication_year == expected_year
    assert pgp1.publication_pmid == "10000001"
    assert pgp1.publication_doi == "10.1/example"


def test_parse_traits_emits_none_category_per_design() -> None:
    """The bundle's EFO CSV has no Trait Category column; loader emits None.

    The category for the schema's ``trait_category`` field flows through
    the separate ``_parse_trait_categories`` REST helper instead.
    """
    stats = _ParseStats()
    with (_FIXTURE_DIR / _EFO_TRAITS_MEMBER).open(encoding="utf-8", newline="") as fh:
        traits = _parse_traits(fh, stats)
    expected_total = 8
    assert len(traits) == expected_total
    assert all(t.trait_category is None for t in traits.values())


# ---------------------------------------------------------------------------
# Trait-category REST payload parser.
# ---------------------------------------------------------------------------


def test_parse_trait_categories_happy_path() -> None:
    """The fixture JSON resolves into the expected efo_id → category map."""
    stats = _ParseStats()
    cats = _parse_trait_categories(_FIXTURE_DIR / "trait_categories.json", stats)
    assert cats["EFO_0001065"] == "Body measurement"
    assert cats["EFO_0007777"] == "Cardiovascular disease"
    assert cats["MONDO_0004989"] == "Other trait"
    assert cats["EFO_0008888"] == "Other measurement"
    expected_categories = 4
    assert stats.rows_read_trait_categories == expected_categories


def test_parse_trait_categories_raises_on_pagination(tmp_path: Path) -> None:
    """A paginated response surfaces a clear loud-fail error."""
    payload_path = tmp_path / "paginated.json"
    payload_path.write_text(
        json.dumps(
            {
                "count": 50,
                "next": "https://www.pgscatalog.org/rest/trait_category/all?offset=50",
                "previous": None,
                "results": [],
            },
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="paginated response"):
        _parse_trait_categories(payload_path, _ParseStats())


def test_parse_trait_categories_raises_on_missing_results(tmp_path: Path) -> None:
    """A payload without a ``results`` list raises with the live keys."""
    payload_path = tmp_path / "no_results.json"
    payload_path.write_text(json.dumps({"count": 0, "next": None}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing 'results'"):
        _parse_trait_categories(payload_path, _ParseStats())


def test_parse_trait_categories_raises_on_non_object_payload(tmp_path: Path) -> None:
    """A JSON array (not object) at the top level raises a clear error."""
    payload_path = tmp_path / "array.json"
    payload_path.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON object"):
        _parse_trait_categories(payload_path, _ParseStats())


def test_parse_trait_categories_records_duplicate_efos(tmp_path: Path) -> None:
    """An EFO listed in two categories is counted under ``extra``."""
    payload_path = tmp_path / "dup.json"
    payload_path.write_text(
        json.dumps(
            {
                "count": 2,
                "next": None,
                "previous": None,
                "results": [
                    {
                        "label": "Category A",
                        "efotraits": [{"id": "EFO_0001", "label": "x"}],
                    },
                    {
                        "label": "Category B",
                        "efotraits": [{"id": "EFO_0001", "label": "x"}],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    stats = _ParseStats()
    cats = _parse_trait_categories(payload_path, stats)
    # Last write wins -- "Category B" overrides "Category A".
    assert cats["EFO_0001"] == "Category B"
    assert stats.extra.get("efo_in_multiple_categories") == 1


def test_parse_trait_categories_skips_malformed_entries(tmp_path: Path) -> None:
    """Non-dict results, missing labels, missing efotraits lists are skipped."""
    payload_path = tmp_path / "messy.json"
    payload_path.write_text(
        json.dumps(
            {
                "count": 4,
                "next": None,
                "previous": None,
                "results": [
                    "not-a-dict",
                    {"label": "", "efotraits": [{"id": "EFO_X", "label": "x"}]},
                    {"label": "Cat", "efotraits": "not-a-list"},
                    {
                        "label": "Cat",
                        "efotraits": [
                            "not-a-dict",
                            {"id": "", "label": "empty"},
                            {"id": "EFO_OK", "label": "good"},
                        ],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    cats = _parse_trait_categories(payload_path, _ParseStats())
    assert cats == {"EFO_OK": "Cat"}


def test_parse_performance_groups_by_pgs_id() -> None:
    """Performance entries grouped by PGS ID; multi-cohort scores get list>1."""
    stats = _ParseStats()
    with (_FIXTURE_DIR / _PERFORMANCE_MEMBER).open(encoding="utf-8", newline="") as fh:
        performance = _parse_performance(fh, stats)
    expected_rows = 12
    assert stats.rows_read_performance == expected_rows
    expected_pgs7_cohorts = 3
    assert len(performance["PGS000007"]) == expected_pgs7_cohorts
    expected_pgs9_cohorts = 2
    assert len(performance["PGS000009"]) == expected_pgs9_cohorts
    assert "PGS000006" not in performance  # the scores_without_performance row


def test_parse_performance_extracts_point_estimates() -> None:
    """Performance cells like ``"1.55 [...]"`` → leading float; AUROC bracket parses."""
    stats = _ParseStats()
    with (_FIXTURE_DIR / _PERFORMANCE_MEMBER).open(encoding="utf-8", newline="") as fh:
        performance = _parse_performance(fh, stats)
    pgs1 = performance["PGS000001"][0]
    assert pgs1.auc == pytest.approx(0.622)
    assert pgs1.or_per_sd == pytest.approx(1.55)


def test_parse_performance_handles_nr_in_one_axis() -> None:
    """PGS000009 has one row with ``NR`` AUC and one with ``NR`` OR."""
    stats = _ParseStats()
    with (_FIXTURE_DIR / _PERFORMANCE_MEMBER).open(encoding="utf-8", newline="") as fh:
        performance = _parse_performance(fh, stats)
    pgs9 = performance["PGS000009"]
    aucs = [e.auc for e in pgs9]
    ors = [e.or_per_sd for e in pgs9]
    assert None in aucs
    assert None in ors


def test_parse_scores_missing_required_header_raises() -> None:
    """Loud-fail when the scores CSV header drifts."""
    bad = "Polygenic Score (PGS) ID,PGS Name\nPGS000001,Foo\n"
    with pytest.raises(ValueError, match="missing expected columns"):
        _parse_scores(io.StringIO(bad), _ParseStats())


def test_parse_publications_missing_required_header_raises() -> None:
    bad = "PGS Publication/Study (PGP) ID,First Author\nPGP000001,Smith\n"
    with pytest.raises(ValueError, match="missing expected columns"):
        _parse_publications(io.StringIO(bad), _ParseStats())


def test_parse_traits_missing_required_header_raises() -> None:
    bad = "Ontology Trait ID\nEFO_0001065\n"
    with pytest.raises(ValueError, match="missing expected columns"):
        _parse_traits(io.StringIO(bad), _ParseStats())


def test_parse_performance_missing_required_header_raises() -> None:
    bad = "PGS Performance Metric (PPM) ID\nPPM000001\n"
    with pytest.raises(ValueError, match="missing expected columns"):
        _parse_performance(io.StringIO(bad), _ParseStats())


# ---------------------------------------------------------------------------
# Bundle TAR helper.
# ---------------------------------------------------------------------------


def test_open_csv_from_bundle_yields_scores_member(tmp_path: Path) -> None:
    """Happy path: helper streams the named CSV out of the bundle."""
    bundle = _build_bundle(tmp_path)
    with _open_csv_from_bundle(bundle, _SCORES_MEMBER) as fh:
        first_line = fh.readline()
    assert first_line.startswith("Polygenic Score (PGS) ID")


def test_open_csv_from_bundle_handles_leading_slash(tmp_path: Path) -> None:
    """Bundle members carry a leading ``/`` (absolute paths); endswith matches."""
    bundle = tmp_path / "weird.tar.gz"
    src = _FIXTURE_DIR / _PERFORMANCE_MEMBER
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(src, arcname=f"/some/nested/path/{_PERFORMANCE_MEMBER}")
    with _open_csv_from_bundle(bundle, _PERFORMANCE_MEMBER) as fh:
        first_line = fh.readline()
    assert first_line.startswith("PGS Performance Metric (PPM) ID")


def test_open_csv_from_bundle_rejects_missing_entry(tmp_path: Path) -> None:
    """A bundle without the expected entry surfaces a clear error."""
    bundle = tmp_path / "no_scores.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(_FIXTURE_DIR / _PUBLICATIONS_MEMBER, arcname=_PUBLICATIONS_MEMBER)
    with (
        pytest.raises(ValueError, match="missing expected entry"),
        _open_csv_from_bundle(bundle, _SCORES_MEMBER),
    ):
        pass


def test_open_csv_from_bundle_skips_directory_entries(tmp_path: Path) -> None:
    """A directory member before the CSV must not short-circuit the scan."""
    bundle = tmp_path / "with_dir.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        dirinfo = tarfile.TarInfo(name="some_dir/")
        dirinfo.type = tarfile.DIRTYPE
        tf.addfile(dirinfo)
        tf.add(_FIXTURE_DIR / _SCORES_MEMBER, arcname=f"/{_SCORES_MEMBER}")
    with _open_csv_from_bundle(bundle, _SCORES_MEMBER) as fh:
        first_line = fh.readline()
    assert first_line.startswith("Polygenic Score (PGS) ID")


# ---------------------------------------------------------------------------
# Join + max-reduction.
# ---------------------------------------------------------------------------


def _load_fixture_into_dicts() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, list[_RawPerformanceRow]],
    dict[str, str],
    _ParseStats,
]:
    """Run the four per-file parsers + the trait_category JSON loader against the fixture."""
    stats = _ParseStats()
    with (_FIXTURE_DIR / _SCORES_MEMBER).open(encoding="utf-8", newline="") as fh:
        scores = _parse_scores(fh, stats)
    with (_FIXTURE_DIR / _PUBLICATIONS_MEMBER).open(encoding="utf-8", newline="") as fh:
        publications = _parse_publications(fh, stats)
    with (_FIXTURE_DIR / _EFO_TRAITS_MEMBER).open(encoding="utf-8", newline="") as fh:
        traits = _parse_traits(fh, stats)
    with (_FIXTURE_DIR / _PERFORMANCE_MEMBER).open(encoding="utf-8", newline="") as fh:
        performance = _parse_performance(fh, stats)
    trait_categories = _parse_trait_categories(
        _FIXTURE_DIR / "trait_categories.json",
        stats,
    )
    return scores, publications, traits, performance, trait_categories, stats  # type: ignore[return-value]


def test_join_metadata_emits_one_row_per_pgs() -> None:
    """10 scores → 10 ``_ParsedRow`` instances, sorted by pgs_id."""
    scores, publications, traits, performance, trait_categories, stats = _load_fixture_into_dicts()
    rows = _join_metadata(
        scores,
        publications,
        traits,
        performance,
        trait_categories,
        stats,  # type: ignore[arg-type]
    )
    expected_total = 10
    assert len(rows) == expected_total
    assert [r.pgs_id for r in rows] == [f"PGS{i:06d}" for i in range(1, 11)]


def test_join_metadata_orphan_publication_counter() -> None:
    """PGS000004 references PGP999999 → orphan_publication_refs=1; row still emits."""
    scores, publications, traits, performance, trait_categories, stats = _load_fixture_into_dicts()
    rows = _join_metadata(
        scores,
        publications,
        traits,
        performance,
        trait_categories,
        stats,  # type: ignore[arg-type]
    )
    assert stats.orphan_publication_refs == 1
    pgs4 = next(r for r in rows if r.pgs_id == "PGS000004")
    assert pgs4.publication_pmid is None
    assert pgs4.publication_doi is None
    assert pgs4.publication_year is None


def test_join_metadata_orphan_trait_counter() -> None:
    """PGS000005 references EFO_9999999 → orphan_trait_refs=1."""
    scores, publications, traits, performance, trait_categories, stats = _load_fixture_into_dicts()
    _join_metadata(
        scores,
        publications,
        traits,
        performance,
        trait_categories,
        stats,  # type: ignore[arg-type]
    )
    assert stats.orphan_trait_refs == 1


def test_join_metadata_scores_without_performance_counter() -> None:
    """PGS000006 has no performance entries → scores_without_performance=1."""
    scores, publications, traits, performance, trait_categories, stats = _load_fixture_into_dicts()
    rows = _join_metadata(
        scores,
        publications,
        traits,
        performance,
        trait_categories,
        stats,  # type: ignore[arg-type]
    )
    assert stats.scores_without_performance == 1
    pgs6 = next(r for r in rows if r.pgs_id == "PGS000006")
    assert pgs6.performance_auc is None
    assert pgs6.performance_or_per_sd is None


def test_join_metadata_multi_cohort_counter_and_max_reduction() -> None:
    """PGS000007 has 3 cohorts → counter incremented, max applied per column."""
    scores, publications, traits, performance, trait_categories, stats = _load_fixture_into_dicts()
    rows = _join_metadata(
        scores,
        publications,
        traits,
        performance,
        trait_categories,
        stats,  # type: ignore[arg-type]
    )
    expected_multi = 2
    assert stats.multi_cohort_performance == expected_multi  # PGS000007 + PGS000009
    pgs7 = next(r for r in rows if r.pgs_id == "PGS000007")
    assert pgs7.performance_auc == pytest.approx(0.75)  # max of 0.71, 0.75, 0.68
    assert pgs7.performance_or_per_sd == pytest.approx(2.10)  # max of 1.90, 2.10, 1.65


def test_join_metadata_max_of_non_null_when_split() -> None:
    """PGS000009 has one cohort with only AUC and one with only OR → max picks each."""
    scores, publications, traits, performance, trait_categories, stats = _load_fixture_into_dicts()
    rows = _join_metadata(
        scores,
        publications,
        traits,
        performance,
        trait_categories,
        stats,  # type: ignore[arg-type]
    )
    pgs9 = next(r for r in rows if r.pgs_id == "PGS000009")
    assert pgs9.performance_auc == pytest.approx(0.78)
    assert pgs9.performance_or_per_sd == pytest.approx(2.20)


def test_join_metadata_publication_year_propagates_to_row() -> None:
    """The publications join contributes ``publication_year`` to each row."""
    scores, publications, traits, performance, trait_categories, stats = _load_fixture_into_dicts()
    rows = _join_metadata(
        scores,
        publications,
        traits,
        performance,
        trait_categories,
        stats,  # type: ignore[arg-type]
    )
    pgs1 = next(r for r in rows if r.pgs_id == "PGS000001")
    expected_year_1 = 2015
    assert pgs1.publication_year == expected_year_1
    pgs10 = next(r for r in rows if r.pgs_id == "PGS000010")
    # PGS000010 shares PGP000001 with PGS000001 -- same publication metadata.
    assert pgs10.publication_year == expected_year_1
    assert pgs10.publication_pmid == "10000001"


def test_join_metadata_populates_trait_category_from_rest_dict() -> None:
    """Every score whose EFO is in the trait_category JSON gets a category."""
    scores, publications, traits, performance, trait_categories, stats = _load_fixture_into_dicts()
    rows = _join_metadata(
        scores,
        publications,
        traits,
        performance,
        trait_categories,
        stats,  # type: ignore[arg-type]
    )
    by_id = {r.pgs_id: r for r in rows}
    assert by_id["PGS000001"].trait_category == "Body measurement"  # EFO_0001065
    assert by_id["PGS000003"].trait_category == "Body measurement"  # first EFO of multi
    assert by_id["PGS000007"].trait_category == "Cardiovascular disease"
    assert by_id["PGS000010"].trait_category == "Other trait"  # MONDO_0004989
    # EFO_9999999 isn't in the trait_category dict → category stays NULL.
    assert by_id["PGS000005"].trait_category is None


def test_join_metadata_trait_category_independent_of_orphan_trait_counter() -> None:
    """Orphan trait refs and trait_category coverage are independent signals."""
    scores, publications, traits, performance, trait_categories, stats = _load_fixture_into_dicts()
    rows = _join_metadata(
        scores,
        publications,
        traits,
        performance,
        trait_categories,
        stats,  # type: ignore[arg-type]
    )
    # PGS000005 has EFO_9999999: missing from both the bundle's EFO traits
    # (orphan_trait_refs += 1) AND from the REST trait_category dict
    # (trait_category = None). The fact that both happen for the same row
    # is coincidence -- they are tracked independently and the loader
    # does NOT short-circuit one based on the other.
    pgs5 = next(r for r in rows if r.pgs_id == "PGS000005")
    assert pgs5.trait_category is None
    assert stats.orphan_trait_refs == 1


def test_join_metadata_rest_dict_orphans_dont_increment_orphan_trait_counter() -> None:
    """A trait_category entry whose EFO isn't in any score is a quiet no-op."""
    scores, publications, traits, performance, trait_categories, stats = _load_fixture_into_dicts()
    # EFO_0099999 is in the trait_categories fixture but no fixture score
    # references it. It must not affect any counter and must not produce
    # a row.
    assert "EFO_0099999" in trait_categories
    rows = _join_metadata(
        scores,
        publications,
        traits,
        performance,
        trait_categories,
        stats,  # type: ignore[arg-type]
    )
    assert all(r.pgs_id != "EFO_0099999" for r in rows)
    expected_orphans = 1
    assert stats.orphan_trait_refs == expected_orphans  # unchanged from PGS000005


# ---------------------------------------------------------------------------
# _reduce_performance unit (no DB / file).
# ---------------------------------------------------------------------------


def test_reduce_performance_empty_returns_none_pair() -> None:
    assert _reduce_performance([]) == (None, None)


def test_reduce_performance_all_none_returns_none_pair() -> None:
    """If every entry has None for a column, the reduction is None."""
    entries = [
        _RawPerformanceRow(auc=None, or_per_sd=None),
        _RawPerformanceRow(auc=None, or_per_sd=None),
    ]
    assert _reduce_performance(entries) == (None, None)


def test_reduce_performance_max_per_column() -> None:
    """Each column's max is taken independently from its non-NULL entries."""
    entries = [
        _RawPerformanceRow(auc=0.6, or_per_sd=1.2),
        _RawPerformanceRow(auc=0.7, or_per_sd=1.1),
        _RawPerformanceRow(auc=None, or_per_sd=1.5),
    ]
    auc, or_value = _reduce_performance(entries)
    assert auc == pytest.approx(0.7)
    assert or_value == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# _iter_chunks contract.
# ---------------------------------------------------------------------------


def _gen_dummy_rows(n: int) -> Iterator[_ParsedRow]:
    for i in range(n):
        yield _ParsedRow(
            pgs_id=f"PGS{i:06d}",
            pgs_name=None,
            trait_efo=None,
            trait_reported=None,
            trait_category=None,
            publication_pmid=None,
            publication_doi=None,
            publication_year=None,
            variants_total=None,
            reference_population=None,
            ancestry_distribution=None,
            performance_auc=None,
            performance_or_per_sd=None,
        )


def test_iter_chunks_exact_boundary_at_default_chunk_size() -> None:
    """2 full chunks + tail of 5 → three chunks with the expected sizes."""
    full_chunks = 2
    expected_total = full_chunks * _CHUNK_SIZE + 5
    chunks = list(_iter_chunks(_gen_dummy_rows(expected_total), _CHUNK_SIZE))
    chunk_sizes = [len(c) for c in chunks]
    expected_tail = 5
    assert chunk_sizes == [_CHUNK_SIZE] * full_chunks + [expected_tail]


def test_iter_chunks_handles_zero_rows() -> None:
    assert list(_iter_chunks(_gen_dummy_rows(0), _CHUNK_SIZE)) == []


def test_iter_chunks_single_partial_chunk() -> None:
    chunks = list(_iter_chunks(_gen_dummy_rows(7), _CHUNK_SIZE))
    assert [len(c) for c in chunks] == [7]


def test_stream_bulk_insert_emits_one_insert_chunk_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every chunk reaches ``_insert_chunk`` with the right base_id offset."""
    monkeypatch.setattr(pgs_loader, "_next_score_record_id", lambda _conn: 1)

    seen: list[tuple[int, int]] = []  # (base_id, rows)

    def _stub_insert_chunk(
        _conn: object,
        rows: list[_ParsedRow],
        *,
        base_id: int,
        source_version_id: int,  # noqa: ARG001
        retrieval_date: datetime,  # noqa: ARG001
    ) -> int:
        seen.append((base_id, len(rows)))
        return len(rows)

    monkeypatch.setattr(pgs_loader, "_insert_chunk", _stub_insert_chunk)
    full_chunks = 1
    expected_total = full_chunks * _CHUNK_SIZE + 3
    total = _stream_bulk_insert(
        conn=None,  # type: ignore[arg-type]
        rows_iter=_gen_dummy_rows(expected_total),
        source_version_id=1,
        retrieval_date=datetime(2026, 5, 17, tzinfo=UTC),
    )
    assert total == expected_total
    expected_tail = 3
    assert seen == [(1, _CHUNK_SIZE), (1 + _CHUNK_SIZE, expected_tail)]


# ---------------------------------------------------------------------------
# Integration: full transaction against the 10-score fixture.
# ---------------------------------------------------------------------------


_EXPECTED_INSERTED = 10


def test_refresh_full_transaction_inserts_expected_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fixture → 1 source_version row, 10 active rows, trait_category populated."""
    init_databases()
    bundle = _build_bundle(tmp_path)
    _patch_download_to_cache(monkeypatch, bundle)
    _patch_resolve_version(monkeypatch, "2026_05_07")

    result = pgs_loader.refresh(force=False)

    assert result.source_db == "pgs_catalog"
    assert result.version == "2026_05_07"
    assert result.record_count == _EXPECTED_INSERTED
    assert result.was_already_current is False

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM pgs_catalog_scores p "
            "JOIN annotation_sources s "
            "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id",
        ).fetchone()
        non_current = conn.execute(
            "SELECT COUNT(*) FROM pgs_catalog_scores p "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM annotation_sources s "
            "  WHERE s.source_db = 'pgs_catalog' "
            "    AND s.current_source_version_id = p.source_version_id"
            ")",
        ).fetchone()
        version_rows = conn.execute(
            "SELECT version, record_count FROM annotation_source_versions"
            " WHERE source_db = 'pgs_catalog'",
        ).fetchall()
        # End-to-end regression for the trait_category=0 finding: the
        # category dict from the REST endpoint must populate the schema
        # column on at least some rows post-load.
        distinct_category = conn.execute(
            "SELECT COUNT(DISTINCT p.trait_category) FROM pgs_catalog_scores p "
            "JOIN annotation_sources s "
            "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id "
            "WHERE p.trait_category IS NOT NULL",
        ).fetchone()
        with_category = conn.execute(
            "SELECT COUNT(*) FROM pgs_catalog_scores p "
            "JOIN annotation_sources s "
            "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id "
            "WHERE p.trait_category IS NOT NULL",
        ).fetchone()
    assert active is not None
    assert active[0] == _EXPECTED_INSERTED
    assert non_current is not None
    assert non_current[0] == 0
    assert version_rows == [("2026_05_07", _EXPECTED_INSERTED)]
    expected_distinct_categories = 4  # Body measurement, CVD, Other measurement, Other trait
    expected_rows_with_category = 9  # all 10 except PGS000005 (EFO_9999999)
    assert distinct_category is not None
    assert distinct_category[0] == expected_distinct_categories
    assert with_category is not None
    assert with_category[0] == expected_rows_with_category


def test_refresh_writes_expected_column_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Spot-check that PGS000007 (multi-cohort) lands the max-reduced metrics."""
    init_databases()
    bundle = _build_bundle(tmp_path)
    _patch_download_to_cache(monkeypatch, bundle)
    _patch_resolve_version(monkeypatch, "2026_05_07")

    pgs_loader.refresh(force=False)

    with duckdb_connection() as conn:
        row = conn.execute(
            """
            SELECT pgs_id, pgs_name, trait_efo, trait_reported,
                   publication_pmid, publication_doi, publication_year,
                   variants_total, performance_auc, performance_or_per_sd,
                   weights_storage
              FROM pgs_catalog_scores
             WHERE pgs_id = 'PGS000007'
            """,
        ).fetchone()
    assert row is not None
    (
        pgs_id,
        pgs_name,
        trait_efo,
        trait_reported,
        pmid,
        doi,
        year,
        variants_total,
        auc,
        or_value,
        weights_storage,
    ) = row
    assert pgs_id == "PGS000007"
    assert pgs_name == "PRS_MULTICOHORT"
    assert trait_efo == "EFO_0007777"
    assert trait_reported == "Coronary artery disease"
    assert pmid == "10000007"
    assert doi == "10.7/example"
    expected_year = 2023
    assert year == expected_year
    expected_variants = 5000
    assert variants_total == expected_variants
    assert auc == pytest.approx(0.75)
    assert or_value == pytest.approx(2.10)
    assert weights_storage == "overlapping_only"


# ---------------------------------------------------------------------------
# Integration: supersession.
# ---------------------------------------------------------------------------


def test_refresh_supersedes_prior_rows_same_version_force(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Same-version ``--force`` re-run → new id, new rows, pointer flips."""
    init_databases()
    bundle = _build_bundle(tmp_path)
    _patch_download_to_cache(monkeypatch, bundle)
    _patch_resolve_version(monkeypatch, "2026_05_07")

    first = pgs_loader.refresh(force=False)
    second = pgs_loader.refresh(force=True)

    assert first.was_already_current is False
    assert second.was_already_current is False
    assert second.source_version_id != first.source_version_id

    with duckdb_connection() as conn:
        current_pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources "
            "WHERE source_db = 'pgs_catalog'",
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) FROM pgs_catalog_scores p "
            "JOIN annotation_sources s "
            "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id",
        ).fetchone()
        prior = conn.execute(
            "SELECT COUNT(*) FROM pgs_catalog_scores WHERE source_version_id = ?",
            [first.source_version_id],
        ).fetchone()
        total = conn.execute(
            "SELECT COUNT(*) FROM pgs_catalog_scores",
        ).fetchone()
        version_rows = conn.execute(
            "SELECT source_version_id, version, record_count"
            " FROM annotation_source_versions"
            " WHERE source_db = 'pgs_catalog' ORDER BY source_version_id",
        ).fetchall()
    assert current_pointer is not None
    assert int(current_pointer[0]) == second.source_version_id
    assert active is not None
    assert active[0] == _EXPECTED_INSERTED
    assert prior is not None
    assert prior[0] == _EXPECTED_INSERTED
    assert total is not None
    assert total[0] == 2 * _EXPECTED_INSERTED
    assert [(int(r[0]), r[1], int(r[2])) for r in version_rows] == [
        (first.source_version_id, "2026_05_07", _EXPECTED_INSERTED),
        (second.source_version_id, "2026_05_07", _EXPECTED_INSERTED),
    ]


def test_refresh_supersedes_prior_rows_on_new_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two refreshes under different version labels: prior version flipped to inactive."""
    init_databases()
    bundle = _build_bundle(tmp_path)
    _patch_download_to_cache(monkeypatch, bundle)

    _patch_resolve_version(monkeypatch, "2026_04_13")
    first = pgs_loader.refresh(force=False)
    _patch_resolve_version(monkeypatch, "2026_05_07")
    second = pgs_loader.refresh(force=False)

    assert first.version == "2026_04_13"
    assert second.version == "2026_05_07"
    assert second.source_version_id > first.source_version_id

    with duckdb_connection() as conn:
        current_pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources "
            "WHERE source_db = 'pgs_catalog'",
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) FROM pgs_catalog_scores WHERE source_version_id = ?",
            [second.source_version_id],
        ).fetchone()
        prior_rows = conn.execute(
            "SELECT COUNT(*) FROM pgs_catalog_scores WHERE source_version_id = ?",
            [first.source_version_id],
        ).fetchone()
        version_rows = conn.execute(
            "SELECT version FROM annotation_source_versions"
            " WHERE source_db = 'pgs_catalog' ORDER BY source_version_id",
        ).fetchall()
    assert current_pointer is not None
    assert int(current_pointer[0]) == second.source_version_id
    assert active is not None
    assert active[0] == _EXPECTED_INSERTED
    assert prior_rows is not None
    assert prior_rows[0] == _EXPECTED_INSERTED
    assert version_rows == [
        ("2026_04_13",),
        ("2026_05_07",),
    ]


def test_refresh_idempotent_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Same version on second call (no force) → was_already_current=True."""
    init_databases()
    bundle = _build_bundle(tmp_path)
    _patch_download_to_cache(monkeypatch, bundle)
    _patch_resolve_version(monkeypatch, "2026_05_07")

    first = pgs_loader.refresh(force=False)
    second = pgs_loader.refresh(force=False)

    assert first.was_already_current is False
    assert second.was_already_current is True
    assert second.source_version_id == first.source_version_id
    assert second.record_count == _EXPECTED_INSERTED

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM pgs_catalog_scores p "
            "JOIN annotation_sources s "
            "ON s.source_db = 'pgs_catalog' AND s.current_source_version_id = p.source_version_id",
        ).fetchone()
        n_versions = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'pgs_catalog'",
        ).fetchone()
    assert active is not None
    assert active[0] == _EXPECTED_INSERTED
    assert n_versions is not None
    assert n_versions[0] == 1


def test_refresh_transaction_rolls_back_on_bulk_insert_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A streaming-insert failure rolls back every chunk + the deactivation
    + cleans up the orphan annotation_source_versions row."""
    init_databases()
    bundle = _build_bundle(tmp_path)
    _patch_download_to_cache(monkeypatch, bundle)
    _patch_resolve_version(monkeypatch, "2026_05_07")

    boom = RuntimeError("simulated insert failure")

    def _explode(*_args: object, **_kwargs: object) -> int:
        raise boom

    monkeypatch.setattr(pgs_loader, "_stream_bulk_insert", _explode)

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        pgs_loader.refresh(force=False)

    with duckdb_connection() as conn:
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'pgs_catalog'",
        ).fetchone()
        annotation_rows = conn.execute(
            "SELECT COUNT(*) FROM pgs_catalog_scores",
        ).fetchone()
    assert version_rows is not None
    assert version_rows[0] == 0
    assert annotation_rows is not None
    assert annotation_rows[0] == 0


# ---------------------------------------------------------------------------
# Integration: external-calls-disabled refusal.
# ---------------------------------------------------------------------------


def test_refresh_blocked_when_external_calls_disabled() -> None:
    """A disabled master switch raises before any bundle bytes are touched.

    The loader's first audited call is the GET against the
    release-current endpoint. With external_calls_enabled=false
    (the ``init_databases`` seed default), the disabled check raises
    :class:`ExternalCallsDisabledError` after writing one intent +
    blocked audit pair.
    """
    init_databases()
    # init_databases seeds external_calls_enabled=false; do not flip.

    with pytest.raises(ExternalCallsDisabledError, match="genome config set"):
        pgs_loader.refresh(force=False)

    rows = _audit_rows()
    expected_pair = 2
    assert len(rows) == expected_pair, rows
    intent, blocked = rows
    intent_details = json.loads(str(intent[3]))
    blocked_details = json.loads(str(blocked[3]))
    assert intent_details["phase"] == "intent"
    assert intent_details["method"] == "GET"
    assert blocked_details["status"] == "blocked"
    assert blocked_details["method"] == "GET"
    assert intent[1] == blocked[1] == "annotation_source"
    assert intent[2] == blocked[2] == "pgs_catalog_release_current"
    assert intent[5] == blocked[5] == "annotations_pgs_catalog"


# ---------------------------------------------------------------------------
# Registry / module-import side effects.
# ---------------------------------------------------------------------------


def test_get_loader_returns_pgs_refresh() -> None:
    assert get_loader("pgs_catalog") is pgs_loader.refresh


def test_source_db_label() -> None:
    assert pgs_loader.SOURCE_DB == "pgs_catalog"


def test_chunk_size_locked_at_250k() -> None:
    """Runbook documents 250K; pin it so a casual flip is loud."""
    expected_chunk_size = 250_000
    assert expected_chunk_size == _CHUNK_SIZE


def test_url_verified_date_is_iso_format() -> None:
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", pgs_loader.URL_VERIFIED_DATE)


def test_pgs_release_latest_url_matches_canonical() -> None:
    assert pgs_loader.PGS_RELEASE_LATEST_URL == ("https://www.pgscatalog.org/rest/release/current/")


def test_pgs_metadata_bundle_url_matches_canonical_ftp() -> None:
    assert pgs_loader.PGS_METADATA_BUNDLE_URL == (
        "https://ftp.ebi.ac.uk/pub/databases/spot/pgs/metadata/pgs_all_metadata.tar.gz"
    )


def test_pgs_trait_category_url_matches_canonical_rest() -> None:
    assert pgs_loader.PGS_TRAIT_CATEGORY_URL == (
        "https://www.pgscatalog.org/rest/trait_category/all"
    )


# ---------------------------------------------------------------------------
# CLI integration.
# ---------------------------------------------------------------------------


def test_cli_refresh_pgs_catalog_runs_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    init_databases()
    bundle = _build_bundle(tmp_path)
    _patch_download_to_cache(monkeypatch, bundle)
    _patch_resolve_version(monkeypatch, "2026_05_07")

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "refresh", "--source", "pgs_catalog"])
    assert result.exit_code == 0, result.output
    assert "source_db=pgs_catalog" in result.output
    assert "version=2026_05_07" in result.output
    assert f"records={_EXPECTED_INSERTED}" in result.output
    assert "already_current=False" in result.output


def test_cli_status_after_refresh_reports_pgs_catalog_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    init_databases()
    bundle = _build_bundle(tmp_path)
    _patch_download_to_cache(monkeypatch, bundle)
    _patch_resolve_version(monkeypatch, "2026_05_07")
    pgs_loader.refresh(force=False)

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "status"])
    assert result.exit_code == 0, result.output
    matching = [line for line in result.output.splitlines() if line.startswith("pgs_catalog:")]
    assert len(matching) == 1
    line = matching[0]
    assert "2026_05_07" in line
    assert f"{_EXPECTED_INSERTED} records" in line


# ---------------------------------------------------------------------------
# _insert_chunk smoke (real DB; verifies NULL columns + default weights_storage).
# ---------------------------------------------------------------------------


def test_insert_chunk_handles_null_columns_and_default_weights_storage() -> None:
    """Direct call exercises the schema's default weights_storage value."""
    init_databases()
    rows = [
        _ParsedRow(
            pgs_id="PGS999000",
            pgs_name=None,
            trait_efo=None,
            trait_reported=None,
            trait_category=None,
            publication_pmid=None,
            publication_doi=None,
            publication_year=None,
            variants_total=None,
            reference_population=None,
            ancestry_distribution=None,
            performance_auc=None,
            performance_or_per_sd=None,
        ),
    ]
    with duckdb_connection() as conn:
        from genome.annotate.source_versions import (  # noqa: PLC0415
            insert_source_version,
        )

        sv_id = insert_source_version(
            conn,
            source_db="pgs_catalog",
            version="2026_05_07",
            source_url="https://example.invalid/x",
            source_file_hash="abc",
            source_file_size=1,
            record_count=None,
        )
        n = _insert_chunk(
            conn,
            rows,
            base_id=1,
            source_version_id=sv_id,
            retrieval_date=datetime(2026, 5, 17, tzinfo=UTC),
        )
    assert n == 1
    with duckdb_connection() as conn:
        row = conn.execute(
            "SELECT score_record_id, pgs_id, weights_storage"
            " FROM pgs_catalog_scores WHERE pgs_id = 'PGS999000'",
        ).fetchone()
    assert row is not None
    score_record_id, pgs_id, weights_storage = row
    assert score_record_id == 1
    assert pgs_id == "PGS999000"
    assert weights_storage == "overlapping_only"


# ---------------------------------------------------------------------------
# Benchmark guard: 5K-row synthetic bundle under 30 s.
# ---------------------------------------------------------------------------


_BENCHMARK_ROW_COUNT: int = 5_000
_BENCHMARK_CEILING_S: float = 30.0


def _build_synthetic_bundle(tmp_path: Path, n: int) -> Path:
    """Construct an N-row synthetic PGS Catalog bundle in tmp_path.

    Mirrors the fixture shape (same column sets) so the parser runs
    through the same code paths. All rows are happy-path single-cohort
    so the join produces N rows with both performance metrics filled.
    """
    scores_dir = tmp_path / "synthetic_csvs"
    scores_dir.mkdir(parents=True, exist_ok=True)

    scores_path = scores_dir / _SCORES_MEMBER
    pubs_path = scores_dir / _PUBLICATIONS_MEMBER
    efo_path = scores_dir / _EFO_TRAITS_MEMBER
    perf_path = scores_dir / _PERFORMANCE_MEMBER

    # Scores: N PGS rows, each referencing PGP000001 and EFO_0001065.
    scores_header = (
        "Polygenic Score (PGS) ID,PGS Name,Reported Trait,"
        "Mapped Trait(s) (EFO label),Mapped Trait(s) (EFO ID),"
        "PGS Development Method,PGS Development Details/Relevant Parameters,"
        "Original Genome Build,Number of Variants,"
        "Number of Interaction Terms,Type of Variant Weight,"
        "PGS Publication (PGP) ID,Publication (PMID),Publication (doi),"
        "Score and results match the original publication,"
        "Ancestry Distribution (%) - Source of Variant Associations (GWAS),"
        "Ancestry Distribution (%) - Score Development/Training,"
        "Ancestry Distribution (%) - PGS Evaluation,"
        "FTP link,Release Date,License/Terms of Use"
    )
    scores_lines = [scores_header]
    for i in range(n):
        pgs_id = f"PGS{i + 1:06d}"
        scores_lines.append(
            f"{pgs_id},Synth_{i},Body mass index,body mass index,EFO_0001065,"
            "Synth,beta,GRCh38,77,0,NR,PGP000001,10000001,10.1/example,True,"
            "European:100,European:100,European:100,"
            f"https://example.invalid/{pgs_id},2026-05-07,Example",
        )
    scores_path.write_text("\n".join(scores_lines) + "\n", encoding="utf-8")

    # Publications: one shared row.
    pubs_path.write_text(
        "PGS Publication/Study (PGP) ID,First Author,Title,Journal Name,"
        "Publication Date,Release Date,Authors,digital object identifier (doi),"
        "PubMed ID (PMID)\n"
        "PGP000001,Smith J,Synthetic publication,Test Journal,2024-01-01,"
        "2024-01-01,Smith J,10.1/example,10000001\n",
        encoding="utf-8",
    )

    efo_path.write_text(
        "Ontology Trait ID,Ontology Trait Label,Ontology Trait Description,Ontology URL\n"
        "EFO_0001065,body mass index,desc,http://www.ebi.ac.uk/efo/EFO_0001065\n",
        encoding="utf-8",
    )

    perf_header = (
        "PGS Performance Metric (PPM) ID,Evaluated Score,PGS Sample Set (PSS),"
        "PGS Publication (PGP) ID,Reported Trait,Covariates Included in the Model,"
        "PGS Performance: Other Relevant Information,Publication (PMID),"
        "Publication (doi),Hazard Ratio (HR),Odds Ratio (OR),Beta,"
        "Area Under the Receiver-Operating Characteristic Curve (AUROC),"
        "Concordance Statistic (C-index),Other Metric(s)"
    )
    perf_lines = [perf_header]
    for i in range(n):
        ppm_id = f"PPM{i + 1:06d}"
        pgs_id = f"PGS{i + 1:06d}"
        perf_lines.append(
            f"{ppm_id},{pgs_id},PSS000001,PGP000001,Body mass index,,,"
            f'10000001,10.1/example,,"1.55 [1.52,1.58]",,"0.622 [0.619,0.627]",,',
        )
    perf_path.write_text("\n".join(perf_lines) + "\n", encoding="utf-8")

    bundle = tmp_path / "synthetic_pgs_all_metadata.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        for member in (
            _SCORES_MEMBER,
            _PUBLICATIONS_MEMBER,
            _EFO_TRAITS_MEMBER,
            _PERFORMANCE_MEMBER,
        ):
            tf.add(scores_dir / member, arcname=f"/{member}")
    return bundle


def test_benchmark_parse_and_load_5k_rows_under_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5K-score synthetic bundle parses and loads in under 30 s.

    The locked < 30 s ceiling is the project-wide "routine refresh"
    target documented in ``CLAUDE.md``. PGS Catalog ships ~5-7K
    scores at the current release, so the synthetic count matches
    the real-data scale exactly.
    """
    init_databases()
    bundle = _build_synthetic_bundle(tmp_path, _BENCHMARK_ROW_COUNT)
    _patch_download_to_cache(monkeypatch, bundle)
    _patch_resolve_version(monkeypatch, "2026_05_07")

    started = time.monotonic()
    result = pgs_loader.refresh(force=False)
    elapsed = time.monotonic() - started

    assert result.record_count == _BENCHMARK_ROW_COUNT
    assert elapsed < _BENCHMARK_CEILING_S, (
        f"5K-row parse+load took {elapsed:.2f}s "
        f"(ceiling {_BENCHMARK_CEILING_S}s); investigate before merging"
    )

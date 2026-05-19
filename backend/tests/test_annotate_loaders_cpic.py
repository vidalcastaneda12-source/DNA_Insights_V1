"""Tests for :mod:`genome.annotate.loaders.cpic`.

Covers the join helpers, the version-resolution path (both with the
``/change_log`` canary present and falling back to retrieval date), and
the end-to-end ``refresh`` flow with ``download_to_cache`` monkey-patched
to return programmatically-built endpoint JSON files. The fixture set
exercises single-gene, multi-gene split, missing-pair, missing-drug,
pediatric vs general population, empty citations, and an empty
lookupkey -- i.e. every quirk the real CPIC API throws at the loader.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from genome.annotate import downloads as annotate_downloads
from genome.annotate.loaders import cpic as cpic_loader
from genome.annotate.loaders.cpic import (
    _build_rows,
    _first_pmid,
    _parse_lookupkey,
    _pediatric_flag,
    _resolve_version,
)
from genome.annotate.registry import get_loader
from genome.annotate.source_versions import get_current_version
from genome.cli import app
from genome.db import duckdb_connection, init_databases

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixture payloads.
#
# Eight recommendations covering the eight join-shape combinations the
# parser has to handle:
#
#   8093158: HLA-B / abacavir, single gene, classification Strong, general
#            population (pediatric=None), one citation (PMID populates).
#   8093159: HLA-B / abacavir, single gene, classification Strong, general
#            population, NO citations (PMID stays None).
#   8093160: HLA-A / allopurinol, single gene, classification Optional,
#            pediatric population (pediatric=True).
#   8093161: CYP2C9 + VKORC1 / warfarin, MULTI-GENE split (yields 2 rows),
#            classification Strong, adults population (pediatric=None
#            because we map adults to None, not False).
#   8093162: CYP2D6 / tramadol, single gene, classification=None
#            (recommendation has no classification), cpic_level=None
#            (pair has no cpic_level either).
#   8093163: CYP2C19 / clopidogrel, single gene, three citations -- first
#            wins for publication_pmid.
#   8093164: <unknown drugid>, recommendation references a drug NOT in
#            the /drug payload; loader must drop the row.
#   8093165: Empty lookupkey {} -- skipped, debug-logged.
#
# The expected emitted row count after multi-gene split and skip-logic
# is 7: 8093158 + 8093159 + 8093160 + 8093161 (x2) + 8093162 + 8093163.
# Recommendations 8093164 (unknown drug) and 8093165 (empty lookupkey)
# are dropped before insert.
# ---------------------------------------------------------------------------

_FIXTURE_GUIDELINES: list[dict[str, object]] = [
    {
        "id": 100421,
        "name": "HLA-B and Abacavir",
        "url": "https://www.clinpgx.org/guideline/PA166251444",
        "clinpgxid": "PA166251444",
    },
    {
        "id": 100422,
        "name": "HLA-A and Allopurinol",
        "url": "https://www.clinpgx.org/guideline/PA166104996",
        "clinpgxid": "PA166104996",
    },
    {
        "id": 100423,
        "name": "CYP2C9, VKORC1, CYP4F2, CYP2C cluster variants and warfarin",
        "url": "https://www.clinpgx.org/guideline/PA166104949",
        "clinpgxid": "PA166104949",
    },
    {
        "id": 100424,
        "name": "CYP2D6, OPRM1, COMT and tramadol",
        "url": "https://www.clinpgx.org/guideline/PA166228194",
        "clinpgxid": "PA166228194",
    },
    {
        "id": 100425,
        "name": "CYP2C19 and Clopidogrel",
        "url": "https://www.clinpgx.org/guideline/PA166104948",
        "clinpgxid": "PA166104948",
    },
]

_FIXTURE_PAIRS: list[dict[str, object]] = [
    {
        "pairid": 110001,
        "genesymbol": "HLA-B",
        "drugid": "RxNorm:190521",
        "guidelineid": 100421,
        "cpiclevel": "A",
        "citations": ["32189324"],
    },
    {
        "pairid": 110002,
        "genesymbol": "HLA-A",
        "drugid": "RxNorm:519",
        "guidelineid": 100422,
        "cpiclevel": "A",
        "citations": ["29588531"],
    },
    {
        "pairid": 110003,
        "genesymbol": "CYP2C9",
        "drugid": "RxNorm:11289",
        "guidelineid": 100423,
        "cpiclevel": "A",
        "citations": ["28198005"],
    },
    {
        "pairid": 110004,
        "genesymbol": "VKORC1",
        "drugid": "RxNorm:11289",
        "guidelineid": 100423,
        "cpiclevel": "A",
        "citations": ["28198005"],
    },
    {
        "pairid": 110005,
        "genesymbol": "CYP2D6",
        "drugid": "RxNorm:10689",
        "guidelineid": 100424,
        # cpic_level intentionally absent -- this row exercises the
        # "pair has no cpic_level" survival path.
        "cpiclevel": None,
        # citations intentionally empty for the same reason.
        "citations": [],
    },
    {
        "pairid": 110006,
        "genesymbol": "CYP2C19",
        "drugid": "RxNorm:32968",
        "guidelineid": 100425,
        "cpiclevel": "A",
        # Three citations -- the first one is the canonical guideline
        # publication; loader should pick it.
        "citations": ["21716271", "23698643", "25974703"],
    },
]

_FIXTURE_DRUGS: list[dict[str, object]] = [
    {
        "drugid": "RxNorm:190521",
        "name": "abacavir",
        "rxnormid": "190521",
    },
    {
        "drugid": "RxNorm:519",
        "name": "allopurinol",
        "rxnormid": "519",
    },
    {
        "drugid": "RxNorm:11289",
        "name": "warfarin",
        "rxnormid": "11289",
    },
    {
        "drugid": "RxNorm:10689",
        "name": "tramadol",
        # rxnormid intentionally null -- exercises the NULL
        # drug_rxnorm_id survival path.
        "rxnormid": None,
    },
    {
        "drugid": "RxNorm:32968",
        "name": "clopidogrel",
        "rxnormid": "32968",
    },
]

_FIXTURE_RECOMMENDATIONS: list[dict[str, object]] = [
    {
        "id": 8093158,
        "guidelineid": 100421,
        "drugid": "RxNorm:190521",
        "drugrecommendation": "Use abacavir per standard dosing guidelines",
        "classification": "Strong",
        "lookupkey": {"HLA-B": "*57:01 negative"},
        "population": "general",
    },
    {
        "id": 8093159,
        "guidelineid": 100421,
        "drugid": "RxNorm:190521",
        "drugrecommendation": "Abacavir is not recommended",
        "classification": "Strong",
        "lookupkey": {"HLA-B": "*57:01 positive"},
        "population": "general",
    },
    {
        "id": 8093160,
        "guidelineid": 100422,
        "drugid": "RxNorm:519",
        "drugrecommendation": "Consider alternative therapy",
        "classification": "Optional",
        "lookupkey": {"HLA-A": "*31:01 positive"},
        "population": "pediatrics",
    },
    {
        "id": 8093161,
        "guidelineid": 100423,
        "drugid": "RxNorm:11289",
        "drugrecommendation": "Initiate warfarin per CPIC dosing algorithm",
        "classification": "Strong",
        # Multi-gene lookupkey: splits into one row per gene.
        "lookupkey": {
            "CYP2C9": "Poor Metabolizer",
            "VKORC1": "rs9923231 variant",
        },
        "population": "adults",
    },
    {
        "id": 8093162,
        "guidelineid": 100424,
        "drugid": "RxNorm:10689",
        "drugrecommendation": "Avoid tramadol; consider alternative analgesic",
        # classification intentionally None -- exercises survival.
        "classification": None,
        "lookupkey": {"CYP2D6": "Poor Metabolizer"},
        "population": "general",
    },
    {
        "id": 8093163,
        "guidelineid": 100425,
        "drugid": "RxNorm:32968",
        "drugrecommendation": "Avoid clopidogrel if possible",
        "classification": "Strong",
        "lookupkey": {"CYP2C19": "Poor Metabolizer"},
        "population": "general",
    },
    {
        "id": 8093164,
        "guidelineid": 100425,
        # Drugid not present in _FIXTURE_DRUGS -- skipped.
        "drugid": "RxNorm:UNKNOWN",
        "drugrecommendation": "Should not appear",
        "classification": "Strong",
        "lookupkey": {"CYP2C19": "Intermediate Metabolizer"},
        "population": "general",
    },
    {
        "id": 8093165,
        "guidelineid": 100421,
        "drugid": "RxNorm:190521",
        "drugrecommendation": "Should not appear",
        "classification": "Strong",
        # Empty lookupkey -- skipped, debug-logged.
        "lookupkey": {},
        "population": "general",
    },
]

# After multi-gene expansion and skip-logic: 7 DB rows.
#   8093158, 8093159, 8093160, 8093161 -> 2 (CYP2C9 + VKORC1),
#   8093162, 8093163, (8093164 skipped), (8093165 skipped) -> 1 + 1.
_EXPECTED_DB_ROW_COUNT = 7

# Distribution of cpic_level across the emitted rows.
_EXPECTED_CPIC_LEVEL_DISTRIBUTION = {
    "A": 6,  # 8093158/9 + 8093160 + 8093161 (x2) + 8093163
    None: 1,  # 8093162 (pair has no cpic_level)
}


def _write_endpoint_json(path: Path, payload: list[dict[str, object]]) -> None:
    """Serialize an endpoint payload to ``path`` in the on-wire shape.

    CPIC's PostgREST returns a top-level JSON array; we match that
    exactly so the loader's parser walks the same code path as a real
    download.
    """
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_change_log_json(path: Path, date_str: str | None) -> None:
    """Write the version canary file with the given date (or empty list)."""
    body: list[dict[str, str]] = [{"date": date_str}] if date_str is not None else []
    path.write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture(autouse=True)
def _ensure_cpic_registered() -> Iterator[None]:
    """Re-register the loader at test start.

    Other annotate test files install autouse fixtures that wipe the
    registry via ``_clear_loaders_for_testing()`` to keep their cases
    hermetic. When their tests run before ours in a full-suite
    invocation, the side-effect registration from
    ``genome.annotate.loaders.cpic`` is gone. Re-registering here makes
    our tests order-independent. We pop again on teardown so we don't
    leak the registration into the next test file.
    """
    from genome.annotate.registry import _LOADERS, register_loader  # noqa: PLC0415

    _LOADERS.pop("cpic", None)
    register_loader("cpic", cpic_loader.refresh)
    try:
        yield
    finally:
        _LOADERS.pop("cpic", None)


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


@pytest.fixture
def fixture_endpoint_files(tmp_path: Path) -> dict[str, Path]:
    """Write all four endpoint JSON files + the change_log canary to tmp.

    Returns a mapping ``endpoint_name -> Path`` keyed by the same names
    the loader uses (``guideline``, ``pair``, ``recommendation``,
    ``drug``, ``change_log_latest``). Tests patch
    ``download_to_cache`` to dispatch to these paths.
    """
    endpoints_dir = tmp_path / "endpoint-fixtures"
    endpoints_dir.mkdir()
    paths = {
        "guideline": endpoints_dir / "guideline.json",
        "pair": endpoints_dir / "pair.json",
        "recommendation": endpoints_dir / "recommendation.json",
        "drug": endpoints_dir / "drug.json",
        "change_log_latest": endpoints_dir / "change_log_latest.json",
    }
    _write_endpoint_json(paths["guideline"], _FIXTURE_GUIDELINES)
    _write_endpoint_json(paths["pair"], _FIXTURE_PAIRS)
    _write_endpoint_json(paths["recommendation"], _FIXTURE_RECOMMENDATIONS)
    _write_endpoint_json(paths["drug"], _FIXTURE_DRUGS)
    _write_change_log_json(paths["change_log_latest"], "2026-05-14")
    return paths


def _patch_download_to_cache(
    monkeypatch: pytest.MonkeyPatch,
    fixture_paths: dict[str, Path],
) -> dict[str, int]:
    """Replace ``download_to_cache`` with a stub that dispatches by filename.

    The loader names each download via its ``filename`` argument
    (``guideline.json``, ``pair.json``, ..., ``change_log_latest.json``).
    The stub looks up the corresponding fixture path and returns a
    DownloadResult populated with the file's real size and SHA-256 so
    the loader's ``annotation_source_versions`` row carries realistic
    provenance.

    The returned counter records how many times each endpoint was
    requested, supporting "download was attempted N times" assertions.
    """
    import hashlib  # noqa: PLC0415

    counter: dict[str, int] = dict.fromkeys(fixture_paths, 0)

    def _stub(
        source_db: str,  # noqa: ARG001
        url: str,  # noqa: ARG001
        filename: str,
        *,
        resource_id: str,
        force: bool = False,  # noqa: ARG001
    ) -> annotate_downloads.DownloadResult:
        # The loader's _ENDPOINTS mapping pairs each filename with the
        # endpoint key passed as resource_id; we dispatch on resource_id
        # to keep the stub agnostic to filename collisions.
        if resource_id not in fixture_paths:
            msg = f"unexpected resource_id {resource_id!r} requested for {filename!r}"
            raise KeyError(msg)
        counter[resource_id] += 1
        path = fixture_paths[resource_id]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        size = path.stat().st_size
        return annotate_downloads.DownloadResult(
            path=path,
            sha256=digest,
            size_bytes=size,
        )

    monkeypatch.setattr(cpic_loader, "download_to_cache", _stub)
    return counter


# ---------------------------------------------------------------------------
# _parse_lookupkey
# ---------------------------------------------------------------------------


def test_parse_lookupkey_single_gene() -> None:
    assert _parse_lookupkey({"CYP2C19": "Poor Metabolizer"}) == [
        ("CYP2C19", "Poor Metabolizer"),
    ]


def test_parse_lookupkey_multi_gene_preserves_insertion_order() -> None:
    """Multi-gene split must be stable across runs.

    Python 3.7+ dict iteration order is insertion order, and the loader
    depends on that for deterministic row sequencing. Pin it here so a
    future refactor that introduces hashing or set-based iteration is
    caught.
    """
    out = _parse_lookupkey(
        {"CYP2C9": "PM", "VKORC1": "rs9923231 variant"},
    )
    assert out == [
        ("CYP2C9", "PM"),
        ("VKORC1", "rs9923231 variant"),
    ]


def test_parse_lookupkey_empty_dict_returns_empty_list() -> None:
    assert _parse_lookupkey({}) == []


def test_parse_lookupkey_none_returns_empty_list() -> None:
    assert _parse_lookupkey(None) == []


def test_parse_lookupkey_non_dict_returns_empty_list() -> None:
    """Strings, lists, integers all return ``[]`` defensively."""
    assert _parse_lookupkey("CYP2C9:PM") == []
    assert _parse_lookupkey(["CYP2C9", "PM"]) == []
    assert _parse_lookupkey(42) == []


def test_parse_lookupkey_drops_empty_phenotype_value() -> None:
    out = _parse_lookupkey({"CYP2C9": "PM", "VKORC1": ""})
    assert out == [("CYP2C9", "PM")]


def test_parse_lookupkey_drops_non_string_phenotype_value() -> None:
    out = _parse_lookupkey({"CYP2C9": "PM", "VKORC1": None})
    assert out == [("CYP2C9", "PM")]


def test_parse_lookupkey_drops_empty_gene_key() -> None:
    out = _parse_lookupkey({"": "PM", "CYP2C9": "PM"})
    assert out == [("CYP2C9", "PM")]


# ---------------------------------------------------------------------------
# _first_pmid
# ---------------------------------------------------------------------------


def test_first_pmid_returns_first_entry() -> None:
    assert _first_pmid(["21716271", "23698643", "25974703"]) == "21716271"


def test_first_pmid_empty_list_returns_none() -> None:
    assert _first_pmid([]) is None


def test_first_pmid_non_list_returns_none() -> None:
    assert _first_pmid(None) is None
    assert _first_pmid("21716271") is None


def test_first_pmid_non_string_element_returns_none() -> None:
    assert _first_pmid([21716271]) is None


# ---------------------------------------------------------------------------
# _pediatric_flag
# ---------------------------------------------------------------------------


def test_pediatric_flag_pediatrics_returns_true() -> None:
    assert _pediatric_flag("pediatrics") is True


def test_pediatric_flag_adults_returns_none() -> None:
    """Adults maps to None, not False, so ``pediatric IS TRUE`` semantics hold."""
    assert _pediatric_flag("adults") is None


def test_pediatric_flag_general_returns_none() -> None:
    assert _pediatric_flag("general") is None


def test_pediatric_flag_condition_string_returns_none() -> None:
    """Condition-based population labels are not age cohorts."""
    assert _pediatric_flag("PHT naive") is None
    assert _pediatric_flag("CVI ACS PCI") is None


def test_pediatric_flag_none_returns_none() -> None:
    assert _pediatric_flag(None) is None


# ---------------------------------------------------------------------------
# _build_rows
# ---------------------------------------------------------------------------


def test_build_rows_emits_expected_total_count() -> None:
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    assert len(rows) == _EXPECTED_DB_ROW_COUNT


def test_build_rows_multi_gene_split_emits_two_rows_sharing_cpic_id() -> None:
    """One recommendation -> two rows when ``lookupkey`` has two genes."""
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    warfarin = [r for r in rows if r.cpic_id == "8093161"]
    assert len(warfarin) == 2
    genes = {r.gene_symbol for r in warfarin}
    assert genes == {"CYP2C9", "VKORC1"}
    phenotypes = {(r.gene_symbol, r.phenotype) for r in warfarin}
    assert phenotypes == {
        ("CYP2C9", "Poor Metabolizer"),
        ("VKORC1", "rs9923231 variant"),
    }
    # Cross-row invariants: the cpic_id, drug_name, classification, and
    # guideline_url are identical across the split rows.
    assert {r.cpic_id for r in warfarin} == {"8093161"}
    assert {r.drug_name for r in warfarin} == {"warfarin"}
    assert {r.classification_strength for r in warfarin} == {"Strong"}
    assert {r.guideline_url for r in warfarin} == {
        "https://www.clinpgx.org/guideline/PA166104949",
    }


def test_build_rows_skips_recommendation_with_empty_lookupkey() -> None:
    """Recommendation 8093165 has ``lookupkey: {}`` and must not emit a row."""
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    assert all(r.cpic_id != "8093165" for r in rows)


def test_build_rows_skips_recommendation_with_unknown_drug() -> None:
    """Recommendation 8093164 references a drugid not in /drug; must skip."""
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    assert all(r.cpic_id != "8093164" for r in rows)


def test_build_rows_populates_cpic_level_from_pair() -> None:
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    distribution: dict[str | None, int] = {}
    for r in rows:
        distribution[r.cpic_level] = distribution.get(r.cpic_level, 0) + 1
    assert distribution == _EXPECTED_CPIC_LEVEL_DISTRIBUTION


def test_build_rows_publication_pmid_first_from_pair_citations() -> None:
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    clopidogrel = next(r for r in rows if r.cpic_id == "8093163")
    assert clopidogrel.publication_pmid == "21716271"


def test_build_rows_empty_citations_yield_none_pmid() -> None:
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    tramadol = next(r for r in rows if r.cpic_id == "8093162")
    assert tramadol.publication_pmid is None


def test_build_rows_pediatric_population_sets_flag_true() -> None:
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    allopurinol = next(r for r in rows if r.cpic_id == "8093160")
    assert allopurinol.pediatric is True


def test_build_rows_adults_population_leaves_flag_none() -> None:
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    warfarin = next(r for r in rows if r.cpic_id == "8093161")
    assert warfarin.pediatric is None


def test_build_rows_classification_none_survives() -> None:
    """A recommendation with classification=None lands as NULL, not skipped."""
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    tramadol = next(r for r in rows if r.cpic_id == "8093162")
    assert tramadol.classification_strength is None
    # And the row still has all the other fields populated.
    assert tramadol.gene_symbol == "CYP2D6"
    assert tramadol.drug_name == "tramadol"


def test_build_rows_pair_cpic_level_none_survives() -> None:
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    tramadol = next(r for r in rows if r.cpic_id == "8093162")
    assert tramadol.cpic_level is None


def test_build_rows_drug_rxnorm_id_populated_from_drug_endpoint() -> None:
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    abacavir = next(r for r in rows if r.cpic_id == "8093158")
    assert abacavir.drug_rxnorm_id == "190521"


def test_build_rows_drug_rxnorm_id_none_survives() -> None:
    """Drug with NULL rxnormid lands as NULL drug_rxnorm_id, not skipped."""
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    tramadol = next(r for r in rows if r.cpic_id == "8093162")
    assert tramadol.drug_rxnorm_id is None


def test_build_rows_guideline_url_resolved_from_guideline_endpoint() -> None:
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    abacavir = next(r for r in rows if r.cpic_id == "8093158")
    assert abacavir.guideline_url == "https://www.clinpgx.org/guideline/PA166251444"


def test_build_rows_last_updated_always_none() -> None:
    """The four data endpoints carry no per-row update dates."""
    rows = _build_rows(
        _FIXTURE_GUIDELINES,
        _FIXTURE_PAIRS,
        _FIXTURE_RECOMMENDATIONS,
        _FIXTURE_DRUGS,
    )
    assert all(r.last_updated is None for r in rows)


# ---------------------------------------------------------------------------
# _resolve_version
# ---------------------------------------------------------------------------


def test_resolve_version_reads_change_log_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A normal canary payload of ``[{"date":"2026-05-14"}]`` -> ``2026_05_14``."""
    canary_path = tmp_path / "canary.json"
    _write_change_log_json(canary_path, "2026-05-14")

    def _stub(
        source_db: str,  # noqa: ARG001
        url: str,  # noqa: ARG001
        filename: str,  # noqa: ARG001
        *,
        resource_id: str,  # noqa: ARG001
        force: bool = False,  # noqa: ARG001
    ) -> annotate_downloads.DownloadResult:
        return annotate_downloads.DownloadResult(
            path=canary_path,
            sha256="x" * 64,
            size_bytes=canary_path.stat().st_size,
        )

    monkeypatch.setattr(cpic_loader, "download_to_cache", _stub)
    assert _resolve_version(force=False) == "2026_05_14"


def test_resolve_version_falls_back_to_today_when_canary_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An empty list canary -> today's UTC date in YYYY_MM_DD form."""
    canary_path = tmp_path / "empty.json"
    canary_path.write_text("[]", encoding="utf-8")

    def _stub(
        source_db: str,  # noqa: ARG001
        url: str,  # noqa: ARG001
        filename: str,  # noqa: ARG001
        *,
        resource_id: str,  # noqa: ARG001
        force: bool = False,  # noqa: ARG001
    ) -> annotate_downloads.DownloadResult:
        return annotate_downloads.DownloadResult(
            path=canary_path,
            sha256="x" * 64,
            size_bytes=canary_path.stat().st_size,
        )

    monkeypatch.setattr(cpic_loader, "download_to_cache", _stub)
    version = _resolve_version(force=False)
    assert re.match(r"^\d{4}_\d{2}_\d{2}$", version)
    assert version == datetime.now(UTC).strftime("%Y_%m_%d")


def test_resolve_version_falls_back_when_canary_payload_malformed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Non-list canary payload (e.g. error object) -> retrieval date."""
    canary_path = tmp_path / "bad.json"
    canary_path.write_text('{"code":"PGRST100","message":"bad query"}', encoding="utf-8")

    def _stub(
        source_db: str,  # noqa: ARG001
        url: str,  # noqa: ARG001
        filename: str,  # noqa: ARG001
        *,
        resource_id: str,  # noqa: ARG001
        force: bool = False,  # noqa: ARG001
    ) -> annotate_downloads.DownloadResult:
        return annotate_downloads.DownloadResult(
            path=canary_path,
            sha256="x" * 64,
            size_bytes=canary_path.stat().st_size,
        )

    monkeypatch.setattr(cpic_loader, "download_to_cache", _stub)
    version = _resolve_version(force=False)
    assert version == datetime.now(UTC).strftime("%Y_%m_%d")


def test_resolve_version_falls_back_when_download_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A download exception falls back cleanly to retrieval date."""

    def _stub(
        source_db: str,  # noqa: ARG001
        url: str,  # noqa: ARG001
        filename: str,  # noqa: ARG001
        *,
        resource_id: str,  # noqa: ARG001
        force: bool = False,  # noqa: ARG001
    ) -> annotate_downloads.DownloadResult:
        msg = "simulated canary failure"
        raise OSError(msg)

    monkeypatch.setattr(cpic_loader, "download_to_cache", _stub)
    version = _resolve_version(force=False)
    assert version == datetime.now(UTC).strftime("%Y_%m_%d")


def test_resolve_version_slices_long_date_string(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A datetime-string upstream returns the date portion only."""
    canary_path = tmp_path / "long.json"
    canary_path.write_text(
        json.dumps([{"date": "2026-05-14T10:30:00Z"}]),
        encoding="utf-8",
    )

    def _stub(
        source_db: str,  # noqa: ARG001
        url: str,  # noqa: ARG001
        filename: str,  # noqa: ARG001
        *,
        resource_id: str,  # noqa: ARG001
        force: bool = False,  # noqa: ARG001
    ) -> annotate_downloads.DownloadResult:
        return annotate_downloads.DownloadResult(
            path=canary_path,
            sha256="x" * 64,
            size_bytes=canary_path.stat().st_size,
        )

    monkeypatch.setattr(cpic_loader, "download_to_cache", _stub)
    assert _resolve_version(force=False) == "2026_05_14"


# ---------------------------------------------------------------------------
# refresh() end-to-end
# ---------------------------------------------------------------------------


def test_refresh_inserts_rows_and_records_source_version(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_endpoint_files)

    result = cpic_loader.refresh(force=False)

    assert result.source_db == "cpic"
    assert result.version == "2026_05_14"
    assert result.record_count == _EXPECTED_DB_ROW_COUNT
    assert result.was_already_current is False

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM cpic_guidelines c "
            "JOIN annotation_sources s "
            "ON s.source_db = 'cpic' AND s.current_source_version_id = c.source_version_id",
        ).fetchone()
        non_current = conn.execute(
            "SELECT COUNT(*) FROM cpic_guidelines c "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM annotation_sources s "
            "  WHERE s.source_db = 'cpic' AND s.current_source_version_id = c.source_version_id"
            ")",
        ).fetchone()
        version_rows = conn.execute(
            "SELECT version, record_count FROM annotation_source_versions"
            " WHERE source_db = 'cpic'",
        ).fetchall()
    assert active is not None
    assert active[0] == _EXPECTED_DB_ROW_COUNT
    assert non_current is not None
    assert non_current[0] == 0
    assert version_rows == [("2026_05_14", _EXPECTED_DB_ROW_COUNT)]


def test_refresh_second_call_is_short_circuit(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    counter = _patch_download_to_cache(monkeypatch, fixture_endpoint_files)
    first = cpic_loader.refresh(force=False)
    second = cpic_loader.refresh(force=False)

    assert first.was_already_current is False
    assert second.was_already_current is True
    assert second.version == first.version
    assert second.source_version_id == first.source_version_id
    # Each refresh attempts a download per endpoint (skip-if-exists is
    # the cache layer's job). Five endpoints (4 data + canary), two
    # refreshes -> 10 stub calls total.
    expected_calls_per_endpoint = 2
    for endpoint in counter:
        assert counter[endpoint] == expected_calls_per_endpoint, (
            f"{endpoint} called {counter[endpoint]}x, expected {expected_calls_per_endpoint}"
        )

    with duckdb_connection() as conn:
        n_active = conn.execute(
            "SELECT COUNT(*) FROM cpic_guidelines c "
            "JOIN annotation_sources s "
            "ON s.source_db = 'cpic' AND s.current_source_version_id = c.source_version_id",
        ).fetchone()
        n_versions = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'cpic'",
        ).fetchone()
    assert n_active is not None
    assert n_active[0] == _EXPECTED_DB_ROW_COUNT
    assert n_versions is not None
    assert n_versions[0] == 1


def test_refresh_new_version_supersedes_prior_rows(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_endpoint_files)
    first = cpic_loader.refresh(force=False)

    # Build a "v2" endpoint set with a different change_log date and a
    # trimmed recommendation list (only the first three).
    v2_dir = tmp_path / "v2-endpoints"
    v2_dir.mkdir()
    v2_paths = {
        "guideline": v2_dir / "guideline.json",
        "pair": v2_dir / "pair.json",
        "recommendation": v2_dir / "recommendation.json",
        "drug": v2_dir / "drug.json",
        "change_log_latest": v2_dir / "change_log_latest.json",
    }
    _write_endpoint_json(v2_paths["guideline"], _FIXTURE_GUIDELINES)
    _write_endpoint_json(v2_paths["pair"], _FIXTURE_PAIRS)
    _write_endpoint_json(
        v2_paths["recommendation"],
        _FIXTURE_RECOMMENDATIONS[:3],  # 8093158, 8093159, 8093160 -> 3 rows
    )
    _write_endpoint_json(v2_paths["drug"], _FIXTURE_DRUGS)
    _write_change_log_json(v2_paths["change_log_latest"], "2026-06-01")
    _patch_download_to_cache(monkeypatch, v2_paths)
    second = cpic_loader.refresh(force=False)

    assert first.version == "2026_05_14"
    assert second.version == "2026_06_01"
    assert second.was_already_current is False
    assert second.source_version_id > first.source_version_id

    expected_v2_rows = 3
    with duckdb_connection() as conn:
        current_pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db = 'cpic'",
        ).fetchone()
        prior_rows = conn.execute(
            "SELECT COUNT(*) FROM cpic_guidelines WHERE source_version_id = ?",
            [first.source_version_id],
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) FROM cpic_guidelines WHERE source_version_id = ?",
            [second.source_version_id],
        ).fetchone()
        version_rows = conn.execute(
            "SELECT version FROM annotation_source_versions"
            " WHERE source_db = 'cpic' ORDER BY source_version_id",
        ).fetchall()
    assert current_pointer is not None
    assert int(current_pointer[0]) == second.source_version_id
    assert prior_rows is not None
    assert prior_rows[0] == _EXPECTED_DB_ROW_COUNT
    assert active is not None
    assert active[0] == expected_v2_rows
    assert version_rows == [("2026_05_14",), ("2026_06_01",)]


def test_refresh_force_reloads_even_when_version_matches(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force`` against the same upstream version → new id, new rows, pointer flips."""
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_endpoint_files)
    first = cpic_loader.refresh(force=False)
    second = cpic_loader.refresh(force=True)

    assert first.was_already_current is False
    assert second.was_already_current is False
    assert second.source_version_id != first.source_version_id

    with duckdb_connection() as conn:
        current_pointer = conn.execute(
            "SELECT current_source_version_id FROM annotation_sources WHERE source_db = 'cpic'",
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) FROM cpic_guidelines c "
            "JOIN annotation_sources s "
            "ON s.source_db = 'cpic' AND s.current_source_version_id = c.source_version_id",
        ).fetchone()
        prior = conn.execute(
            "SELECT COUNT(*) FROM cpic_guidelines WHERE source_version_id = ?",
            [first.source_version_id],
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM cpic_guidelines").fetchone()
        version_rows = conn.execute(
            "SELECT source_version_id, version, record_count"
            " FROM annotation_source_versions"
            " WHERE source_db = 'cpic' ORDER BY source_version_id",
        ).fetchall()
    assert current_pointer is not None
    assert int(current_pointer[0]) == second.source_version_id
    assert active is not None
    assert active[0] == _EXPECTED_DB_ROW_COUNT
    assert prior is not None
    assert prior[0] == _EXPECTED_DB_ROW_COUNT
    assert total is not None
    assert total[0] == 2 * _EXPECTED_DB_ROW_COUNT
    assert [(int(r[0]), r[1], int(r[2])) for r in version_rows] == [
        (first.source_version_id, "2026_05_14", _EXPECTED_DB_ROW_COUNT),
        (second.source_version_id, "2026_05_14", _EXPECTED_DB_ROW_COUNT),
    ]


def test_refresh_writes_expected_column_values(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_endpoint_files)
    cpic_loader.refresh(force=False)

    with duckdb_connection() as conn:
        row = conn.execute(
            """
            SELECT cpic_id, gene_symbol, drug_name, drug_rxnorm_id,
                   phenotype, recommendation, classification_strength,
                   cpic_level, pediatric, guideline_url,
                   publication_pmid, last_updated
              FROM cpic_guidelines
             WHERE cpic_id = '8093158'
            """,
        ).fetchone()
    assert row is not None
    (
        cpic_id,
        gene,
        drug,
        rxnorm,
        phenotype,
        recommendation,
        classification,
        level,
        pediatric,
        url,
        pmid,
        last_updated,
    ) = row
    assert cpic_id == "8093158"
    assert gene == "HLA-B"
    assert drug == "abacavir"
    assert rxnorm == "190521"
    assert phenotype == "*57:01 negative"
    assert recommendation == "Use abacavir per standard dosing guidelines"
    assert classification == "Strong"
    assert level == "A"
    assert pediatric is None
    assert url == "https://www.clinpgx.org/guideline/PA166251444"
    assert pmid == "32189324"
    assert last_updated is None


def test_refresh_invokes_download_for_all_five_endpoints(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each refresh must hit the four data endpoints + the version canary."""
    init_databases()
    counter = _patch_download_to_cache(monkeypatch, fixture_endpoint_files)
    cpic_loader.refresh(force=False)
    expected = {"guideline", "pair", "recommendation", "drug", "change_log_latest"}
    assert set(counter) == expected
    for endpoint in expected:
        assert counter[endpoint] == 1, f"{endpoint} called {counter[endpoint]}x"


def test_refresh_source_version_row_captures_combined_hash(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``annotation_source_versions`` records a single combined hash + total size."""
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_endpoint_files)
    cpic_loader.refresh(force=False)

    with duckdb_connection() as conn:
        current = get_current_version(conn, "cpic")
    assert current is not None
    assert current.version == "2026_05_14"
    assert current.source_url == cpic_loader.GUIDELINE_URL
    assert current.source_file_hash is not None
    assert len(current.source_file_hash) == 64  # sha256 hex digest
    assert current.source_file_size is not None
    # Total size = sum of the four data files (NOT including the canary).
    expected_total = sum(
        fixture_endpoint_files[name].stat().st_size
        for name in ("guideline", "pair", "recommendation", "drug")
    )
    assert current.source_file_size == expected_total


def test_refresh_transaction_rolls_back_on_bulk_insert_failure(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bulk-insert failure must leave both DB tables untouched."""
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_endpoint_files)

    boom = RuntimeError("simulated insert failure")

    def _explode(*_args: object, **_kwargs: object) -> int:
        raise boom

    monkeypatch.setattr(cpic_loader, "_bulk_insert", _explode)

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        cpic_loader.refresh(force=False)

    with duckdb_connection() as conn:
        version_rows = conn.execute(
            "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'cpic'",
        ).fetchone()
        cpic_rows = conn.execute("SELECT COUNT(*) FROM cpic_guidelines").fetchone()
    assert version_rows is not None
    assert version_rows[0] == 0
    assert cpic_rows is not None
    assert cpic_rows[0] == 0


# ---------------------------------------------------------------------------
# Registry / module-import side effects
# ---------------------------------------------------------------------------


def test_get_loader_returns_cpic_refresh() -> None:
    """Side-effect import wires the loader into the registry."""
    assert get_loader("cpic") is cpic_loader.refresh


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_refresh_cpic_runs_and_prints_summary(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_endpoint_files)

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "refresh", "--source", "cpic"])
    assert result.exit_code == 0, result.output
    assert "source_db=cpic" in result.output
    assert "version=2026_05_14" in result.output
    assert f"records={_EXPECTED_DB_ROW_COUNT}" in result.output
    assert "already_current=False" in result.output


def test_cli_status_after_refresh_reports_cpic_loaded(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_endpoint_files)
    cpic_loader.refresh(force=False)

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "status"])
    assert result.exit_code == 0, result.output
    cpic_lines = [line for line in result.output.splitlines() if line.startswith("cpic:")]
    assert len(cpic_lines) == 1
    line = cpic_lines[0]
    assert "2026_05_14" in line
    assert "ingested " in line
    assert f"{_EXPECTED_DB_ROW_COUNT} records" in line


def test_cli_status_after_both_loaders_reports_pharmgkb_and_cpic(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A combined refresh shows both PharmGKB and CPIC as loaded."""
    import zipfile  # noqa: PLC0415

    from genome.annotate.loaders import pharmgkb as pharmgkb_loader  # noqa: PLC0415

    init_databases()

    # CPIC refresh first.
    _patch_download_to_cache(monkeypatch, fixture_endpoint_files)
    cpic_loader.refresh(force=False)

    # Build a minimal PharmGKB ZIP fixture and route only its
    # download_to_cache through a separate stub.
    pgkb_path = tmp_path / "clinicalAnnotations.zip"
    with zipfile.ZipFile(pgkb_path, "w") as zf:
        zf.writestr("CREATED_2026-05-14.txt", "Created on 2026/05/14 at 00:00:00 UTC.\n")
        zf.writestr(
            "clinical_annotations.tsv",
            "Clinical Annotation ID\tVariant/Haplotypes\tGene\tLevel of Evidence\t"
            "Level Override\tLevel Modifiers\tScore\tPhenotype Category\tPMID Count\t"
            "Evidence Count\tDrug(s)\tPhenotype(s)\tLatest History Date (YYYY-MM-DD)\t"
            "URL\tSpecialty Population\n"
            "1001\trs951439\tRGS4\t3\t\t\t1.75\tEfficacy\t1\t1\trisperidone\t"
            "Schizophrenia\t2021-03-24\thttps://www.pharmgkb.org/clinicalAnnotation/1001\t\n",
        )

    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(pgkb_path.read_bytes()).hexdigest()
    size = pgkb_path.stat().st_size

    def _pgkb_stub(
        source_db: str,  # noqa: ARG001
        url: str,  # noqa: ARG001
        filename: str,  # noqa: ARG001
        *,
        resource_id: str,  # noqa: ARG001
        force: bool = False,  # noqa: ARG001
    ) -> annotate_downloads.DownloadResult:
        return annotate_downloads.DownloadResult(
            path=pgkb_path,
            sha256=digest,
            size_bytes=size,
        )

    monkeypatch.setattr(pharmgkb_loader, "download_to_cache", _pgkb_stub)
    pharmgkb_loader.refresh(force=False)

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "status"])
    assert result.exit_code == 0, result.output
    cpic_lines = [line for line in result.output.splitlines() if line.startswith("cpic:")]
    pgkb_lines = [line for line in result.output.splitlines() if line.startswith("pharmgkb:")]
    assert len(cpic_lines) == 1
    assert len(pgkb_lines) == 1
    assert "2026_05_14" in cpic_lines[0]
    assert "2026_05_14" in pgkb_lines[0]


def test_cli_refresh_force_flag_passes_through_to_loader(
    fixture_endpoint_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--force`` flag must reach ``refresh`` (covered with a recording stub)."""
    init_databases()
    _patch_download_to_cache(monkeypatch, fixture_endpoint_files)
    cpic_loader.refresh(force=False)

    received: dict[str, bool] = {}

    def _recording_refresh(
        force: bool,  # noqa: FBT001
        skip_if_same_version: bool,  # noqa: FBT001
    ) -> object:
        received["force"] = force
        received["skip_if_same_version"] = skip_if_same_version
        from genome.annotate.registry import RefreshResult  # noqa: PLC0415

        return RefreshResult(
            source_db="cpic",
            source_version_id=42,
            version="2026_05_14",
            record_count=0,
            was_already_current=False,
        )

    monkeypatch.setattr(
        "genome.annotate.registry._LOADERS",
        {"cpic": _recording_refresh},
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["annotate", "refresh", "--source", "cpic", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert received == {"force": True, "skip_if_same_version": False}


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_url_verified_date_is_iso_format() -> None:
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", cpic_loader.URL_VERIFIED_DATE)


def test_endpoint_urls_point_at_cpic_canonical_host() -> None:
    assert cpic_loader.GUIDELINE_URL == "https://api.cpicpgx.org/v1/guideline"
    assert cpic_loader.PAIR_URL == "https://api.cpicpgx.org/v1/pair"
    assert cpic_loader.RECOMMENDATION_URL == "https://api.cpicpgx.org/v1/recommendation"
    assert cpic_loader.DRUG_URL == "https://api.cpicpgx.org/v1/drug"


def test_change_log_canary_url_uses_latest_one_row_query() -> None:
    """The canary must be the minimal payload to keep version-resolution cheap."""
    assert cpic_loader.CHANGE_LOG_LATEST_URL == (
        "https://api.cpicpgx.org/v1/change_log?order=date.desc&limit=1&select=date"
    )


def test_source_db_label() -> None:
    assert cpic_loader.SOURCE_DB == "cpic"

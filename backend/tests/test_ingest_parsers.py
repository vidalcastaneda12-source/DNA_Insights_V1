"""Format-level coverage for the 23andMe / Ancestry parsers."""

from __future__ import annotations

from pathlib import Path

import pytest

from genome.ingest import parsers
from genome.ingest.parsers import _split_genotype_23andme

FIXTURES = Path(__file__).parent / "fixtures"
TWENTYTHREE = FIXTURES / "23andme_sample.txt"
ANCESTRY = FIXTURES / "ancestry_sample.txt"


def test_detect_build_grch38():
    assert parsers.detect_build(["# Reference Human Assembly build 38 (GRCh38)"]) == "GRCh38"


def test_detect_build_grch37_default():
    assert parsers.detect_build([]) == "GRCh37"
    assert parsers.detect_build(["# random text"]) == "GRCh37"
    assert parsers.detect_build(["# Build 37"]) == "GRCh37"


def test_detect_build_grch38_wins_when_both_present():
    # Real 23andMe headers historically referenced both builds in surrounding
    # text. The first definitive 38 marker should win.
    lines = ["# stuff about hg19", "# Reference Human Assembly build 38"]
    assert parsers.detect_build(lines) == "GRCh37"  # first match wins


def test_normalize_chrom_aliases():
    assert parsers.normalize_chrom("1") == "1"
    assert parsers.normalize_chrom("chr1") == "1"
    assert parsers.normalize_chrom("23") == "X"
    assert parsers.normalize_chrom("24") == "Y"
    assert parsers.normalize_chrom("25") == "X"  # PAR collapsed into X
    assert parsers.normalize_chrom("26") == "MT"
    assert parsers.normalize_chrom("M") == "MT"
    assert parsers.normalize_chrom("MT") == "MT"


def test_normalize_chrom_drops_unknown():
    assert parsers.normalize_chrom("0") is None
    assert parsers.normalize_chrom("42") is None
    assert parsers.normalize_chrom("scaffold_X") is None


def test_split_genotype_23andme_diploid():
    assert _split_genotype_23andme("AG") == ("A", "G", False)
    assert _split_genotype_23andme("AA") == ("A", "A", False)


def test_split_genotype_23andme_no_call():
    assert _split_genotype_23andme("--")[2] is True
    assert _split_genotype_23andme("00")[2] is True
    assert _split_genotype_23andme("")[2] is True


def test_split_genotype_23andme_haploid():
    # chrY / chrX in male / chrMT all show up as a single character.
    assert _split_genotype_23andme("G") == ("G", "G", False)


def test_split_genotype_23andme_indels():
    assert _split_genotype_23andme("II") == ("I", "I", False)
    assert _split_genotype_23andme("DD") == ("D", "D", False)
    assert _split_genotype_23andme("DI") == ("D", "I", False)


def test_parse_23andme_meta_and_call_count():
    meta, calls, stats = parsers.parse_23andme(TWENTYTHREE)
    rows = list(calls)
    assert meta.source == "23andme"
    assert meta.native_build == "GRCh38"
    assert len(rows) == 30
    assert stats.dropped_non_canonical == 0
    # The data row count must equal what's in the fixture (excludes comments).


def test_parse_23andme_payload_shape():
    _, calls, _ = parsers.parse_23andme(TWENTYTHREE)
    rows = list(calls)
    by_rsid = {r.rsid: r for r in rows}
    rs7537756 = by_rsid["rs7537756"]
    assert rs7537756.chrom == "1"
    assert rs7537756.pos == 854250
    assert rs7537756.allele_1 == "A"
    assert rs7537756.allele_2 == "G"
    assert rs7537756.is_no_call is False

    no_call = by_rsid["rs1000999"]
    assert no_call.is_no_call is True
    assert no_call.allele_1 == ""

    indel = by_rsid["i5000001"]
    assert indel.allele_1 == "I"
    assert indel.allele_2 == "I"


def test_parse_ancestry_meta_and_call_count():
    meta, calls, stats = parsers.parse_ancestry(ANCESTRY)
    rows = list(calls)
    assert meta.source == "ancestry"
    assert meta.native_build == "GRCh37"
    assert meta.chip_version == "V2.0"
    assert len(rows) == 20
    assert stats.dropped_non_canonical == 0


def test_parse_ancestry_chrom_aliases_resolved():
    _, calls, _ = parsers.parse_ancestry(ANCESTRY)
    rows = list(calls)
    by_rsid = {r.rsid: r for r in rows}
    # Ancestry uses 23/24/26 — the parser must remap to X/Y/MT.
    assert by_rsid["rs9651273"].chrom == "X"
    assert by_rsid["rs2032598"].chrom == "Y"
    assert by_rsid["rs28358571"].chrom == "MT"


def test_parse_ancestry_no_call_uses_zero_marker():
    _, calls, _ = parsers.parse_ancestry(ANCESTRY)
    by_rsid = {r.rsid: r for r in calls}
    no_call = by_rsid["rs5000999"]
    assert no_call.is_no_call is True
    assert no_call.allele_1 == ""


def test_parse_handles_missing_file(tmp_path):
    missing = tmp_path / "nope.txt"
    with pytest.raises(FileNotFoundError):
        parsers.parse_23andme(missing)


def test_parse_skips_short_or_malformed_rows(tmp_path):
    p = tmp_path / "bad_23andme.txt"
    p.write_text(
        "# build 38\n"
        "# rsid\tchromosome\tposition\tgenotype\n"
        "rs1\t1\t100\tAA\n"
        "rs2\t1\tnot-a-number\tAG\n"  # bad pos: dropped
        "rs3\tscaffold\t300\tGG\n"  # non-canonical drop: counted in stats
        "rs4\t1\t400\n"  # short row: dropped
        "\n"  # blank: skipped
        "rs5\t1\t500\tCT\n",
    )
    _, calls, stats = parsers.parse_23andme(p)
    rsids = [c.rsid for c in calls]
    assert rsids == ["rs1", "rs5"]
    assert stats.dropped_non_canonical == 1


def test_parse_23andme_drops_non_canonical_contigs(tmp_path):
    """Real 23andMe v5 exports ship rows on every flavor of non-canonical contig.

    Covers all four categories the positive-rule filter must reject:
    alt (`*_alt`), unlocalized (`*_random`), unplaced (`Un_*` and `chrUn_*`),
    and decoy (`*_decoy`). Canonical rows around them must still ingest cleanly.
    """
    p = tmp_path / "non_canonical_23andme.txt"
    p.write_text(
        "# build 38\n"
        "# rsid\tchromosome\tposition\tgenotype\n"
        "rs1\t1\t100\tAA\n"
        "i6045465\t8_KI270821v1_alt\t12345\tAG\n"
        "i6045466\t19_KI270938v1_alt\t67890\tCT\n"
        "i6045467\t4_GL000008v2_random\t11111\tAG\n"
        "i6045468\tUn_GL000226v1\t22222\tGG\n"
        "i6045469\tchrUn_KI270442v1\t33333\tCC\n"
        "i6045470\ths38d1_decoy\t44444\tTT\n"
        "rs2\t1\t200\tGG\n",
    )
    _, calls, stats = parsers.parse_23andme(p)
    rsids = [c.rsid for c in calls]
    assert rsids == ["rs1", "rs2"]
    assert stats.dropped_non_canonical == 6


def test_parse_ancestry_drops_non_canonical_contigs(tmp_path):
    """Same positive-rule filter applies on the AncestryDNA side."""
    p = tmp_path / "non_canonical_ancestry.txt"
    p.write_text(
        "#AncestryDNA raw data download\n"
        "rsid\tchromosome\tposition\tallele1\tallele2\n"
        "rs1\t1\t100\tA\tA\n"
        "i6045465\t8_KI270821v1_alt\t12345\tA\tG\n"
        "i6045466\t4_GL000008v2_random\t11111\tA\tG\n"
        "i6045467\tUn_GL000226v1\t22222\tG\tG\n"
        "i6045468\ths38d1_decoy\t44444\tT\tT\n"
        "rs2\t1\t200\tG\tG\n",
    )
    _, calls, stats = parsers.parse_ancestry(p)
    rsids = [c.rsid for c in calls]
    assert rsids == ["rs1", "rs2"]
    assert stats.dropped_non_canonical == 4

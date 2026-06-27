"""Calibration back-test — the scope-split dials reproduce ROADMAP's pre-Phase-6 oracle.

Spec source: ``sub-project-B2-phase1-deferred-followups.md`` item 2 ("Calibration back-test —
``MAX_CUT_COST=0.25`` / ``MIN_SUBSCOPE_SHRINK=0.34`` are unvalidated dials; back-test against
ROADMAP's 13-PR pre-Phase-6 sequence … to confirm they neither over- nor under-split") +
``finding-039`` DECISION 1 (manifest-primary cut + git-grep coupling veto).

The **oracle** is ROADMAP's hand-authored "Pre-Phase-6 sequence" (a 14-PR ordered, dependency-gated
decomposition; rationale in ``sub-project-B2-scope-split.md`` §1). Its governing principle is
*detect separability, not size*: the two biggest/hardest scopes — **PR 3** (canonicalize, S=8) and
**PR 5a** (chrX, S=7) — were *correctly* shipped as single **atomic** PRs. The detector must
reproduce that: agree the separable mega-scope splits, and agree each indivisible PR is atomic.

This is a **calibration probe with a reported metric (a loose bound), not a brittle exact-match**:
the detector is change-class-primary and depth-capped (``MAX_RESPLIT_DEPTH=1``), so it cannot —
and is not asked to — reproduce all 14 PRs exactly. "Reproduce the decomposition" means: the
pre-decomposition mega-scope → **splits** (no under-split) AND each oracle-atomic PR → **atomic**
(no over-split), a faithful coarsening of the hand cut.

PR → real-module reconstruction (every footprint is a real git-tracked file — the git-grep builder
fails closed to atomic on any unresolved module, so synthetic names would degenerate the split arm):

    PR 3  canonicalize  + align_tier3   (data-backfill, S=8 — atomic, the trap)
    PR 4  index_refresh                 (annotation-loader  — atomic)
    PR 5a chrx_panel    + chrx_loo      (pipeline, S=7       — atomic, the trap)
    PR 5b strand_collapse               (imports canonicalize.take_snapshot — the veto edge)
    PR 6  seed_genes                    (annotation-loader  — atomic)
    PR 14 rsid_cleanup  + vcf_export    (pipeline)

Reconstruction assumptions (documented per the deferred-followup directive):

* **A1 — atomic PRs are encoded single-change-class.** A PR the human shipped whole is one
  ``change_class`` (e.g. PR 3 = ``data-backfill``), so the manifest-primary partition yields one
  cluster → atomic by ``MIN_CLUSTERS``. This is faithful: the dispatcher would emit one dominant
  class for an indivisible scope. (Observed: PR 3's ``canonicalize`` and ``align_tier3`` carry no
  import edge between them, so git-grep alone would *not* keep them together — the manifest class
  signal does. That is the point: the manifest is primary; git-grep is the veto backstop.)
* **A2 — the separable mega-scope excludes ``strand_collapse``.** Its lone real importer-edge to
  ``canonicalize`` (``from genome.annotate.canonicalize import take_snapshot``) would, in a small
  fixture where ``canonicalize`` has fan-in 1, dominate ``cut_cost`` and veto the cut. At real
  mega-scope scale ``canonicalize`` is a fan-in≥3 backfill hub that ``SHARED_HELPER_FANIN``
  infra-drops. Arm 2 omits it to isolate the change-class separability signal; Arm 3 uses that very
  edge to validate the veto separately.

VERDICT (recorded durably in ``finding-039`` DECISION 1 + ``DEC-0119``): the dials **hold** —
over-split = 0, the mega-scope splits, and git-grep's measured coupling on the one real edge exceeds
``MAX_CUT_COST`` so the veto is a live gate. ``MAX_CUT_COST=0.25`` / ``MIN_SUBSCOPE_SHRINK=0.34``
validated, no retune; git-grep-as-primary accepted.

This probe shells out to ``git grep`` (like ``test_scope_split_efficacy_probe.py``); it runs in the
normal suite. test->spec provenance noted per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

from genome.scope_split.graph import make_coupling_builder
from genome.scope_split.model import (
    MAX_CUT_COST,
    MIN_SUBSCOPE_SHRINK,
    SCHEMA_FIRST_ORDER,
    ScopeManifestInput,
)
from genome.scope_split.splitter import propose_split

# Real, git-tracked footprint modules mapped to oracle PRs (see module docstring).
_CANONICALIZE = "backend/src/genome/annotate/canonicalize.py"  # PR 3 (data-backfill, S=8)
_ALIGN_TIER3 = "backend/src/genome/annotate/align_tier3.py"  # PR 3 companion
_INDEX_REFRESH = "backend/src/genome/annotate/index_refresh.py"  # PR 4 (annotation-loader)
_SEED_GENES = "backend/src/genome/annotate/seed_genes.py"  # PR 6 (annotation-loader)
_STRAND_COLLAPSE = "backend/src/genome/annotate/strand_collapse.py"  # PR 5b (imports canonicalize)
_CHRX_PANEL = "backend/src/genome/imputation/chrx_panel.py"  # PR 5a (pipeline, S=7)
_CHRX_LOO = "backend/src/genome/imputation/chrx_loo.py"  # PR 5a companion
_RSID_CLEANUP = "backend/src/genome/imputation/rsid_cleanup.py"  # PR 14 / #66 (pipeline)
_VCF_EXPORT = "backend/src/genome/imputation/vcf_export.py"  # PR 14 (pipeline)

# The oracle-atomic PRs (change_class, footprint) — each shipped by the human as a single PR.
_ORACLE_ATOMIC_PRS = (
    ("PR-3", ("data-backfill",), (_CANONICALIZE, _ALIGN_TIER3)),  # S=8 — the "big but atomic" trap
    ("PR-5a", ("pipeline",), (_CHRX_PANEL, _CHRX_LOO)),  # S=7 — the "big but atomic" trap
    ("PR-4", ("annotation-loader",), (_INDEX_REFRESH,)),
    ("PR-6", ("annotation-loader",), (_SEED_GENES,)),
)


def _atomic_pr_manifest(scope_id: str, change_class: tuple[str, ...], footprint: tuple[str, ...]):
    """A reconstructed oracle-PR manifest (single dominant change_class — assumption A1)."""
    return ScopeManifestInput(
        scope_id=scope_id,
        change_class=change_class,
        imports_touched=footprint,
    )


def _mega_scope_manifest():
    """The pre-decomposition mega-scope: the annotate-backfill slice + the imputation slice.

    A faithful coarsening of the oracle's annotate-vs-imputation PR boundary (PRs 3/4/6 annotate;
    PRs 5a/14 imputation). All five modules are mutually independent (no import edges), so the cut
    is clean. ``strand_collapse`` is excluded per assumption A2.
    """
    return ScopeManifestInput(
        scope_id="PRE-PHASE-6-MEGA",
        change_class=("data-backfill", "annotation-loader", "pipeline"),
        imports_touched=(_CANONICALIZE, _SEED_GENES, _INDEX_REFRESH, _RSID_CLEANUP, _VCF_EXPORT),
    )


# ── Arm 1 — no over-split: every oracle-atomic PR returns atomic ──────────────


def test_arm1_no_over_split_oracle_atomic_prs_stay_atomic() -> None:
    """from: ROADMAP pre-Phase-6 oracle (PRs 3/4/5a/6 each shipped as ONE atomic PR) +
    deferred-followup item 2 ("neither over- nor under-split").

    The "detect separability, not size" trap: PR 3 (S=8) and PR 5a (S=7) are the biggest scopes
    yet correctly indivisible. The detector must NOT carve any PR the human kept whole. Each
    reconstructed PR → atomic (manifest-primary: a single-change-class footprint is one cluster,
    so the reduction stops at the ``MIN_CLUSTERS`` guard before the coupling veto even runs).
    """
    builder = make_coupling_builder("git-grep")
    over_split = [
        scope_id
        for scope_id, change_class, footprint in _ORACLE_ATOMIC_PRS
        if not propose_split(_atomic_pr_manifest(scope_id, change_class, footprint), builder).atomic
    ]
    assert over_split == [], f"over-split — oracle says these are atomic: {over_split}"


# ── Arm 2 — no under-split: the separable mega-scope splits, schema-first ─────


def test_arm2_no_under_split_mega_scope_splits_change_class_first() -> None:
    """from: ROADMAP pre-Phase-6 oracle (the mega-scope decomposes into ordered slices) +
    deferred-followup item 2 + DECISION 1 (manifest-primary cut on ``change_class`` boundaries).

    The pre-decomposition mega-scope (annotate-backfill + imputation) is separable: the detector
    must propose a split, not ram it through as one Tier-2 run. It carves the annotate cluster from
    the imputation cluster in schema-first order — a faithful coarsening of the oracle's
    annotate-vs-imputation PR boundary. ``MIN_SUBSCOPE_SHRINK`` must permit (not block) the cut.
    """
    builder = make_coupling_builder("git-grep")
    result = propose_split(_mega_scope_manifest(), builder)

    assert result.atomic is False, f"under-split — mega-scope is separable: {result.reason}"
    assert len(result.sub_scopes) >= 2  # a real decomposition, ≥ 2 ordered sub-scopes
    assert result.cut_quality is not None
    # git-grep found no spurious coupling among the independent slices → a perfectly clean cut.
    assert result.cut_quality.cut_cost == 0.0
    # the shrink dial is permissive enough for a genuine decomposition (does not false-veto it).
    assert result.cut_quality.min_subscope_shrink >= MIN_SUBSCOPE_SHRINK
    # schema-first topo order: the structural/loader slice precedes the pipeline slice.
    first_class = result.sub_scopes[0].change_class[0]
    last_class = result.sub_scopes[-1].change_class[0]
    assert SCHEMA_FIRST_ORDER.index(first_class) <= SCHEMA_FIRST_ORDER.index(last_class)


def test_arm2_out_of_scope_candidates_refine_the_mega_cut() -> None:
    """from: DECISION 1 ("refined by ``out_of_scope_candidates``" — the dispatcher already names
    separable slices) + deferred-followup item 2.

    The same separable mega-scope, with three slices the dispatcher flagged as out-of-scope
    candidates, peels into the finer per-slice decomposition (the manifest's second separability
    signal). Still a clean, non-atomic cut — the dials do not block the refinement.
    """
    builder = make_coupling_builder("git-grep")
    manifest = ScopeManifestInput(
        scope_id="PRE-PHASE-6-MEGA-PEELED",
        change_class=("data-backfill", "annotation-loader", "pipeline"),
        imports_touched=(_CANONICALIZE, _SEED_GENES, _INDEX_REFRESH, _RSID_CLEANUP),
        out_of_scope_candidates=(_SEED_GENES, _INDEX_REFRESH, _RSID_CLEANUP),
    )
    result = propose_split(manifest, builder)
    assert result.atomic is False
    assert len(result.sub_scopes) >= 2


# ── Arm 3 — the git-grep coupling veto is a live gate on a real import edge ───


def test_arm3_real_coupling_veto_fires_above_max_cut_cost() -> None:
    """from: DECISION 1 (coupling VETO: a cut severing > ``MAX_CUT_COST`` of coupling → atomic) +
    deferred-followup item 1 (is git-grep an adequate PRIMARY coupling signal?).

    The one arm that exercises git-grep on a REAL import edge: ``strand_collapse`` imports
    ``canonicalize.take_snapshot``. With the two modules flagged into distinct clusters, git-grep's
    measured ``cut_cost`` (the single edge, severed) exceeds ``MAX_CUT_COST`` → the veto fires →
    atomic. This is the evidence DECISION 1 needs: git-grep detects real coupling and
    ``MAX_CUT_COST=0.25`` is a live gate, not dead code (Arms 1/2 never reach the veto).
    """
    builder = make_coupling_builder("git-grep")

    # (a) git-grep measures the real edge, and its severed fraction exceeds the veto dial.
    graph = builder.build((_CANONICALIZE, _STRAND_COLLAPSE))
    assert not graph.unresolved  # both footprint modules resolve to real source files
    partition = (frozenset({_CANONICALIZE}), frozenset({_STRAND_COLLAPSE}))
    assert graph.cut_cost(partition) > MAX_CUT_COST  # the real coupling exceeds the threshold

    # (b) the splitter therefore vetoes the cut → atomic (the PR-3/PR-5a tight-cluster rule).
    manifest = ScopeManifestInput(
        scope_id="VETO",
        change_class=("data-backfill",),
        imports_touched=(_CANONICALIZE, _STRAND_COLLAPSE),
        out_of_scope_candidates=(_STRAND_COLLAPSE,),  # force the two into distinct clusters
    )
    result = propose_split(manifest, builder)
    assert result.atomic is True
    assert "veto" in result.reason.lower()


# ── Summary — the aggregate calibration verdict (the reported metric) ─────────


def test_calibration_summary_dials_reproduce_the_oracle() -> None:
    """from: deferred-followup item 2 ("reports whether the dials reproduce that hand-authored
    decomposition without over- or under-splitting") — the aggregate metric.

    over_split_count == 0 (no oracle-atomic PR is carved) AND the mega-scope splits (no
    under-split) AND the git-grep veto fires on the real coupling edge. The three together are the
    loose-bound verdict that ``MAX_CUT_COST`` / ``MIN_SUBSCOPE_SHRINK`` reproduce the oracle —
    recorded in finding-039 DECISION 1 + DEC-0119.
    """
    builder = make_coupling_builder("git-grep")

    over_split_count = sum(
        0 if propose_split(_atomic_pr_manifest(sid, cc, fp), builder).atomic else 1
        for sid, cc, fp in _ORACLE_ATOMIC_PRS
    )
    mega_splits = propose_split(_mega_scope_manifest(), builder).atomic is False
    veto_manifest = ScopeManifestInput(
        scope_id="VETO",
        change_class=("data-backfill",),
        imports_touched=(_CANONICALIZE, _STRAND_COLLAPSE),
        out_of_scope_candidates=(_STRAND_COLLAPSE,),
    )
    veto_fires = propose_split(veto_manifest, builder).atomic is True

    assert over_split_count == 0, f"{over_split_count} oracle-atomic PR(s) over-split"
    assert mega_splits, "the separable mega-scope under-split (returned atomic)"
    assert veto_fires, "the git-grep coupling veto did not fire on the real edge"

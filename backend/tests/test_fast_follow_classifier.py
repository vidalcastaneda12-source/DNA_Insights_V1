"""Classifier reduction — ``classify`` (the §2 SAFETY INVARIANT, the rigor center).

Plan-blind spec source: synthesized-plan §2 (the safety invariant: "no candidate carrying a
guarded class, non-empty anchor set, over-cap blast_radius, or a touched path under
docs/schemas/** or ddl/** is EVER classified DRAIN"), §4 classifier reduction order (1.
extraction fail-closed → EJECT; 2. touched_paths INDEPENDENT literal-path guard → EJECT; 3.
stale → DISCARD; 4. guarded-class / anchors!=0 / blast>cap → EJECT; 5. else → DRAIN), §5 test
list item 2 ("EXHAUSTIVE PROPERTY test … enumerated not sampled; truth table;
single-attribute-flip negative-control sweep; touched_paths guard incl. class-mismatch;
fail-closed extraction"), A2 (the path guard runs on LITERAL read-from-disk paths, never on
the derived change_class label, so a mislabelled-core schema item still EJECTs), and the
FROZEN INTERFACE CONTRACT (the ``Candidate`` field names; ``classify(candidate) -> Triage``;
the ``Classification`` members; ``GUARDED_CLASSES`` / ``MAX_DRAIN_FILES`` values).

Every expected classification comes from the §4 reduction order, which pins the exact outcome
for every attribute combination; nothing is reverse-engineered from the stubbed body
(``classify`` ``raise NotImplementedError`` now — RED is correct, RED on NotImplementedError).

Pre-mortem coverage (RANKED riskiest #1: "Candidate attributes are TRUSTED skill-derived
inputs the core can't verify → mis-extraction relocates false-DRAIN to the scan step"): the
exhaustive property test is the guard test proving the CORE never emits a false DRAIN for any
guard-tripping combination, so even a mis-extracted attribute that survives to the classifier
cannot produce a false DRAIN.
"""

from __future__ import annotations

import itertools

from genome.fast_follow.classifier import classify
from genome.fast_follow.model import (
    GUARDED_CLASSES,
    MAX_DRAIN_FILES,
    Candidate,
    Classification,
)

# ── A canonical KNOWN-DRAIN candidate (every guard cleared) ───────────────────


def _drain_candidate(  # noqa: PLR0913 — a typed test factory; each kwarg is one flippable guard attribute
    *,
    candidate_id: str = "cand-drain",
    change_class: frozenset[str] = frozenset({"core"}),
    blast_radius: int | None = 1,
    applicable_anchors: int | None = 0,
    tier: str | None = "tier-0",
    touched_paths: tuple[str, ...] = ("docs/notes/foo.md",),
    is_stale: bool = False,
) -> Candidate:
    """A docs/core candidate that clears every guard → must classify DRAIN.

    Defaults: change_class={core}, anchors=0, blast_radius≤cap, tier-0, no schema/ddl path,
    not stale. Single attributes are overridden per test to walk the truth table / flip sweep.
    """
    return Candidate(
        candidate_id=candidate_id,
        source="repo-sweep",
        kind="doc-nit",
        change_class=change_class,
        blast_radius=blast_radius,
        applicable_anchors=applicable_anchors,
        tier=tier,
        touched_paths=touched_paths,
        is_stale=is_stale,
    )


# ── EXHAUSTIVE PROPERTY: no guard-tripping candidate EVER yields DRAIN ─────────


def test_no_guard_tripping_candidate_is_ever_drain_exhaustive() -> None:
    """from: plan §2 (the SAFETY INVARIANT) + §5 item 2 (EXHAUSTIVE, enumerated not sampled) +
    pre-mortem riskiest #1.

    Enumerate the full cross-product of the guard-relevant Candidate attributes and assert:
    ANY candidate that (a) carries a GUARDED class, OR (b) has applicable_anchors != 0, OR
    (c) has blast_radius > MAX_DRAIN_FILES, OR (d) touches a path under docs/schemas/** or
    ddl/** — NEVER classifies DRAIN. This is the §2 invariant proven over the enumerated space,
    not a sample.
    """
    change_class_options: list[frozenset[str]] = [
        frozenset({"core"}),
        frozenset({"schema"}),
        frozenset({"pipeline"}),
        frozenset({"annotation"}),
        frozenset({"core", "schema"}),
        frozenset({"core", "annotation"}),
    ]
    anchor_options: list[int] = [0, 1, 5]
    blast_options: list[int] = [0, 1, MAX_DRAIN_FILES, MAX_DRAIN_FILES + 1, 10]
    path_options: list[tuple[str, ...]] = [
        ("docs/notes/foo.md",),
        ("docs/schemas/schema_group_1.md",),
        ("ddl/group_5_app_state.sql",),
        ("backend/src/genome/foo.py", "docs/schemas/x.md"),
    ]
    tier_options: list[str] = ["tier-0", "tier-1"]

    for change_class, anchors, blast, paths, tier in itertools.product(
        change_class_options, anchor_options, blast_options, path_options, tier_options
    ):
        trips_guard = (
            bool(change_class & GUARDED_CLASSES)
            or anchors != 0
            or blast > MAX_DRAIN_FILES
            or any(p.startswith(("docs/schemas/", "ddl/")) for p in paths)
        )
        if not trips_guard:
            continue
        candidate = _drain_candidate(
            change_class=change_class,
            applicable_anchors=anchors,
            blast_radius=blast,
            touched_paths=paths,
            tier=tier,
        )
        result = classify(candidate)
        assert result.classification is not Classification.DRAIN, (
            "SAFETY INVARIANT VIOLATED — a guard-tripping candidate classified DRAIN: "
            f"change_class={set(change_class)} anchors={anchors} blast={blast} "
            f"paths={paths} tier={tier}"
        )


# ── Truth table: each row pins the exact specified classification ─────────────


def test_truth_table_core_tier0_no_guard_is_drain() -> None:
    """from: plan §4 step 5 (tier-0 core, anchors=0, blast≤cap, no schema path → DRAIN)."""
    candidate = _drain_candidate(change_class=frozenset({"core"}), tier="tier-0")
    assert classify(candidate).classification is Classification.DRAIN


def test_truth_table_tier1_small_core_no_anchor_is_drain() -> None:
    """from: plan §4 step 5 (bounded tier-1 small core with no anchors → DRAIN)."""
    candidate = _drain_candidate(tier="tier-1", blast_radius=2, applicable_anchors=0)
    assert classify(candidate).classification is Classification.DRAIN


def test_truth_table_schema_class_is_eject() -> None:
    """from: plan §4 step 4 (guarded class schema → EJECT)."""
    candidate = _drain_candidate(change_class=frozenset({"schema"}))
    assert classify(candidate).classification is Classification.EJECT


def test_truth_table_applicable_anchors_one_is_eject() -> None:
    """from: plan §4 step 4 (applicable_anchors != 0 → EJECT)."""
    candidate = _drain_candidate(applicable_anchors=1)
    assert classify(candidate).classification is Classification.EJECT


def test_truth_table_blast_over_cap_is_eject() -> None:
    """from: plan §4 step 4 (blast_radius > MAX_DRAIN_FILES → EJECT)."""
    candidate = _drain_candidate(blast_radius=MAX_DRAIN_FILES + 1)
    assert classify(candidate).classification is Classification.EJECT


def test_truth_table_is_stale_is_discard() -> None:
    """from: plan §4 step 3 (is_stale → DISCARD)."""
    candidate = _drain_candidate(is_stale=True)
    assert classify(candidate).classification is Classification.DISCARD


# ── Single-attribute-flip negative-control sweep (start from DRAIN) ───────────
# Flip exactly ONE guard attribute on a known-DRAIN candidate; assert it leaves DRAIN.


def test_flip_change_class_to_pipeline_ejects() -> None:
    """from: plan §4 step 4 + §5 single-attribute-flip negative control."""
    assert (
        classify(_drain_candidate(change_class=frozenset({"pipeline"}))).classification
        is Classification.EJECT
    )


def test_flip_change_class_to_annotation_ejects() -> None:
    """from: plan §4 step 4 + §5 single-attribute-flip negative control."""
    assert (
        classify(_drain_candidate(change_class=frozenset({"annotation"}))).classification
        is Classification.EJECT
    )


def test_flip_change_class_add_schema_ejects() -> None:
    """from: plan §4 step 4 (change_class ∩ GUARDED_CLASSES, even mixed with core → EJECT)."""
    assert (
        classify(_drain_candidate(change_class=frozenset({"core", "schema"}))).classification
        is Classification.EJECT
    )


def test_flip_anchors_nonzero_ejects() -> None:
    """from: plan §4 step 4 + §5 single-attribute-flip negative control."""
    assert classify(_drain_candidate(applicable_anchors=2)).classification is Classification.EJECT


def test_flip_blast_radius_to_four_ejects() -> None:
    """from: plan §4 step 4 (blast_radius=4 > cap=3 → EJECT) + §5 single-attribute-flip."""
    assert classify(_drain_candidate(blast_radius=4)).classification is Classification.EJECT


def test_flip_is_stale_discards() -> None:
    """from: plan §4 step 3 (is_stale flip → DISCARD, not EJECT) + §5 single-attribute-flip."""
    assert classify(_drain_candidate(is_stale=True)).classification is Classification.DISCARD


def test_flip_touched_path_to_schema_ejects() -> None:
    """from: plan §4 step 2 (touched_paths under docs/schemas/** → EJECT) + §5 flip sweep."""
    assert (
        classify(_drain_candidate(touched_paths=("docs/schemas/schema_group_2.md",))).classification
        is Classification.EJECT
    )


def test_flip_touched_path_to_ddl_ejects() -> None:
    """from: plan §4 step 2 (touched_paths under ddl/** → EJECT) + §5 flip sweep."""
    assert (
        classify(_drain_candidate(touched_paths=("ddl/group_2_reference.sql",))).classification
        is Classification.EJECT
    )


# ── touched_paths class-mismatch: the INDEPENDENT literal-path guard (A2) ──────


def test_mislabelled_core_touching_ddl_still_ejects() -> None:
    """from: plan §4 step 2 + A2 (the path guard keys on the LITERAL touched path, not the
    derived change_class label) + §5 item 2 (class-mismatch case).

    A candidate the skill mislabels ``change_class={core}`` but whose LITERAL touched_paths
    include ``ddl/group_5.sql`` must STILL EJECT — the independent literal-path guard catches
    the mislabel that the class-based guard (step 4) would have missed. This is the §2 safety
    invariant's defence-in-depth.
    """
    mislabelled = _drain_candidate(
        change_class=frozenset({"core"}),
        touched_paths=("ddl/group_5.sql",),
    )
    assert classify(mislabelled).classification is Classification.EJECT


def test_mislabelled_core_touching_schema_doc_still_ejects() -> None:
    """from: plan §4 step 2 + A2 + §5 class-mismatch case (docs/schemas/** side)."""
    mislabelled = _drain_candidate(
        change_class=frozenset({"core"}),
        touched_paths=("docs/schemas/schema_group_1_raw_inputs.md",),
    )
    assert classify(mislabelled).classification is Classification.EJECT


# ── Fail-closed extraction: any undecidable field → EJECT ─────────────────────


def test_empty_change_class_ejects() -> None:
    """from: plan §4 step 1 (empty change_class → EJECT) + §5 fail-closed extraction.

    An empty (undecidable) change_class fails closed to EJECT — never DRAIN.
    """
    assert (
        classify(_drain_candidate(change_class=frozenset())).classification is Classification.EJECT
    )


def test_applicable_anchors_none_ejects() -> None:
    """from: plan §4 step 1 (None applicable_anchors → EJECT) + §5 fail-closed extraction."""
    assert (
        classify(_drain_candidate(applicable_anchors=None)).classification is Classification.EJECT
    )


def test_blast_radius_none_ejects() -> None:
    """from: plan §4 step 1 (None blast_radius → EJECT) + §5 fail-closed extraction."""
    assert classify(_drain_candidate(blast_radius=None)).classification is Classification.EJECT


def test_tier_none_ejects() -> None:
    """from: plan §4 step 1 (None tier → EJECT) + §5 fail-closed extraction."""
    assert classify(_drain_candidate(tier=None)).classification is Classification.EJECT


# ── Reduction ORDER: stale dominates the class/anchor guards; path guard runs
#    before the stale check is not asserted (order 2 vs 3) — only the published
#    contract outcomes are pinned. ──────────────────────────────────────────────


def test_stale_guarded_class_is_discard_not_eject() -> None:
    """from: plan §4 reduction order (step 3 stale → DISCARD precedes step 4 class → EJECT).

    A candidate that is BOTH stale AND carries a guarded class reduces to DISCARD — the stale
    check (step 3) fires before the class guard (step 4). Pins the published order.
    """
    candidate = _drain_candidate(change_class=frozenset({"schema"}), is_stale=True)
    assert classify(candidate).classification is Classification.DISCARD


# ── Out-of-vocab fail-closed (review: silent-failure-hunter silent-1/silent-2) ─
# An out-of-vocab change_class label or tier is a mis-derived / undecidable read; the
# fail-closed contract maps it to EJECT, never DRAIN. (The exhaustive sweep above only
# enumerates in-vocab labels, so these pin the membership-not-just-presence guard.)


def test_out_of_vocab_change_class_typo_is_eject() -> None:
    """from: review silent-1 — a typo'd class ('pipline') must EJECT, not silently DRAIN."""
    assert (
        classify(_drain_candidate(change_class=frozenset({"pipline"}))).classification
        is Classification.EJECT
    )


def test_out_of_vocab_change_class_unknown_is_eject() -> None:
    """from: review silent-1 — an entirely unknown class ('infra') must EJECT (undecidable)."""
    assert (
        classify(_drain_candidate(change_class=frozenset({"infra"}))).classification
        is Classification.EJECT
    )


def test_out_of_vocab_change_class_mixed_with_core_is_eject() -> None:
    """from: review silent-1 — a benign 'core' mixed with an unknown label still EJECTs."""
    assert (
        classify(_drain_candidate(change_class=frozenset({"core", "infra"}))).classification
        is Classification.EJECT
    )


def test_out_of_vocab_tier_is_eject() -> None:
    """from: review silent-2 — a tier outside TIER_VOCAB ('tier-9') must EJECT, not DRAIN."""
    assert classify(_drain_candidate(tier="tier-9")).classification is Classification.EJECT


# ── At-cap boundary + provenance (review: pr-test-analyzer ptest-1/2/9) ────────


def test_truth_table_blast_at_cap_is_drain() -> None:
    """from: review ptest-1 — blast_radius == MAX_DRAIN_FILES (the exact at-cap boundary) → DRAIN.

    Guards the strict '>' in the classifier: a future '>=' edit would break this with no other
    failing test (the exhaustive sweep skips the non-guard-tripping arm).
    """
    assert (
        classify(_drain_candidate(blast_radius=MAX_DRAIN_FILES)).classification
        is Classification.DRAIN
    )


def test_drain_records_drains_provenance() -> None:
    """from: review ptest-2 — a DRAIN verdict carries drains == candidate_id (decision #8)."""
    candidate = _drain_candidate(candidate_id="cand-prov")
    result = classify(candidate)
    assert result.classification is Classification.DRAIN
    assert result.drains == "cand-prov"


def test_eject_and_discard_drains_is_none() -> None:
    """from: review ptest-9 — EJECT / DISCARD verdicts carry drains is None (no provenance)."""
    assert classify(_drain_candidate(change_class=frozenset({"schema"}))).drains is None
    assert classify(_drain_candidate(is_stale=True)).drains is None

"""DECISION 2 efficacy probe — propose_split FIRES on REAL git-grep import data.

Plan-blind spec source: IMPL-CONTRACT DECISION 2 ("Efficacy probe is a HARD §6 verification
gate"): (a) a real ATOMIC scope (single module / tight cluster) run through propose_split with
the REAL git-grep builder (engine='git-grep') → atomic True; (b) a fat manifest = the UNION of
two known-separable real slices that share only infra helpers (which DECISION 1 drops) →
atomic False, 2 sub_scopes — "Proves the core FIRES on real import data, not just synthetic
injected graphs." FROZEN-INTERFACE (make_coupling_builder('git-grep'); propose_split STUBBED +
GitGrepCouplingBuilder.build STUBBED).

RED-until-filled: both GitGrepCouplingBuilder.build and propose_split must land before these
pass. Each asserts the SPECIFIED real-repo outcome (atomic True / atomic False + 2 sub-scopes),
so they go RED on NotImplementedError now and GREEN when the bodies land — never
pytest.raises(NotImplementedError).

DETERMINISM: the two (b) files — genome/fast_follow/cli.py (buckets to change_class cli) and
backend/tests/test_fast_follow_model.py (buckets to change_class tests) — are both git-tracked
and share NO import edge (the cli module does not import the test, nor vice versa), so the
coupling veto permits the cut and the manifest-primary partition carves them into 2 sub-scopes.
This is a real, stable, git-tracked in-repo pair.

This probe shells out to ``git grep`` (slower); it runs in the normal suite (no skip marker).

test->spec provenance noted per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

from genome.scope_split.graph import make_coupling_builder
from genome.scope_split.model import ScopeManifestInput
from genome.scope_split.splitter import propose_split

# Real, git-tracked files. The model is a single tight module (atomic case); the cli + test
# files bucket to distinct change_class slices (cli / tests) and share no import edge (split).
_FAST_FOLLOW_MODEL = "backend/src/genome/fast_follow/model.py"
_FAST_FOLLOW_CLI = "backend/src/genome/fast_follow/cli.py"
_FAST_FOLLOW_TEST = "backend/tests/test_fast_follow_model.py"


def test_efficacy_real_atomic_single_module_scope_is_atomic() -> None:
    """from: IMPL-CONTRACT DECISION 2 (a) ("a real ATOMIC scope … propose_split with the REAL
    git-grep builder → assert atomic is True (correct-atomic on a true blob / tight cluster)").

    A single-module manifest is one indivisible cluster: against the REAL git-grep import graph
    it is correctly atomic (the detector does not over-propose). RED until GitGrep build +
    propose_split land.
    """
    manifest = ScopeManifestInput(
        scope_id="EFFICACY-ATOMIC",
        change_class=("tests",),
        imports_touched=(_FAST_FOLLOW_MODEL,),
    )
    builder = make_coupling_builder("git-grep")
    result = propose_split(manifest, builder)
    assert result.atomic is True


def test_efficacy_real_separable_union_splits_into_two() -> None:
    """from: IMPL-CONTRACT DECISION 2 (b) ("a fat manifest = the UNION of two known-separable
    real slices … run propose_split with the real git-grep builder → assert atomic is False and
    the two slices come back as 2 sub_scopes. Proves the core FIRES on real import data").

    The UNION of two real leaf modules with disjoint non-infra imports, split across two
    change_class boundaries, must come back NON-atomic with exactly 2 sub-scopes against the REAL
    git-grep graph (no high-coupling edge to veto the cut). RED until GitGrep build +
    propose_split land.
    """
    manifest = ScopeManifestInput(
        scope_id="EFFICACY-SPLIT",
        change_class=("cli", "tests"),
        imports_touched=(_FAST_FOLLOW_CLI, _FAST_FOLLOW_TEST),
    )
    builder = make_coupling_builder("git-grep")
    result = propose_split(manifest, builder)
    assert result.atomic is False
    assert len(result.sub_scopes) == 2
    assert all(s.origin_scope == "EFFICACY-SPLIT" for s in result.sub_scopes)

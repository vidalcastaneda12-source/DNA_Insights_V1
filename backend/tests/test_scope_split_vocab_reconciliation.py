"""Vocab reconciliation — scope_split.CHANGE_CLASS_VOCAB == the dispatcher C-map intake vocab.

Plan-blind spec source: SYNTHESIZED-PLAN §3 ("Vocab independence: own CHANGE_CLASS_VOCAB
reconciled to DISPATCHER C-map NOT verify_gate gate vocab") + §5 ("vocab_reconciliation
(against DISPATCHER C-map NOT verify_gate)"); IMPL-CONTRACT CONFIRMED ("vocab=dispatcher C-map");
FROZEN-INTERFACE model.py ("CHANGE_CLASS_VOCAB: frozenset[str] = {docs, tests, cli,
data-backfill, annotation-loader, analysis, insights, pipeline, schema, ddl}"); and the
authoritative C-map in .claude/agents/scope-dispatcher.md ("docs 0 · tests 1 · cli 1 ·
data-backfill 2 · annotation-loader 2 · analysis/insights 2 · pipeline 3 · schema|ddl 4").

The splitter classifies by the SAME intake vocabulary the dispatcher emits (it consumes the
dispatcher's manifest), which is a DIFFERENT vocabulary from verify_gate's positive check-set
selector. This guard fails loudly if the two ever diverge. GREEN from freeze.

test->spec provenance noted per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

from genome.scope_split.model import CHANGE_CLASS_VOCAB

# The dispatcher C-map intake vocabulary (scope-dispatcher.md "Classify change_class" + the
# C sub-score table). This is the SPEC'd vocab, transcribed from the agent doc — NOT read from
# the implementation.
_DISPATCHER_C_MAP_VOCAB = frozenset(
    {
        "docs",
        "tests",
        "cli",
        "data-backfill",
        "annotation-loader",
        "analysis",
        "insights",
        "pipeline",
        "schema",
        "ddl",
    }
)


def test_change_class_vocab_equals_dispatcher_c_map() -> None:
    """from: SYNTHESIZED-PLAN §3/§5 + IMPL-CONTRACT CONFIRMED (vocab=dispatcher C-map) +
    FROZEN-INTERFACE model CHANGE_CLASS_VOCAB.

    scope_split's change-class vocabulary is EXACTLY the dispatcher intake C-map — the splitter
    partitions by the same labels the Stage-0 manifest carries.
    """
    assert CHANGE_CLASS_VOCAB == _DISPATCHER_C_MAP_VOCAB


def test_change_class_vocab_is_not_the_verify_gate_gate_vocab() -> None:
    """from: SYNTHESIZED-PLAN §3 ("NOT verify_gate gate vocab") + the fast_follow precedent
    (test_fast_follow_vocab_reconciliation.py: a separate vocab on a safety path, decoupled).

    The reconciliation target is the dispatcher C-map, NOT verify_gate's vocab. We assert the
    C-map members the dispatcher has but a gate-style positive-selector vocab would not (the
    intake-specific "annotation-loader" / "data-backfill" labels), so a future copy-paste of the
    wrong vocab fails here loudly rather than silently re-routing the partition.
    """
    assert "annotation-loader" in CHANGE_CLASS_VOCAB
    assert "data-backfill" in CHANGE_CLASS_VOCAB
    assert "pipeline" in CHANGE_CLASS_VOCAB
    # the irreversible-structural classes the dispatcher floors at Tier 2
    assert {"schema", "ddl"} <= CHANGE_CLASS_VOCAB

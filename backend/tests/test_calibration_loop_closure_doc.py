"""scope-dispatcher.md closes the loop — RUN compute-tier + CONSUME {tier,breakdown}.

from: §5 test #16 (test_calibration_loop_closure_doc.py) + §6:
  * ``.claude/agents/scope-dispatcher.md`` instructs RUNNING
    ``genome calibrate compute-tier --manifest -`` and CONSUMING the returned ``{tier,breakdown}``;
  * the prose C-map / B / P / t1 / t2 "compute exactly — do not improvise" block is DEMOTED to a
    non-authoritative "Reference" (no competing live-compute instruction);
  * (v2.1 amendment) the ``deep_T2`` selector and the ``+1`` probe-first / open-questions /
    human-bump escalation instructions SURVIVE — positively assert their presence.

Predicted-surprise guard (DISPATCHER HONORS RUN+CONSUME + bump/deep_T2 survival). The RUN /
CONSUME / DEMOTION assertions are written to the EXPECTED FINAL doc state and are RED until the
implementer's doc edit (T10) lands; the survival assertions are GREEN from freeze and guard
against the demotion accidentally deleting the v2.1 escalation rules.

test->spec provenance is stamped per test for the Stage-3 test-integrity lens.
"""

from __future__ import annotations

import re
from pathlib import Path


def _repo_root() -> Path:
    """Walk up from this file to the first directory holding ``CLAUDE.md`` (the repo root)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "CLAUDE.md").is_file():
            return parent
    msg = "could not locate repo root (no CLAUDE.md found walking up from the test file)"
    raise AssertionError(msg)


def _dispatcher_text() -> str:
    """The scope-dispatcher agent doc text."""
    return (_repo_root() / ".claude" / "agents" / "scope-dispatcher.md").read_text(encoding="utf-8")


def test_dispatcher_instructs_running_compute_tier() -> None:
    """from: §6 (instructs RUNNING ``genome calibrate compute-tier --manifest -``).

    The dispatcher RUNS the deterministic CLI as the single tier source of truth (Gate-1 D1)
    rather than improvising the formula in prose. RED until the doc edit lands.
    """
    assert "genome calibrate compute-tier --manifest -" in _dispatcher_text()


def test_dispatcher_consumes_the_returned_tier_breakdown() -> None:
    """from: §6 (CONSUMING the returned ``{tier,breakdown}``).

    The doc tells the dispatcher to USE the CLI's returned object (the tier + breakdown), not to
    re-derive it — the consume half of run-and-consume. RED until the doc edit lands.
    """
    text = _dispatcher_text()
    pattern = re.compile(
        r"compute-tier[\s\S]{0,400}(consume|returned|returns|\{\s*tier|tier['\" ,]+breakdown)",
        re.IGNORECASE,
    )
    assert pattern.search(text) is not None, (
        "dispatcher does not consume the returned tier/breakdown"
    )


def test_prose_formula_block_is_demoted_to_non_authoritative_reference() -> None:
    """from: §6 (the "compute exactly — do not improvise" block is DEMOTED to a Reference).

    The authoritative live-compute prose must no longer compete with the CLI: the
    "compute exactly — do not improvise" directive is gone and the block is reframed as a
    non-authoritative reference. RED until the doc edit lands.
    """
    text = _dispatcher_text()
    assert "compute exactly — do not improvise" not in text
    assert re.search(r"reference", text, re.IGNORECASE) is not None


def test_deep_t2_selector_survives_the_demotion() -> None:
    """from: §6 (v2.1: the deep_T2 selector SURVIVES) — GREEN, must stay.

    The review-depth selector is preserved through the demotion (positively asserted, so the
    edit cannot silently drop it).
    """
    assert "deep_T2" in _dispatcher_text()


def test_escalation_bumps_survive_the_demotion() -> None:
    """from: §6 (v2.1: the +1 probe-first / open-questions / human-bump escalation SURVIVES).

    All three conservative-bump triggers are preserved through the demotion — GREEN, must stay.
    """
    text = _dispatcher_text()
    assert "probe-first" in text
    assert "open_questions" in text
    assert "human-bump" in text

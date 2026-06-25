"""Cross-doc consistency — the POST-RECONCILE invariants of the Sub Project A doc edits.

Plan-blind spec source: synthesized-plan §4.8 + GATE-1 decision #3 (finding-034 is AMENDED,
not gutted — reconcile the operational docs to the evidence-gated path while PRESERVING the
human-GATE-2 fallback mentions) + R3 (the grep-guard asserts the POST-RECONCILE invariant, NOT
a blanket "string gone"), §5 test list item 7 (cross-doc grep guards: the evidence-gated
framing is present; no GATE-FILL/TODO survivor in the NEW docs), and §6 (cross-doc grep
empty / N/A self-gate).

These assert the POSITIVE invariant the reconcile must establish, deliberately NOT a brittle
"phrase X is gone" (which would false-fail on the legitimately-surviving human-fallback
mentions, per GATE-1 #3 / R3). They are RED until the fill round performs the doc edits:
- ``docs/runbooks/verification.md`` Purpose region currently has no "evidence-gated" framing.
- ``.claude/commands/scope-run.md`` currently has no ``/verify-and-merge`` reference.
- the NEW docs (``finding-037``, ``verify-and-merge.md``) do not exist yet, so the
  no-GATE-FILL-survivor check is RED (the new docs are required to exist AND be sentinel-free).

NB (verified during authoring): ``scope-run.md`` ALREADY contains the literal ``GATE-FILL`` in
ordinary prose ("get GATE-FILL / CHANGELOG nudges"). That is a legitimate mention, not a
survivor — so the survivor check is scoped to the NEW docs only (exactly as §5 specifies),
never to scope-run.md.
"""

from __future__ import annotations

from pathlib import Path


def _repo_root() -> Path:
    """Walk up from this file to the first directory holding ``CLAUDE.md`` (the repo root).

    Mirrors the ``genome.docs.cli`` repo-root anchor so the test is location-independent.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "CLAUDE.md").is_file():
            return parent
    msg = "could not locate repo root (no CLAUDE.md found walking up from the test file)"
    raise AssertionError(msg)


def _purpose_region(verification_md: str) -> str:
    """Slice the ``## Purpose`` region (heading → next ``## `` heading) of verification.md."""
    marker = "## Purpose"
    start = verification_md.find(marker)
    assert start != -1, "verification.md has no '## Purpose' section"
    rest = verification_md[start + len(marker) :]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


# ── verification.md Purpose region carries the evidence-gated framing ─────────


def test_verification_purpose_region_has_evidence_gated_framing() -> None:
    """from: plan §4.8 + GATE-1 #3 (reconcile verification.md to the evidence-gated path) +
    §5 item 7 (positive grep guard).

    After the reconcile, the Purpose region of ``docs/runbooks/verification.md`` records the
    Sub-A evidence-gated path (Claude runs the full verify + anchor captures, presents RAW
    evidence, takes a typed approval, merges). Asserted as the presence of the
    ``evidence-gated`` framing IN the Purpose region — the post-reconcile invariant, not a
    brittle "old phrase gone".
    """
    root = _repo_root()
    text = (root / "docs" / "runbooks" / "verification.md").read_text(encoding="utf-8")
    region = _purpose_region(text)
    assert "evidence-gated" in region, (
        "verification.md Purpose region is missing the post-reconcile 'evidence-gated' framing"
    )


def test_verification_preserves_human_gate2_fallback() -> None:
    """from: GATE-1 #3 / R3 (AMEND not gut — the human-GATE-2 fallback STILL EXISTS).

    The reconcile must PRESERVE the independent-human-run fallback, not delete it: the
    runbook still describes the operator's independent run (the surviving legitimate mention).
    This is the other half of the post-reconcile invariant — a blanket "independence string
    gone" would be wrong.
    """
    root = _repo_root()
    text = (root / "docs" / "runbooks" / "verification.md").read_text(encoding="utf-8")
    lowered = text.lower()
    assert "independent" in lowered, (
        "verification.md no longer mentions the operator's independent run — the human-GATE-2 "
        "fallback was gutted instead of amended (violates GATE-1 #3 'AMEND, do not gut')"
    )


# ── scope-run.md references the new /verify-and-merge skill ───────────────────


def test_scope_run_references_verify_and_merge_skill() -> None:
    """from: plan §4.8 (scope-run.md Stage 4-5 reconciled with the temporal split) +
    §5 item 7.

    After the reconcile, ``.claude/commands/scope-run.md`` references the new
    ``/verify-and-merge`` skill (the agentic merge path that governs FUTURE scopes), so a
    reader is pointed at the gate that replaces the manual Stage-4 "the team does not merge".
    """
    root = _repo_root()
    text = (root / ".claude" / "commands" / "scope-run.md").read_text(encoding="utf-8")
    assert "verify-and-merge" in text, (
        "scope-run.md does not reference the new /verify-and-merge skill"
    )


# ── No GATE-FILL / TODO survivor in the NEW docs ─────────────────────────────


def test_new_docs_have_no_gate_fill_or_todo_survivor() -> None:
    """from: plan §4.8 (finding-037 born durable) + §5 item 7 (no GATE-FILL/TODO survivor in
    the NEW docs) + §6 (cross-doc grep empty).

    The two NEW docs created this PR — ``docs/findings/finding-037-*.md`` and
    ``.claude/commands/verify-and-merge.md`` — must EXIST and carry no ``GATE-FILL`` /
    ``TODO`` placeholder survivor. (Scoped to the new docs only: pre-existing legitimate
    GATE-FILL mentions elsewhere, e.g. scope-run.md, are not in scope.)
    """
    root = _repo_root()

    # The new skill file.
    skill = root / ".claude" / "commands" / "verify-and-merge.md"
    assert skill.is_file(), f"new skill doc not created yet: {skill}"

    # The new finding (glob — exact slug is chosen at fill time).
    findings = sorted((root / "docs" / "findings").glob("finding-037-*.md"))
    assert findings, "new finding-037-*.md not created yet"

    new_docs = [skill, *findings]
    for doc in new_docs:
        body = doc.read_text(encoding="utf-8")
        assert "GATE-FILL" not in body, f"GATE-FILL survivor in new doc {doc.name}"
        assert "TODO" not in body, f"TODO survivor in new doc {doc.name}"


def test_finding_037_cross_links_into_the_amendment() -> None:
    """from: GATE-1 #3 (finding-034 amendment cross-links finding-037) + plan §4.7
    (finding-037 is the durable provenance anchor).

    The finding-034 amendment names finding-037 (the durable cross-link), so the agent-team
    charter points at the evidence-gated reconciling record. Asserted as: ``finding-034`` text
    references ``finding-037``. RED until the amendment + finding-037 land.
    """
    root = _repo_root()
    f034 = sorted((root / "docs" / "findings").glob("finding-034-*.md"))
    assert f034, "finding-034-*.md not found"
    text = f034[0].read_text(encoding="utf-8")
    assert "finding-037" in text, (
        "finding-034 amendment does not cross-link finding-037 (GATE-1 #3 reconciling note)"
    )

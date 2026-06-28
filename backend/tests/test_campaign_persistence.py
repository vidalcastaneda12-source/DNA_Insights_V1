"""Persistence tests for ``genome.campaign`` — the append-only JSONL ledger I/O.

Spec source: SYNTHESIZED-PLAN §4 step 3 (``persistence.py`` — append-only JSONL, I/O-out-of-core),
§5 (``test_campaign_persistence.py``), and Gate-1 refinement C (cancel is append-only; a cancelled
campaign reloads cleanly). Plan-blind: written from the contract, mirroring the verified
``genome.calibration.persistence`` precedent (``open("a")``, never-rewrite, empty-on-absent,
malformed-raises).

Covers: empty-on-absent load; append→load roundtrip; append never truncates; a malformed line
raises; a multi-record transition is written as one batch (no partial transition on disk); the
derived current view via ``load_campaign``; cancel append-only + clean reload; and the
path-traversal guard on the ``campaign_id`` (it is a filename stem).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from genome.campaign.model import CampaignStatus
from genome.campaign.persistence import append_records, load_campaign, load_history
from genome.campaign.state_machine import (
    advance_on_merge,
    cancel_campaign,
    seed_campaign,
    transition,
)
from genome.scope_split.model import SplitResult, SubScope

if TYPE_CHECKING:
    from pathlib import Path

    from genome.campaign.model import SubScopeState


def _linear_split(origin: str, n: int) -> SplitResult:
    """A non-atomic SplitResult of ``n`` sub-scopes in a linear dependency chain (s1←s2←…←sN)."""
    subs = tuple(
        SubScope(
            sub_scope_id=f"{origin}-s{i}",
            origin_scope=origin,
            change_class=("cli",),
            est_imports_touched=2,
            applicable_anchors=(),
            est_risk_tier=1,
            depends_on=() if i == 1 else (f"{origin}-s{i - 1}",),
            rationale=f"cluster {i}",
        )
        for i in range(1, n + 1)
    )
    return SplitResult(
        atomic=False,
        reason="clean cut",
        sub_scopes=subs,
        order=tuple(s.sub_scope_id for s in subs),
        cut_quality=None,
    )


def _drive_to_implementing(history: list[SubScopeState], sub_id: str) -> None:
    """Append legal records to bring ``sub_id`` from pending to implementing (Gate 1 passed)."""
    history.append(transition(history, sub_id, CampaignStatus.READY))
    history.append(transition(history, sub_id, CampaignStatus.PLANNING))
    history.append(transition(history, sub_id, CampaignStatus.IMPLEMENTING, external_event=True))


def test_load_history_is_empty_on_absent(tmp_path: Path) -> None:
    """from: §4 step 3 ('empty-on-absent — first run is not an error')."""
    assert load_history("PR-X", campaign_dir=tmp_path) == []


def test_append_then_load_is_a_faithful_roundtrip(tmp_path: Path) -> None:
    """from: §4 step 3 (append-only JSONL roundtrip)."""
    records = seed_campaign(_linear_split("PR-X", 2), "PR-X")
    append_records("PR-X", records, campaign_dir=tmp_path)
    loaded = load_history("PR-X", campaign_dir=tmp_path)
    assert [r.to_json() for r in loaded] == [r.to_json() for r in records]


def test_append_never_truncates_the_ledger(tmp_path: Path) -> None:
    """from: §5 ('append never truncates — two append_records calls, both batches present')."""
    seed = seed_campaign(_linear_split("PR-X", 1), "PR-X")
    append_records("PR-X", seed, campaign_dir=tmp_path)
    history = list(seed)
    append_records(
        "PR-X", [transition(history, "PR-X-s1", CampaignStatus.READY)], campaign_dir=tmp_path
    )

    loaded = load_history("PR-X", campaign_dir=tmp_path)
    assert [r.status for r in loaded] == [CampaignStatus.PENDING, CampaignStatus.READY]


def test_a_malformed_line_raises_rather_than_dropping_history(tmp_path: Path) -> None:
    """from: §4 step 3 ('a malformed line RAISES rather than silently dropping history')."""
    (tmp_path / "PR-X.jsonl").write_text("{ not valid json\n", encoding="utf-8")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        load_history("PR-X", campaign_dir=tmp_path)


def test_multi_record_transition_is_written_as_one_batch(tmp_path: Path) -> None:
    """from: §5 ('a multi-record transition writes its records in one atomic write').

    ``advance_on_merge`` produces the merged record AND the readied dependent; ``append_records``
    writes them together, so the ledger never shows the merge without its readied dependent.
    """
    history = list(seed_campaign(_linear_split("PR-X", 2), "PR-X"))
    append_records("PR-X", history, campaign_dir=tmp_path)
    _drive_to_implementing(history, "PR-X-s1")
    append_records("PR-X", history[2:], campaign_dir=tmp_path)

    batch = advance_on_merge(history, "PR-X-s1")
    assert len(batch) == 2  # merged + readied dependent
    append_records("PR-X", batch, campaign_dir=tmp_path)

    loaded = load_history("PR-X", campaign_dir=tmp_path)
    pairs = {(r.sub_scope_id, r.status) for r in loaded}
    assert ("PR-X-s1", CampaignStatus.MERGED) in pairs
    assert ("PR-X-s2", CampaignStatus.READY) in pairs
    # one atomic write: the batch is the final two CONTIGUOUS lines on disk, in order (a two-write
    # implementation could interleave them — so pin contiguity, not just presence-after-load).
    raw = (tmp_path / "PR-X.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in raw[-2:]] == [r.to_json() for r in batch]


def test_load_campaign_reduces_to_the_current_view(tmp_path: Path) -> None:
    """from: §4 step 3 ('load_campaign = reduce_current(load_history(...))')."""
    records = seed_campaign(_linear_split("PR-X", 2), "PR-X")
    append_records("PR-X", records, campaign_dir=tmp_path)
    history = list(records)
    append_records(
        "PR-X", [transition(history, "PR-X-s1", CampaignStatus.READY)], campaign_dir=tmp_path
    )

    state = load_campaign("PR-X", campaign_dir=tmp_path)
    s1 = state.by_id("PR-X-s1")
    assert s1 is not None
    assert s1.status is CampaignStatus.READY
    assert len(state.sub_scopes) == 2


def test_cancel_is_append_only_and_the_campaign_reloads_clean(tmp_path: Path) -> None:
    """from: refinement C ('cancel appends terminal records … reloads cleanly').

    Cancel ejects every active non-terminal sub-scope via the same insert-then-flip — it never
    mutates or truncates the file — and a cancelled campaign reloads to a clean all-terminal state.
    """
    seed = seed_campaign(_linear_split("PR-X", 3), "PR-X")
    append_records("PR-X", seed, campaign_dir=tmp_path)

    history = load_history("PR-X", campaign_dir=tmp_path)
    ejections = cancel_campaign(history)
    append_records("PR-X", ejections, campaign_dir=tmp_path)

    reloaded = load_history("PR-X", campaign_dir=tmp_path)
    state = load_campaign("PR-X", campaign_dir=tmp_path)
    assert state.is_done()
    assert all(s.status is CampaignStatus.EJECTED for s in state.sub_scopes)
    assert all("cancel" in s.note.lower() for s in state.sub_scopes)
    # append-only: the 3 original PENDING seed records survive untouched alongside the 3 ejections.
    assert len(reloaded) == 6
    assert sum(1 for r in reloaded if r.status is CampaignStatus.PENDING) == 3


@pytest.mark.parametrize("bad_id", ["../etc/passwd", "a/b", "-x", "", "a:b"])
def test_a_path_unsafe_campaign_id_is_rejected(tmp_path: Path, bad_id: str) -> None:
    """from: §3 (the campaign_id is a filename stem — guard it against path traversal).

    Mirrors scope_split's git-pathspec safety guard: an id that is empty, a path separator, a
    leading dash, or otherwise unsafe to use as a file stem raises rather than reaching the disk.
    """
    with pytest.raises(ValueError, match="campaign_id"):
        load_history(bad_id, campaign_dir=tmp_path)


def test_a_campaign_resumes_forward_from_disk_across_sessions(tmp_path: Path) -> None:
    """from: ptest-4 (review) + design #6 (multi-session resumability) — advance a campaign loaded
    PURELY from disk (no in-memory carryover): the reload→advance resume seam, not just
    reload→inspect."""
    # session 1: seed + persist, then drop all in-memory state.
    append_records("PR-X", seed_campaign(_linear_split("PR-X", 2), "PR-X"), campaign_dir=tmp_path)

    # session 2: reload from disk ALONE and drive the head sub-scope through its gates to MERGED.
    history = load_history("PR-X", campaign_dir=tmp_path)
    base = len(history)
    _drive_to_implementing(history, "PR-X-s1")
    append_records("PR-X", history[base:], campaign_dir=tmp_path)
    batch = advance_on_merge(history, "PR-X-s1")
    append_records("PR-X", batch, campaign_dir=tmp_path)

    # session 3: reload and assert the resumed current view.
    state = load_campaign("PR-X", campaign_dir=tmp_path)
    s1 = state.by_id("PR-X-s1")
    s2 = state.by_id("PR-X-s2")
    assert s1 is not None
    assert s1.status is CampaignStatus.MERGED
    assert s2 is not None
    assert s2.status is CampaignStatus.READY  # dependent teed up post-merge


@pytest.mark.parametrize(
    "payload",
    [
        {
            "record_seq": "not-an-int",  # wrong type: str where int expected
            "sub_scope_id": "PR-X-s1",
            "status": "pending",
            "origin_scope": "PR-X",
            "manifest_snapshot": {"k": 1},
        },
        {
            "record_seq": 0,
            "sub_scope_id": "PR-X-s1",
            "status": 99,  # wrong type: int where a status string is expected
            "origin_scope": "PR-X",
            "manifest_snapshot": {"k": 1},
        },
    ],
)
def test_a_valid_json_line_with_a_wrong_typed_field_raises(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    """from: ptest-5 (review) — a syntactically-valid ledger line whose field is the wrong TYPE
    (not merely a JSON parse error) is rejected by the fail-closed narrowing, never silently
    coerced — so a corrupted ledger cannot reduce to a plausible-but-wrong current view."""
    (tmp_path / "PR-X.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises((TypeError, ValueError)):
        load_history("PR-X", campaign_dir=tmp_path)

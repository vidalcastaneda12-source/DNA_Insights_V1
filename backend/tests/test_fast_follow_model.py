"""Model-level units — Candidate JSON round-trip + seen_key format.

Spec source: review ptest-10 (seen_key concrete format stability) + ptest-11 (from_json/to_json
unit round-trip). These pin behaviour the CLI tests exercise only transitively.
"""

from __future__ import annotations

from genome.fast_follow.model import Candidate


def _candidate() -> Candidate:
    return Candidate(
        candidate_id="cand-1",
        source="repo-sweep",
        kind="doc-nit",
        change_class=frozenset({"core"}),
        blast_radius=2,
        applicable_anchors=0,
        tier="tier-0",
        touched_paths=("docs/notes/a.md", "docs/notes/b.md"),
        is_stale=False,
    )


def test_candidate_from_json_to_json_round_trips() -> None:
    """from: review ptest-11 — a Candidate survives to_json() -> from_json() field-for-field."""
    original = _candidate()
    assert Candidate.from_json(original.to_json()) == original


def test_candidate_round_trip_preserves_none_and_empty() -> None:
    """from: review ptest-11 — None/undecidable fields and empty collections survive the seam."""
    c = Candidate(
        candidate_id="c2",
        source="finding-oos",
        kind="k",
        change_class=frozenset(),
        blast_radius=None,
        applicable_anchors=None,
        tier=None,
        touched_paths=(),
        is_stale=True,
    )
    assert Candidate.from_json(c.to_json()) == c


def test_seen_key_is_source_colon_candidate_id() -> None:
    """from: review ptest-10 — seen_key's concrete format is 'source:candidate_id' (stable)."""
    assert _candidate().seen_key() == "repo-sweep:cand-1"

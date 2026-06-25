"""Seen-set persistence — ``save_seen`` / ``load_seen`` cross-invocation round-trip.

Plan-blind spec source: synthesized-plan R4 ("Seen-set MUST persist across invocations …
read at scan, appended at close … tested for round-trip + 'a handled key is excluded on the
next scan'"), A3 (the load/save filesystem I/O lives in ``fast_follow/persistence.py``;
DEFAULT location is ``data/fast_follow/seen.json``), ESC-3 (seen-set persists at
``data/fast_follow/seen.json``), and the FROZEN INTERFACE CONTRACT (``DEFAULT_SEEN_PATH``;
``load_seen(path) -> set[str]``; ``save_seen(keys, path) -> None``).

Every test routes I/O through ``tmp_path`` — never the real ``data/`` directory — so the suite
never reads or writes operational runtime state. The expected behaviour (a key saved in one
"run" is present on the next ``load_seen``) comes from R4; nothing is reverse-engineered from
the stubbed bodies (``raise NotImplementedError`` now — RED is correct).

Pre-mortem coverage (R4 surprise: "an in-memory seen-set re-surfaces handled items every
run"): the cross-invocation round-trip is the guard test proving a handled key persisted in
one process is seen by the next.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from genome.fast_follow.persistence import (
    DEFAULT_SEEN_PATH,
    load_seen,
    save_seen,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_save_then_load_round_trips_a_set_of_keys(tmp_path: Path) -> None:
    """from: R4 (round-trip) + frozen ``save_seen`` / ``load_seen`` signatures.

    A set of seen_keys saved to a tmp_path JSON loads back as the identical set — the on-disk
    seam preserves the handled-key set exactly.
    """
    path = tmp_path / "fast_follow" / "seen.json"
    keys = {"key-a", "key-b", "key-c"}
    save_seen(keys, path)
    loaded = load_seen(path)
    assert loaded == keys


def test_handled_key_persists_across_invocations(tmp_path: Path) -> None:
    """from: R4 ("a handled key is excluded on the next scan" — cross-invocation dedup).

    Simulate two separate ``/fast-follow`` runs (separate processes share only the file):
    run 1 saves the seen-set including a handled key; run 2 loads it and the handled key is
    present — proving the dedup survives a fresh process, not just an in-memory set.
    """
    path = tmp_path / "fast_follow" / "seen.json"
    # Run 1: handle "cand-handled" plus an earlier key.
    save_seen({"cand-earlier", "cand-handled"}, path)
    # Run 2: a fresh load picks up the handled key.
    next_run_seen = load_seen(path)
    assert "cand-handled" in next_run_seen
    assert "cand-earlier" in next_run_seen


def test_load_seen_missing_file_is_empty_set(tmp_path: Path) -> None:
    """from: R4 (read at scan — a first-ever run has no file yet).

    The very first ``/fast-follow`` run has no seen.json yet; ``load_seen`` on a missing path
    yields an empty set (a clean start), not an error.
    """
    path = tmp_path / "fast_follow" / "does-not-exist.json"
    assert load_seen(path) == set()


def test_default_seen_path_is_under_data_runtime_home() -> None:
    """from: A3 / ESC-3 (DEFAULT location is ``data/fast_follow/seen.json``, the gitignored
    runtime-state home) + frozen ``DEFAULT_SEEN_PATH``.

    The default path points under ``data/`` (the conventional mutable-runtime-state home per
    CLAUDE.md), never under ``archive/`` (snapshot territory) or a tracked source tree.
    """
    parts = DEFAULT_SEEN_PATH.parts
    assert "data" in parts, f"DEFAULT_SEEN_PATH not under data/: {DEFAULT_SEEN_PATH}"
    assert DEFAULT_SEEN_PATH.name == "seen.json"


def test_load_seen_malformed_json_raises(tmp_path: Path) -> None:
    """from: review ptest-3 — a non-array seen.json must raise, never silently return empty.

    A silent empty return would re-surface every handled candidate (defeating cross-invocation
    dedup), so the corrupt-file path must fail loud (the safe direction).
    """
    bad = tmp_path / "seen.json"
    bad.write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(TypeError):
        load_seen(bad)

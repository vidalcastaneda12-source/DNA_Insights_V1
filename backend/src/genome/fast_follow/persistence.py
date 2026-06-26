"""Seen-set load/save for the fast-follow drain loop (``finding-038``; plan R4 / A3).

The cross-invocation dedup state. Each ``/fast-follow`` run is a separate post-merge process
(spec Decision-1), so an in-memory seen-set re-surfaces handled items every run; the keys of
every handled (drained / ejected / discarded) candidate are persisted to a gitignored
``data/fast_follow/seen.json`` and read back at the next scan. The ``seen_key`` *derivation*
is a pure function on :class:`~genome.fast_follow.model.Candidate` and stays in
:mod:`genome.fast_follow.model`; only the **filesystem I/O** lives here (plan A3 — mirrors how
verify_gate keeps I/O in cli.py and model.py stays I/O-free).

``data/`` is the gitignored runtime-state home (CLAUDE.md), the conventional fit for mutable
operational state; ``archive/`` is snapshot territory. **No** :mod:`genome.db` import.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

#: The gitignored default seen-set location (plan A3). ``data/`` is the runtime-state home.
DEFAULT_SEEN_PATH: Path = Path("data/fast_follow/seen.json")


def load_seen(path: Path = DEFAULT_SEEN_PATH) -> set[str]:
    """Read the persisted seen-set of handled ``seen_key`` values (plan R4).

    Returns an empty set when the file is absent (first run) — a missing seen-set is not an
    error. A malformed file raises rather than silently returning empty (which would
    re-surface every handled candidate).
    """
    if not path.exists():
        logger.info("fast_follow.seen.absent", path=str(path))
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        msg = f"seen-set {path} must be a JSON array of keys, got {type(raw).__name__}"
        raise TypeError(msg)
    keys = {str(key) for key in raw}
    logger.info("fast_follow.seen.loaded", path=str(path), count=len(keys))
    return keys


def save_seen(keys: set[str], path: Path = DEFAULT_SEEN_PATH) -> None:
    """Persist the seen-set of handled ``seen_key`` values to ``path`` (plan R4).

    Creates the parent directory if needed and writes the union of handled keys, so the next
    scan excludes every drained / ejected / discarded candidate (the self-spawning-nit
    termination guard).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(keys), indent=2), encoding="utf-8")
    logger.info("fast_follow.seen.saved", path=str(path), count=len(keys))

"""Append-only campaign-ledger I/O (``finding-041``; B2 Phase 2).

The campaign's persisted state — **no** campaign tables in either DB. Each campaign is one
append-only JSONL file under the gitignored ``data/campaign/`` (the ROADMAP managed block is the
git-tracked human-readable reflection). This mirrors the verified
:mod:`genome.calibration.persistence` precedent: ``open("a")`` one line per record, **never**
rewrite or truncate; loads are **empty-on-absent** (a missing campaign is not an error — first
run); a **malformed** line RAISES rather than silently dropping history (which would corrupt the
supersession reduction).

Paths are **hard-coded** ``Path('data/campaign')`` — this module calls **no** ``get_settings`` and
imports neither :mod:`genome.db` nor :mod:`genome.config`, so the campaign core stays importable on
a fresh checkout (the no-DB / no-settings guarantee, carried by the clean-subprocess test). The
``campaign_id`` is a **file stem**, so it is validated against a safe charset (the path-traversal
guard, mirroring scope_split's git-pathspec name guard) before it ever reaches the filesystem.

A multi-record transition (e.g. ``advance_on_merge`` = the merged record + its readied dependents,
or ``cancel_campaign``) is written in **one** ``write()`` call so a torn write can never leave the
ledger showing a partial transition (locked decision #7 — readers never see a torn state).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from genome.campaign.model import SubScopeState
from genome.campaign.state_machine import reduce_current

if TYPE_CHECKING:
    from collections.abc import Sequence

    from genome.campaign.model import CampaignState

logger = structlog.get_logger(__name__)

#: The gitignored per-campaign ledger home (CLAUDE.md: ``data/`` is the runtime-state home). One
#: append-only ``<campaign_id>.jsonl`` per campaign; the relative path assumes a repo-root cwd, the
#: same cwd assumption :mod:`genome.calibration.persistence` / ``genome.fast_follow`` make.
DEFAULT_CAMPAIGN_DIR: Path = Path("data/campaign")

#: A ``campaign_id`` is used as a file stem, so it must match this safe charset before it reaches
#: the filesystem: letters, digits, ``_``, ``.``, ``-`` only, no leading ``-`` (option-injection
#: shape) and no path separator. Combined with the explicit ``..`` reject, this is the
#: path-traversal guard (mirrors scope_split's ``_FOOTPRINT_NAME_RE``).
_CAMPAIGN_ID_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")


def _validate_campaign_id(campaign_id: str) -> None:
    """Reject a ``campaign_id`` that is unsafe to use as a file stem (the path-traversal guard).

    An empty id, a path separator, a leading ``-``, a ``:``/whitespace, or a ``..`` segment raises
    :class:`ValueError` so an unvalidated id never becomes a filesystem path.
    """
    if not _CAMPAIGN_ID_RE.match(campaign_id) or ".." in campaign_id:
        msg = (
            f"campaign_id {campaign_id!r} is not a safe file stem (allowed charset "
            "[A-Za-z0-9_.-], no leading '-', no '..' segment or path separator)"
        )
        raise ValueError(msg)


def _campaign_path(campaign_id: str, campaign_dir: Path) -> Path:
    """The append-only ledger path for ``campaign_id`` under ``campaign_dir`` (validated stem)."""
    return campaign_dir / f"{campaign_id}.jsonl"


def load_history(
    campaign_id: str, *, campaign_dir: Path = DEFAULT_CAMPAIGN_DIR
) -> list[SubScopeState]:
    """Read a campaign's full append-only ledger into a list of records (oldest-first).

    Returns an empty list when the file is absent (first run — not an error). One JSON object per
    line; a malformed line raises (via :meth:`~genome.campaign.model.SubScopeState.from_json` or
    :func:`json.loads`) rather than silently dropping history.
    """
    _validate_campaign_id(campaign_id)
    path = _campaign_path(campaign_id, campaign_dir)
    if not path.exists():
        logger.info("campaign.history.absent", campaign_id=campaign_id, path=str(path))
        return []
    records = [
        SubScopeState.from_json(json.loads(line))
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip())
    ]
    logger.info(
        "campaign.history.loaded", campaign_id=campaign_id, count=len(records), path=str(path)
    )
    return records


def append_records(
    campaign_id: str,
    records: Sequence[SubScopeState],
    *,
    campaign_dir: Path = DEFAULT_CAMPAIGN_DIR,
) -> None:
    """Append a transition's records to the campaign ledger as one atomic write (locked #7).

    Creates the parent directory if needed, then writes **all** ``records`` of one transition in a
    single ``write()`` call (never rewriting or truncating), so a torn write can never leave the
    ledger showing a partial transition. An empty ``records`` is a no-op (after validating the id).
    """
    _validate_campaign_id(campaign_id)
    if not records:
        return
    path = _campaign_path(campaign_id, campaign_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(record.to_json()) + "\n" for record in records)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(payload)
    logger.info(
        "campaign.records.appended", campaign_id=campaign_id, count=len(records), path=str(path)
    )


def load_campaign(campaign_id: str, *, campaign_dir: Path = DEFAULT_CAMPAIGN_DIR) -> CampaignState:
    """Load a campaign's current view — the latest-active record per sub-scope (locked #7).

    Reduces the full append-only ledger to its derived current view; an absent campaign reduces to
    an empty :class:`~genome.campaign.model.CampaignState` (no sub-scopes).
    """
    history = load_history(campaign_id, campaign_dir=campaign_dir)
    return reduce_current(history, campaign_id=campaign_id)

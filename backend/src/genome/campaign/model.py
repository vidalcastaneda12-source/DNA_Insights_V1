"""Frozen vocabulary + data model for the campaign orchestrator (``finding-041``; B2 Phase 2).

This module is the source of truth for the campaign state machine's closed vocabularies
(:class:`CampaignStatus`, :class:`RevalidationDecision`), the legal-transition + human-gate maps
(:data:`LEGAL_TRANSITIONS`, :data:`GATE_CROSSINGS`, :data:`TERMINAL_STATUSES`), and the frozen
records the append-only ledger is built from (:class:`SubScopeState`, :class:`CampaignState`).

The campaign applies **locked decision #7 (supersession over update)** to its own runtime state:
every status transition is an INSERT of a new immutable :class:`SubScopeState` that supersedes the
prior one (``finding-039`` LIFECYCLE: "Phase 2 ``genome.campaign`` is an insert-then-flip
supersession, never an in-place edit"). The *current view* is **derived** — the latest
``record_seq`` per ``sub_scope_id`` — not a stored ``is_active`` flag, so prior bytes are never
rewritten. :class:`CampaignState` carries that derived view and rejects any torn >1-active state.

It is **import-side-effect-free** and imports **no** :mod:`genome.db` and **no**
:mod:`genome.config` (the DB-free / no-settings guarantee, carried by the package-local
``test_campaign_no_db_import.py`` clean-subprocess test). It reuses
:data:`genome.scope_split.model.MAX_RESPLIT_DEPTH` (itself DB-free) rather than redefining the cap.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Mapping


# ── Closed status + decision vocabularies ────────────────────────────────────


class CampaignStatus(enum.Enum):
    """The lifecycle status of one sub-scope within a campaign (design §2 Part 2).

    The non-terminal path is ``PENDING → READY → PLANNING → IMPLEMENTING → MERGED`` with two
    human-gate crossings (:data:`GATE_CROSSINGS`): ``PLANNING → IMPLEMENTING`` is **Gate 1** (plan
    approval) and ``IMPLEMENTING → MERGED`` is **Gate 2** (``/verify-and-merge``). :attr:`MOOT`
    (re-validated away) and :attr:`EJECTED` (re-split past the cap, or cancelled — always with a
    human-readable note) are the terminal off-ramps. The campaign sequences and tees up, but it
    crosses neither gate on its own.
    """

    PENDING = "pending"
    READY = "ready"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    MERGED = "merged"
    MOOT = "moot"
    EJECTED = "ejected"


class RevalidationDecision(enum.Enum):
    """The verdict of re-dispatching a sub-scope immediately before it runs (design §2 Part 2).

    :attr:`STILL_NEEDED` → run it (``READY → PLANNING``). :attr:`MOOT` → skip it (``→ MOOT``).
    :attr:`CHANGED` → re-propose with a fresh ``manifest_snapshot`` (stays ``READY``).
    :attr:`GROWN` → re-split into children (capped at
    :data:`~genome.scope_split.model.MAX_RESPLIT_DEPTH`, then eject + escalate).
    """

    STILL_NEEDED = "still_needed"
    MOOT = "moot"
    CHANGED = "changed"
    GROWN = "grown"


#: The terminal statuses — a sub-scope here is done; it has no outgoing transitions. ``MERGED`` and
#: ``MOOT`` are *resolved* (they satisfy a downstream dependency); ``EJECTED`` is *escalated* (it
#: does **not** satisfy a dependency — an ejected dep leaves its dependents blocked, by design).
TERMINAL_STATUSES: frozenset[CampaignStatus] = frozenset(
    {CampaignStatus.MERGED, CampaignStatus.MOOT, CampaignStatus.EJECTED},
)

#: The legal status transitions (current status → allowed next statuses). Status-changing only —
#: a content-only supersession (the ``CHANGED`` re-validation keeps ``READY``) does not appear here
#: and is built directly by the state machine. Terminal statuses map to the empty set. The two
#: human-gate edges are present here AND in :data:`GATE_CROSSINGS` (which gates them on an external
#: event); every other edge is the campaign's own autonomous sequencing / off-ramp.
LEGAL_TRANSITIONS: Mapping[CampaignStatus, frozenset[CampaignStatus]] = {
    CampaignStatus.PENDING: frozenset(
        {CampaignStatus.READY, CampaignStatus.MOOT, CampaignStatus.EJECTED},
    ),
    CampaignStatus.READY: frozenset(
        {CampaignStatus.PLANNING, CampaignStatus.MOOT, CampaignStatus.EJECTED},
    ),
    CampaignStatus.PLANNING: frozenset(
        {CampaignStatus.IMPLEMENTING, CampaignStatus.MOOT, CampaignStatus.EJECTED},
    ),
    CampaignStatus.IMPLEMENTING: frozenset(
        {CampaignStatus.MERGED, CampaignStatus.EJECTED},
    ),
    CampaignStatus.MERGED: frozenset(),
    CampaignStatus.MOOT: frozenset(),
    CampaignStatus.EJECTED: frozenset(),
}

#: The two human-gate crossings — each is reachable ONLY via an external event (the human's plan
#: approval at Gate 1 and ``/verify-and-merge`` at Gate 2); the campaign can never produce one of
#: these transitions autonomously (Gate-1 refinement A — symmetric gate-guard). ``PLANNING →
#: IMPLEMENTING`` is Gate 1; ``IMPLEMENTING → MERGED`` is Gate 2.
GATE_CROSSINGS: frozenset[tuple[CampaignStatus, CampaignStatus]] = frozenset(
    {
        (CampaignStatus.PLANNING, CampaignStatus.IMPLEMENTING),
        (CampaignStatus.IMPLEMENTING, CampaignStatus.MERGED),
    },
)


# ── JSON serialization shape (the typed to_json() output contract) ───────────


class SubScopeStateJSON(TypedDict):
    """The :meth:`SubScopeState.to_json` shape — one append-only ledger line."""

    record_seq: int
    sub_scope_id: str
    status: str
    origin_scope: str
    manifest_snapshot: dict[str, object]
    depends_on: list[str]
    supersedes: int | None
    resplit_depth: int
    note: str


# ── Frozen records ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SubScopeState:
    """One immutable record in a campaign's append-only ledger (design §2 Part 2; locked #7).

    A status transition appends a NEW ``SubScopeState`` (``record_seq`` = the prior max + 1,
    :attr:`supersedes` = the prior active record's seq); the prior record is never edited. The
    *active* record for a ``sub_scope_id`` is the one with the highest ``record_seq`` — derived at
    read time, never a stored flag. :attr:`origin_scope` + :attr:`manifest_snapshot` are the
    locked-#8 provenance every record must carry (fail-closed at construction).
    """

    record_seq: int
    """Monotonic append index across the whole campaign ledger (the supersession key)."""
    sub_scope_id: str
    """The placeholder sub-scope id this record is for (``<origin>-sN``; finding-039)."""
    status: CampaignStatus
    """The lifecycle status this record records."""
    origin_scope: str
    """The parent scope this sub-scope was carved from — the campaign identity (locked #8)."""
    manifest_snapshot: Mapping[str, object]
    """The sub-scope mini-manifest snapshot at this transition (provenance #8; never empty)."""
    depends_on: tuple[str, ...] = ()
    """Sub-scope ids that must reach a resolved (merged/moot) status before this one is ready."""
    supersedes: int | None = None
    """The ``record_seq`` this record supersedes, or ``None`` for the initial (seed) record."""
    resplit_depth: int = 0
    """Re-split recursion depth (0 = seeded; +1 per GROWN carve, capped at MAX_RESPLIT_DEPTH)."""
    note: str = ""
    """Reason for this transition — carries the eject/cancel escalation note (refinement B)."""

    def __post_init__(self) -> None:
        """Fail-closed #8: reject a record with no attributability (empty provenance).

        A record missing its ``origin_scope`` or ``manifest_snapshot`` is unattributable and is
        rejected at construction, so every persisted transition names what it acted on and why
        (locked decision #8 — provenance everywhere).
        """
        if not self.origin_scope:
            msg = "SubScopeState requires a non-empty origin_scope (locked decision #8 provenance)"
            raise ValueError(msg)
        if not self.manifest_snapshot:
            msg = (
                "SubScopeState requires a non-empty manifest_snapshot "
                "(locked decision #8 provenance)"
            )
            raise ValueError(msg)

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> SubScopeState:
        """Build a :class:`SubScopeState` from a parsed ledger line, fail-closed on bad provenance.

        Explicit per-field narrowing with no ``Any`` leak. ``origin_scope`` and
        ``manifest_snapshot`` are required (a missing one raises :class:`ValueError` rather than
        reconstructing an unattributable record); the optional fields default. The accepted shape
        is exactly what :meth:`to_json` emits.
        """
        for required in (
            "record_seq",
            "sub_scope_id",
            "status",
            "origin_scope",
            "manifest_snapshot",
        ):
            if required not in data:
                msg = f"SubScopeState.from_json: record is missing required field {required!r}"
                raise ValueError(msg)
        return cls(
            record_seq=_as_int(data.get("record_seq")),
            sub_scope_id=_as_str(data.get("sub_scope_id")),
            status=_as_status(data.get("status")),
            origin_scope=_as_str(data.get("origin_scope")),
            manifest_snapshot=_as_mapping(data.get("manifest_snapshot")),
            depends_on=tuple(_as_str_list(data.get("depends_on"))),
            supersedes=_as_opt_int(data.get("supersedes")),
            resplit_depth=_as_int(data.get("resplit_depth", 0)),
            note=_as_str(data.get("note", "")),
        )

    def to_json(self) -> SubScopeStateJSON:
        """Serialize to a JSON-ready mapping — one ledger line (the inverse of from_json)."""
        return {
            "record_seq": self.record_seq,
            "sub_scope_id": self.sub_scope_id,
            "status": self.status.value,
            "origin_scope": self.origin_scope,
            "manifest_snapshot": dict(self.manifest_snapshot),
            "depends_on": list(self.depends_on),
            "supersedes": self.supersedes,
            "resplit_depth": self.resplit_depth,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class CampaignState:
    """The derived current view of a campaign — exactly the latest-active record per sub-scope.

    Reduced from the append-only ledger by :func:`genome.campaign.state_machine.reduce_current`.
    The structural invariant (enforced at construction) is the no-torn-state guarantee of locked
    decision #7: a campaign can never present two active records for one ``sub_scope_id``.
    """

    campaign_id: str
    """The campaign identity — the parent ``origin_scope`` (and the persisted-file stem)."""
    sub_scopes: tuple[SubScopeState, ...] = field(default_factory=tuple)
    """The active record per sub-scope, in seed (topological) order."""

    def __post_init__(self) -> None:
        """Reject a torn current view: more than one active record for a single sub-scope (#7)."""
        seen: set[str] = set()
        for sub in self.sub_scopes:
            if sub.sub_scope_id in seen:
                msg = (
                    f"CampaignState has >1 active record for sub_scope {sub.sub_scope_id!r} — "
                    "a torn supersession view (locked decision #7 forbids this)"
                )
                raise ValueError(msg)
            seen.add(sub.sub_scope_id)

    def by_id(self, sub_scope_id: str) -> SubScopeState | None:
        """Return the active record for ``sub_scope_id``, or ``None`` if the campaign has none."""
        for sub in self.sub_scopes:
            if sub.sub_scope_id == sub_scope_id:
                return sub
        return None

    def is_done(self) -> bool:
        """``True`` when every sub-scope has reached a terminal status (merged / moot / ejected)."""
        return all(sub.status in TERMINAL_STATUSES for sub in self.sub_scopes)


# ── Strict JSON narrowing (no ``Any`` leak across the serialization seam) ─────


def _as_str(value: object) -> str:
    """Narrow a JSON scalar to ``str`` or raise — the seam never silently coerces."""
    if isinstance(value, str):
        return value
    msg = f"expected a string, got {type(value).__name__}: {value!r}"
    raise TypeError(msg)


def _as_int(value: object) -> int:
    """Narrow a JSON scalar to ``int`` or raise (rejects ``bool`` masquerading as int)."""
    if isinstance(value, bool):
        msg = f"expected an integer, got bool: {value!r}"
        raise TypeError(msg)
    if isinstance(value, int):
        return value
    msg = f"expected an integer, got {type(value).__name__}: {value!r}"
    raise TypeError(msg)


def _as_opt_int(value: object) -> int | None:
    """Narrow to ``int | None`` (JSON ``null`` → ``None``); rejects ``bool`` as int."""
    if value is None:
        return None
    return _as_int(value)


def _as_obj_list(value: object) -> list[object]:
    """Narrow a JSON value to ``list[object]`` (``None`` → empty list) or raise."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    msg = f"expected a list, got {type(value).__name__}: {value!r}"
    raise TypeError(msg)


def _as_str_list(value: object) -> list[str]:
    """Narrow a JSON value to ``list[str]`` (``None`` → empty list) or raise."""
    return [_as_str(item) for item in _as_obj_list(value)]


def _as_mapping(value: object) -> Mapping[str, object]:
    """Narrow a JSON value to a ``Mapping[str, object]`` (``None`` → empty) or raise."""
    if value is None:
        return {}
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, val in value.items():
            out[_as_str(key)] = val
        return out
    msg = f"expected an object, got {type(value).__name__}: {value!r}"
    raise TypeError(msg)


def _as_status(value: object) -> CampaignStatus:
    """Narrow a JSON string to a :class:`CampaignStatus` member or raise (closed vocabulary)."""
    raw = _as_str(value)
    try:
        return CampaignStatus(raw)
    except ValueError as exc:
        valid = sorted(s.value for s in CampaignStatus)
        msg = f"unknown campaign status {raw!r}; expected one of {valid!r}"
        raise ValueError(msg) from exc

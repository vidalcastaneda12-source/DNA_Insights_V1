"""Frozen vocabulary + data model for the fast-follow drain loop (``finding-038``).

This module is the source of truth for the loop's closed vocabularies
(:class:`Classification`, :data:`TIER_VOCAB`, :data:`GUARD_CLASS_VOCAB`,
:data:`GUARDED_CLASSES`), the tunable bound constants, and the frozen records the
fail-closed classifier reduces (:class:`Candidate`, :class:`Triage`, :class:`TriagePlan`).
It is **import-side-effect-free** and has **no** dependency on :mod:`genome.db` or any
database driver (plan §3 / A4): ``python -c "import genome.fast_follow.model"`` must not
import DuckDB or SQLCipher.

Two vocabulary decisions are load-bearing for safety (plan A1, R8):

* :data:`GUARD_CLASS_VOCAB` is **independent**, not imported from
  :mod:`genome.verify_gate.model` — the two consumers use the same four labels with
  *opposite* polarity (verify_gate: positive check-set selector; fast_follow: guard →
  EJECT), so a raw shared frozenset would be action-at-a-distance on a safety path. A
  reconciliation test (``GUARD_CLASS_VOCAB ⊆ CHANGE_CLASS_VOCAB``) keeps the
  single-source-of-truth benefit without the coupling.
* :data:`TIER_VOCAB` is named to **not** collide with the clinical ``1A | 1B | 2A | 2B |
  3 | 4`` evidence-tier scale; these are loop-internal drain tiers only.

The signatures below are the frozen data-model contract.
"""

from __future__ import annotations

import enum
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# ── Closed classification vocabulary ─────────────────────────────────────────


class Classification(enum.Enum):
    """The three terminal dispositions the fail-closed classifier assigns (plan §4).

    :attr:`DRAIN` is the only affirmative outcome — a Tier-0 / bounded-Tier-1 candidate
    safe to push through Sub-A's verify-and-merge gate. :attr:`EJECT` routes a guarded /
    anchor-exposed / over-cap / schema-touching candidate back to ``/scope-run``;
    :attr:`DISCARD` drops a stale / already-handled candidate. The classifier is
    fail-closed: anything undecidable resolves to :attr:`EJECT`, never :attr:`DRAIN`.
    """

    DRAIN = "drain"
    EJECT = "eject"
    DISCARD = "discard"


#: The loop-internal drain tiers. **Deliberately named to not collide** with the clinical
#: ``1A | 1B | 2A | 2B | 3 | 4`` evidence-tier scale (CLAUDE.md) — these are scan-derived
#: drain priorities, not evidence grades. ``tier-0`` = trivially drainable;
#: ``tier-1`` = bounded-drainable (subject to the blast-radius / anchor / guard checks).
TIER_VOCAB: frozenset[str] = frozenset({"tier-0", "tier-1"})

#: The change-class labels the guard recognizes — **independent** of
#: :data:`genome.verify_gate.model.CHANGE_CLASS_VOCAB` (plan A1). A reconciliation test
#: asserts ``GUARD_CLASS_VOCAB ⊆ CHANGE_CLASS_VOCAB`` so drift fails a test rather than
#: silently re-routing the safety classifier.
GUARD_CLASS_VOCAB: frozenset[str] = frozenset({"core", "schema", "pipeline", "annotation"})

#: The guarded subset the classifier EJECTs on (plan §4 step 4). A candidate whose
#: ``change_class`` intersects this set can never be DRAINed — schema/pipeline/annotation
#: changes carry real-data anchors and rebuild obligations that the drain lane cannot honor.
GUARDED_CLASSES: frozenset[str] = frozenset({"schema", "pipeline", "annotation"})

# ── Tunable bound constants ──────────────────────────────────────────────────

#: Per-item blast-radius DRAIN cap (ESC-1): a candidate touching more than this many files
#: is over-cap and EJECTs. Tunable here, not transcribed elsewhere.
MAX_DRAIN_FILES: int = 3

#: Per-batch item cap (plan §4 loop): a single ``plan_next_batch`` never plans more than
#: this many DRAIN/EJECT items; the remainder is explicit overflow, never silent truncation.
MAX_ITEMS: int = 10

#: Loop iteration cap (plan §4 loop): ``loop_done`` returns ``"cap"`` once this many batches
#: have run, bounding the self-spawning-nit termination alongside the seen-set dedup.
MAX_BATCHES: int = 3


# ── Frozen records ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Candidate:
    """One backlog item the loop triages — the skill-derived classifier input (plan §4, R3).

    The fields are **trusted skill-derived inputs** the pure core cannot itself verify
    (rank-1 risk): the model-driven triage step reads what each candidate would touch and
    derives ``change_class`` / ``applicable_anchors`` / ``blast_radius`` / ``tier`` /
    ``touched_paths``, emitting ``None`` where the read is unclear. A ``None`` in any
    decision-bearing field is the fail-closed signal that drives :attr:`Classification.EJECT`.
    ``touched_paths`` is the **literal read-from-disk** path list (plan A2): the classifier's
    independent path guard keys on it, so a schema item mislabeled ``core`` still EJECTs on
    its ``docs/schemas/**`` / ``ddl/**`` path.
    """

    candidate_id: str
    """Stable identifier for this candidate within a scan."""
    source: str
    """Where the candidate came from: ``repo-sweep`` | ``roadmap-deferred`` |
    ``finding-oos`` | ``stage3-nit``."""
    kind: str
    """The candidate's kind tag (free-form scan label, e.g. ``dead-code`` / ``nit``)."""
    change_class: frozenset[str]
    """The derived change-class label set (subset of :data:`GUARD_CLASS_VOCAB`). An empty
    set is the undecidable / unclassified case and fails closed to EJECT."""
    blast_radius: int | None
    """Derived count of files the fix would touch, or ``None`` when undecidable (→ EJECT)."""
    applicable_anchors: int | None
    """Count of real-data anchors the change would expose, or ``None`` when undecidable
    (→ EJECT). A non-zero count EJECTs (anchor-exposed)."""
    tier: str | None
    """The derived drain tier (a member of :data:`TIER_VOCAB`), or ``None`` (→ EJECT)."""
    touched_paths: tuple[str, ...]
    """The literal files the fix would touch — the independent path-guard key (plan A2)."""
    is_stale: bool
    """``True`` when the candidate is already handled / no longer applicable (→ DISCARD)."""

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> Candidate:
        """Build a :class:`Candidate` from a parsed-JSON mapping (the canonical seam, R2).

        Performs explicit per-field narrowing with no ``Any`` leak — collection fields go
        through the strict list narrowing so a mis-encoded ``touched_paths`` (a false-DRAIN
        path) cannot slip through. The accepted shape is the one :meth:`to_json` produces.
        """
        return cls(
            candidate_id=_as_str(data.get("candidate_id")),
            source=_as_str(data.get("source")),
            kind=_as_str(data.get("kind")),
            change_class=frozenset(_as_str_list(data.get("change_class"))),
            blast_radius=_as_opt_int(data.get("blast_radius")),
            applicable_anchors=_as_opt_int(data.get("applicable_anchors")),
            tier=_as_opt_str(data.get("tier")),
            touched_paths=tuple(_as_str_list(data.get("touched_paths"))),
            is_stale=_as_bool(data.get("is_stale", False)),
        )

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping — the inverse of :meth:`from_json`."""
        return {
            "candidate_id": self.candidate_id,
            "source": self.source,
            "kind": self.kind,
            "change_class": sorted(self.change_class),
            "blast_radius": self.blast_radius,
            "applicable_anchors": self.applicable_anchors,
            "tier": self.tier,
            "touched_paths": list(self.touched_paths),
            "is_stale": self.is_stale,
        }

    def seen_key(self) -> str:
        """Derive the stable cross-invocation dedup key for this candidate (plan R4, A3).

        A pure function over the candidate's identity-bearing fields — the key the
        persisted seen-set is keyed on, so a handled candidate is excluded on the next scan
        (the self-spawning-nit termination guard). No I/O here (I/O lives in
        :mod:`genome.fast_follow.persistence`).
        """
        return f"{self.source}:{self.candidate_id}"


@dataclass(frozen=True, slots=True)
class Triage:
    """The classifier's verdict for one :class:`Candidate` (plan §4).

    Pairs a candidate with its terminal :class:`Classification`, the reason string (for the
    human-readable triage block), an optional re-tier, and — for a DRAIN — which backlog
    item it drains (provenance, plan §4 step 5).
    """

    candidate_id: str
    """The :attr:`Candidate.candidate_id` this verdict is for."""
    classification: Classification
    """The terminal disposition assigned by :func:`genome.fast_follow.classifier.classify`."""
    retier: str | None
    """An optional re-assigned tier (a member of :data:`TIER_VOCAB`), or ``None``."""
    reason: str
    """Human-readable justification rendered in the triage block."""
    drains: str | None
    """For a DRAIN: which backlog item this drains (provenance); ``None`` otherwise."""


@dataclass(frozen=True, slots=True)
class TriagePlan:
    """The full result of one :func:`genome.fast_follow.loop.plan_next_batch` (plan §4).

    Holds the ordered per-item :class:`Triage` verdicts plus the explicit overflow /
    discard partitions (no silent truncation, plan §4 loop) and the ``dry`` /
    termination-summary metadata the formatter renders.
    """

    triaged: tuple[Triage, ...]
    """The ordered per-item verdicts for the items planned this batch."""
    overflow: tuple[Triage, ...]
    """Items beyond :data:`MAX_ITEMS` this batch — deferred, never dropped."""
    discards: tuple[Triage, ...]
    """Items classified :attr:`Classification.DISCARD` (stale / already handled)."""
    dry: bool
    """``True`` when this plan was produced under ``--dry-run`` (scan + triage only)."""
    termination: str | None
    """The loop-termination summary (``"dry"`` / ``"cap"`` / ``None``) from
    :func:`genome.fast_follow.loop.loop_done`, or ``None`` when the loop continues."""

    def counts(self) -> Mapping[str, int]:
        """Return the per-disposition counts (``drain`` / ``eject`` / ``discard``).

        A pure tally over ALL partitions — :attr:`triaged` + :attr:`overflow` + :attr:`discards`
        — so the headline the formatter and the ``--dry-run`` smoke assert against (e.g.
        ``2 DRAIN / 1 EJECT``) accounts for overflowed items too and never under-reports.
        """
        tally: Counter[str] = Counter()
        for triage in (*self.triaged, *self.overflow, *self.discards):
            tally[triage.classification.value] += 1
        return {
            "drain": tally[Classification.DRAIN.value],
            "eject": tally[Classification.EJECT.value],
            "discard": tally[Classification.DISCARD.value],
        }


# ── Strict JSON narrowing (no ``Any`` leak across the serialization seam) ─────


def _as_str(value: object) -> str:
    """Narrow a JSON scalar to ``str`` or raise — the seam never silently coerces."""
    if isinstance(value, str):
        return value
    msg = f"expected a string, got {type(value).__name__}: {value!r}"
    raise TypeError(msg)


def _as_opt_str(value: object) -> str | None:
    """Narrow to ``str | None`` (JSON ``null`` → ``None``)."""
    if value is None:
        return None
    return _as_str(value)


def _as_bool(value: object) -> bool:
    """Narrow a JSON scalar to ``bool`` or raise."""
    if isinstance(value, bool):
        return value
    msg = f"expected a boolean, got {type(value).__name__}: {value!r}"
    raise TypeError(msg)


def _as_opt_int(value: object) -> int | None:
    """Narrow to ``int | None`` (JSON ``null`` → ``None``); rejects ``bool`` masquerading as int."""
    if value is None:
        return None
    if isinstance(value, bool):
        msg = f"expected an integer, got bool: {value!r}"
        raise TypeError(msg)
    if isinstance(value, int):
        return value
    msg = f"expected an integer, got {type(value).__name__}: {value!r}"
    raise TypeError(msg)


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

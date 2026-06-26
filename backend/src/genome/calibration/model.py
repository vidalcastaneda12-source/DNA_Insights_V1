"""Frozen vocabulary + data model for the cross-run learning calibrator (``finding-040``).

This module is the **single deterministic source of truth** for the per-scope risk-tier
formula (Gate-1 = Option D1): :func:`compute_tier` is the one place ``S = C + B + P``, the
immutable floor, and the ``t1`` / ``t2`` banding live, parameterised by the tunable
:class:`RiskWeights` read from the git-tracked ``risk_weights.json``. The dispatcher RUNS
``genome calibrate compute-tier --manifest -`` and consumes the returned ``{tier, breakdown}``;
the prose C-map / B / P table in ``scope-dispatcher.md`` is demoted to non-authoritative
reference.

It also owns the calibration records the slow-loop ratchet reduces — the outcome ledger datum
(:class:`OutcomeRecord` with its nested :class:`PredictedBlock` / :class:`ActualBlock`), the
dispatch-time predicted manifest (:class:`PredictedManifest`), the ratchet verdict
(:class:`RatchetDecision` + :class:`Disposition` / :class:`Direction`), and the audit row
(:class:`AuditRow`) — plus the frozen regression fixtures the back-test and direction logic key
on (:data:`SEED_RISK_WEIGHTS`, :data:`BACKTEST_ROWS`, :data:`DIRECTION_WITNESS_LADDER`,
:data:`KNOB_COVERAGE`).

It is **import-side-effect-free** and has **no** dependency on :mod:`genome.db` *or*
:mod:`genome.config` (plan §3 / D1-two-db): ``python -c "import genome.calibration.model"`` must
not import DuckDB, SQLCipher, or the pydantic ``Settings``. The data/ paths are hard-coded in
:mod:`genome.calibration.persistence` (mirroring ``fast_follow``), never sourced from
``get_settings``.

Two design decisions are load-bearing for safety:

* The immutable floor (``schema | ddl`` touched **or** ``applicable_anchors >= 1`` → tier ≥ 2)
  is hard-coded in :func:`compute_tier`, **not** representable in :class:`RiskWeights` — there is
  no ``floor`` field and :meth:`RiskWeights.from_json` rejects a ``"floor"`` key (plan §3
  floors-immutable-by-construction). The only tunable surface is the additive ``c_map`` /
  ``b_buckets`` / ``p_levels`` maps + the ``t1`` / ``t2`` thresholds.
* The strict ``_as_*`` narrowers are **copied, not imported** from
  :mod:`genome.scope_split.model` — this module owns its serialization seam so a refactor of the
  frozen splitter can never silently move the calibrator's JSON contract. Every ``from_json``
  rejects an unexpected field, so PHI is structurally impossible in the ledger / manifest.

The frozen records, enums, seed data, and fixtures below are the contract the plan-blind
``test-author``'s tests are written against; :func:`compute_tier` and every serialization body are
implemented.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

# ── Closed change-class vocabulary (mirrors the dispatcher C-map / scope_split) ──

#: The closed set of canonical change classes — the **keys** of :attr:`RiskWeights.c_map` and a
#: byte-equal copy of :data:`genome.scope_split.model.CHANGE_CLASS_VOCAB` (a reconciliation test
#: pins ``CHANGE_CLASS_VOCAB == frozenset(SEED_RISK_WEIGHTS.c_map)`` and ``== scope_split``'s). A
#: label outside this set is a malformed manifest: :meth:`TierFields.from_json` rejects it (so a
#: structural ``ddl`` / ``schema`` typo can never silently under-tier — the irreversible
#: direction), and :func:`_c_score` raises on it (the direct-construction backstop).
CHANGE_CLASS_VOCAB: frozenset[str] = frozenset(
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
    },
)

#: The static `Literal` of the same 10 classes — typing ``TierFields.change_class`` with it makes a
#: directly-constructed bad label (e.g. the frozen ``BACKTEST_ROWS`` / witnesses) a mypy error,
#: closing the floor-bypass statically; the runtime guards above close it dynamically.
ChangeClass = Literal[
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
]

# ── Closed precedent-surprise vocabulary ─────────────────────────────────────

#: The closed set of precedent-surprise categories a :class:`TierFields` may carry. These are
#: the **keys** of :attr:`RiskWeights.p_levels`; :func:`compute_tier` scores ``P`` by looking the
#: category up in ``p_levels`` (``P = p_levels[fields.precedent_surprise]``). Under
#: :data:`SEED_RISK_WEIGHTS` they map ``clean → 0 · minor → 1 · correction → 2`` (matching the
#: dispatcher's ``clean 0 · minor/noted 1 · correction-class 2``). A category outside this set is
#: a malformed manifest (fail-closed at the seam).
PRECEDENT_SURPRISE_VOCAB: frozenset[str] = frozenset({"clean", "minor", "correction"})

#: The static `Literal` of the precedent categories — typing ``precedent_surprise`` with it makes
#: the bare ``p_levels[fields.precedent_surprise]`` subscript safe under direct construction.
PrecedentSurprise = Literal["clean", "minor", "correction"]

# ── Closed gate-verdict vocabulary (the merge-time ground-truth verdict) ──────

#: The closed set of verify-gate verdicts an :class:`ActualBlock` may carry. ``pass`` confirms a
#: merged scope; ``blocked`` / ``escalate`` are the hindsight Tier-2 signals
#: (:data:`genome.calibration.accuracy._BLOCKING_VERDICTS` is a subset). A verdict outside this set
#: is a malformed close: :meth:`ActualBlock.from_json` rejects it, so a mis-cased ``BLOCKED`` can
#: never be silently treated as a pass and corrupt the learning signal.
GATE_VERDICT_VOCAB: frozenset[str] = frozenset({"pass", "blocked", "escalate"})

#: The static `Literal` of the gate verdicts (exhaustive over the hindsight ladder's branches).
GateVerdict = Literal["pass", "blocked", "escalate"]

# ── Closed tier / floor literals (a tier is always 0/1/2; a floor 0/2) ────────

#: A risk tier is always one of ``{0, 1, 2}`` — typing it closes an out-of-range tier statically.
TierInt = Literal[0, 1, 2]

#: The immutable floor is always ``0`` or ``2`` (Tier 2 on a structural change or any anchor).
FloorInt = Literal[0, 2]

#: The four ``b_buckets`` band names, in ascending footprint order. The band *boundaries*
#: (``|imports| <= 1`` isolated · ``2-5`` small · ``6-15`` moderate · ``> 15`` large) are
#: hard-coded in :func:`compute_tier` and are **not** tunable; only the per-band *score* in
#: :attr:`RiskWeights.b_buckets` is.
BLAST_BAND_NAMES: tuple[str, ...] = ("isolated", "small", "moderate", "large")


# ── Tunable weights (the only mutable knobs; floors are NOT here) ─────────────


@dataclass(frozen=True, slots=True)
class RiskWeights:
    """The tunable risk-tier knobs — the data the dispatcher reads and the ratchet writes.

    Extracted from ``scope-dispatcher.md`` prose into versioned, git-tracked data
    (``risk_weights.json``). The **floors are deliberately absent** (plan §3): the immutable
    trip-wire (``schema | ddl`` or any anchor → tier ≥ 2) is hard-coded in :func:`compute_tier`,
    and :meth:`from_json` rejects a ``"floor"`` key so an auto-tune can never weaken it. The
    tunable surface is exactly the three additive maps plus the two banding thresholds.
    """

    weights_version: str
    """Monotonic version label (e.g. ``rw-1``); every ratchet auto-change bumps it (plan §3 D8)."""
    c_map: Mapping[str, int]
    """Per-change-class sub-score (the dispatcher ``C`` term). Reconciliation-pinned to
    :data:`genome.scope_split.model._C_MAP` on the seed."""
    b_buckets: Mapping[str, int]
    """Per-band blast-radius sub-score (the ``B`` term), keyed by :data:`BLAST_BAND_NAMES`."""
    p_levels: Mapping[str, int]
    """Per-category precedent-surprise sub-score (the ``P`` term), keyed by
    :data:`PRECEDENT_SURPRISE_VOCAB`."""
    t1: int
    """Lower banding threshold: ``S < t1`` → Tier 0 (seed ``1``)."""
    t2: int
    """Upper banding threshold: ``S >= t2`` → Tier 2 (seed ``5``)."""
    auto_tuning_enabled: bool
    """The kill switch (plan OQ-3): when ``False`` the ratchet is dark (always ``NO_OP``)."""
    provenance: Mapping[str, object]
    """The provenance block: rationale + cited-outcomes + back-test-diff + parent-version a
    ratchet write records (plan §3 D8). Seed carries a ``source: "seed"`` rationale."""

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> RiskWeights:
        """Build a :class:`RiskWeights` from a parsed ``risk_weights.json`` mapping.

        **Fail-closed**: a ``"floor"`` key raises :class:`ValueError` (the floor is immutable and
        not representable — plan §3); every required knob is narrowed with no ``Any`` leak. This
        is the *only* reader of the live config.
        """
        if "floor" in data:
            msg = "risk_weights.json must not carry a 'floor' key — the floor is immutable"
            raise ValueError(msg)
        unexpected = set(data) - _RISK_WEIGHTS_KEYS
        if unexpected:
            msg = f"risk_weights.json has unexpected field(s) {sorted(unexpected)!r}"
            raise ValueError(msg)
        for required in ("weights_version", "c_map", "b_buckets", "p_levels", "t1", "t2"):
            if required not in data:
                msg = f"risk_weights.json is missing required field {required!r}"
                raise ValueError(msg)
        return cls(
            weights_version=_as_str(data["weights_version"]),
            c_map=_as_int_map(data["c_map"]),
            b_buckets=_as_int_map(data["b_buckets"]),
            p_levels=_as_int_map(data["p_levels"]),
            t1=_as_int(data["t1"]),
            t2=_as_int(data["t2"]),
            auto_tuning_enabled=_as_bool(data.get("auto_tuning_enabled", False)),
            provenance=_as_mapping(data.get("provenance")),
        )

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping — the inverse of :meth:`from_json`.

        Emits exactly ``weights_version`` / ``c_map`` / ``b_buckets`` / ``p_levels`` / ``t1`` /
        ``t2`` / ``auto_tuning_enabled`` / ``provenance`` — never a ``floor`` key.
        """
        return {
            "weights_version": self.weights_version,
            "c_map": dict(self.c_map),
            "b_buckets": dict(self.b_buckets),
            "p_levels": dict(self.p_levels),
            "t1": self.t1,
            "t2": self.t2,
            "auto_tuning_enabled": self.auto_tuning_enabled,
            "provenance": dict(self.provenance),
        }


# ── Tier inputs + breakdown ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TierFields:
    """The raw, manifest-derived inputs :func:`compute_tier` scores (plan §4 T1c + v2.1).

    Assembled by the dispatcher from its scope manifest and piped to
    ``genome calibrate compute-tier --manifest -``. These are the *raw* signals (change classes,
    footprint count, precedent category, anchor count) plus the two dispatch-time bump triggers
    the formula can know — **not** the already-computed sub-scores. :meth:`from_json` is
    fail-closed and **rejects an unexpected field** (PHI structurally impossible, plan §3 D9).
    """

    change_class: tuple[ChangeClass, ...]
    """The (possibly multi-label) change classes (each a :data:`ChangeClass`) — keys into
    :attr:`RiskWeights.c_map`."""
    imports_touched_count: int
    """``|blast_radius.imports_touched|`` — banded into ``B`` by :data:`BLAST_BAND_NAMES`."""
    precedent_surprise: PrecedentSurprise
    """The precedent-surprise category (a :data:`PrecedentSurprise`) — keyed into
    :attr:`RiskWeights.p_levels` for ``P``."""
    applicable_anchors_count: int
    """``|applicable_anchors|`` — drives the immutable Tier-2 floor and the ``A`` depth
    sub-score."""
    has_open_questions: bool = False
    """``True`` when ``manifest.open_questions`` is non-empty — a dispatch-time conservative bump
    trigger (v2.1 amendment): ``tier = min(2, base_tier + 1)``."""
    human_bump: bool = False
    """``True`` when the operator forces a conservative bump — the second dispatch-time bump
    trigger (v2.1 amendment). The pre-mortem=probe-first bump is a *later-stage* signal and is
    **not** a :class:`TierFields` input."""

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> TierFields:
        """Build a :class:`TierFields` from a parsed manifest mapping (the compute-tier seam).

        Mirrors :meth:`genome.scope_split.model.ScopeManifestInput.from_json` (strict narrow) but
        **rejects any unexpected key** so no profile-bearing field can ride into the calibrator
        (plan §3 D9). ``change_class`` and ``imports_touched_count`` are required; the two bump
        triggers default to ``False``.
        """
        unexpected = set(data) - _TIER_FIELDS_KEYS
        if unexpected:
            msg = f"TierFields has unexpected field(s) {sorted(unexpected)!r}"
            raise ValueError(msg)
        for required in ("change_class", "imports_touched_count"):
            if required not in data:
                msg = f"TierFields is missing required field {required!r}"
                raise ValueError(msg)
        change_class = tuple(_as_str_list(data["change_class"]))
        unknown = [label for label in change_class if label not in CHANGE_CLASS_VOCAB]
        if unknown:
            msg = (
                f"TierFields change_class has unknown label(s) {unknown!r}; expected a subset of "
                f"{sorted(CHANGE_CLASS_VOCAB)!r}"
            )
            raise ValueError(msg)
        precedent_surprise = _as_str(data.get("precedent_surprise", "clean"))
        if precedent_surprise not in PRECEDENT_SURPRISE_VOCAB:
            msg = (
                f"TierFields precedent_surprise {precedent_surprise!r} is not one of "
                f"{sorted(PRECEDENT_SURPRISE_VOCAB)!r}"
            )
            raise ValueError(msg)
        return cls(
            change_class=cast("tuple[ChangeClass, ...]", change_class),
            imports_touched_count=_as_int(data["imports_touched_count"]),
            precedent_surprise=cast("PrecedentSurprise", precedent_surprise),
            applicable_anchors_count=_as_int(data.get("applicable_anchors_count", 0)),
            has_open_questions=_as_bool(data.get("has_open_questions", False)),
            human_bump=_as_bool(data.get("human_bump", False)),
        )

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping — the inverse of :meth:`from_json`."""
        return {
            "change_class": list(self.change_class),
            "imports_touched_count": self.imports_touched_count,
            "precedent_surprise": self.precedent_surprise,
            "applicable_anchors_count": self.applicable_anchors_count,
            "has_open_questions": self.has_open_questions,
            "human_bump": self.human_bump,
        }


@dataclass(frozen=True, slots=True)
class TierBreakdown:
    """The auditable sub-scores :func:`compute_tier` emits alongside the final tier.

    Mirrors the dispatcher's ``risk_breakdown`` object so the rewired dispatcher can splice it
    straight into its manifest. ``floor`` is the *derived* immutable-floor value (0 or 2), not a
    tunable input. ``deep_t2`` is the review-depth selector (``S >= 7`` or ``A >= 3``) the
    dispatcher emits as ``deep_T2`` (v2.1 amendment — re-homed into :func:`compute_tier`).
    """

    c: int
    """The change-class sub-score ``C`` (max ``c_map`` over the classes, ``+1`` if ≥3 code
    concerns)."""
    b: int
    """The blast-radius sub-score ``B`` (the band score for ``imports_touched_count``)."""
    p: int
    """The precedent-surprise sub-score ``P`` (``p_levels[precedent_surprise]``)."""
    a: int
    """The anchor-exposure depth sub-score ``A`` (``none 0 · 1-2 → 2 · 3+ → 3``)."""
    s: int
    """The additive score ``S = C + B + P`` (``A`` folds in only inside Tier 2, never into
    ``S``)."""
    floor: FloorInt
    """The immutable floor (``2`` iff ``schema | ddl`` touched or ``anchors >= 1``, else ``0``)."""
    deep_t2: bool
    """``True`` iff ``S >= 7`` or ``A >= 3`` — the deep-review (``deep_T2``) selector."""

    def to_json(self) -> dict[str, object]:
        """Serialize to the dispatcher ``risk_breakdown`` shape.

        Emits uppercase keys ``{"C", "B", "P", "A", "S", "floor", "deep_T2"}`` so the output is a
        drop-in for the dispatcher manifest's ``risk_breakdown`` object.
        """
        return {
            "C": self.c,
            "B": self.b,
            "P": self.p,
            "A": self.a,
            "S": self.s,
            "floor": self.floor,
            "deep_T2": self.deep_t2,
        }


#: At or above this many distinct *code* concerns the ``C`` term gets a ``+1`` (the dispatcher's
#: multi-concern bump). ``docs`` is the one non-code concern, excluded from the count.
_MULTI_CONCERN_THRESHOLD: int = 3

#: ``docs`` is the one non-code concern — excluded from the ``+1`` multi-concern count.
_NON_CODE_CONCERNS: frozenset[str] = frozenset({"docs"})

#: The immutable Tier-2 floor change classes (a structural change is Tier 2, period).
_STRUCTURAL_CLASSES: frozenset[str] = frozenset({"schema", "ddl"})

#: ``S >= _DEEP_T2_S`` selects the deep-review (``deep_T2``) lane on the additive score.
_DEEP_T2_S: int = 7

#: ``A >= _DEEP_T2_A`` (3+ anchors) also selects the deep-review lane.
_DEEP_T2_A: int = 3

#: The maximum tier — the conservative bump and the banding both clamp to this.
_MAX_TIER: Literal[2] = 2


def _blast_band(imports_touched_count: int) -> str:
    """Band ``|imports_touched|`` to a :data:`BLAST_BAND_NAMES` name (boundaries are fixed)."""
    if imports_touched_count <= 1:
        return "isolated"
    if imports_touched_count <= 5:  # noqa: PLR2004 - dispatcher band boundary (small 2-5)
        return "small"
    if imports_touched_count <= 15:  # noqa: PLR2004 - dispatcher band boundary (moderate 6-15)
        return "moderate"
    return "large"


def _anchor_score(applicable_anchors_count: int) -> int:
    """The ``A`` depth sub-score: ``none 0 · 1-2 → 2 · 3+ → 3`` (within-Tier-2 depth knob)."""
    if applicable_anchors_count <= 0:
        return 0
    if applicable_anchors_count <= 2:  # noqa: PLR2004 - dispatcher anchor band (1-2 → 2)
        return 2
    return _DEEP_T2_A


def _c_score(change_class: tuple[str, ...], c_map: Mapping[str, int]) -> int:
    """The ``C`` term: max class sub-score, ``+1`` when ≥3 distinct code concerns (``docs`` out).

    Raises :class:`ValueError` on a label absent from ``c_map`` — the direct-construction backstop
    to the :meth:`TierFields.from_json` vocab guard, so a typo'd or mis-cased structural class can
    never silently score ``C = 0`` and under-tier (the irreversible direction).
    """
    if not change_class:
        return 0
    for label in change_class:
        if label not in c_map:
            msg = (
                f"unknown change_class label {label!r}; expected a subset of "
                f"{sorted(CHANGE_CLASS_VOCAB)!r}"
            )
            raise ValueError(msg)
    base = max(c_map[label] for label in change_class)
    code_concerns = {label for label in change_class if label not in _NON_CODE_CONCERNS}
    if len(code_concerns) >= _MULTI_CONCERN_THRESHOLD:
        return base + 1
    return base


def compute_tier(fields: TierFields, weights: RiskWeights) -> tuple[TierInt, TierBreakdown]:
    """The deterministic risk-tier formula (Gate-1 = D1) — returns ``(final_tier, breakdown)``.

    Pure. The one source of truth the dispatcher RUNS via
    ``genome calibrate compute-tier --manifest -`` and consumes. Computation:

    * ``C`` = ``max(c_map[label] for label in change_class)``, ``+1`` when ≥3 distinct *code*
      concerns (``docs`` excluded).
    * ``B`` = ``b_buckets[band]`` where ``band`` is ``isolated`` (``<= 1``) / ``small`` (``2-5``) /
      ``moderate`` (``6-15``) / ``large`` (``> 15``) over ``imports_touched_count``.
    * ``P`` = ``p_levels[precedent_surprise]``.
    * ``A`` = ``0`` (no anchors) / ``2`` (1-2) / ``3`` (3+) from ``applicable_anchors_count``.
    * ``S`` = ``C + B + P`` (``A`` is **not** in ``S``).
    * ``floor`` = ``2`` iff (``{"schema", "ddl"}`` ∩ ``change_class``) **or**
      ``applicable_anchors_count >= 1``, else ``0`` — immutable, never from ``weights``.
    * ``base_tier`` = ``max(floor, 0 if S < t1 else 1 if S < t2 else 2)``.
    * ``final_tier`` = ``min(2, base_tier + 1)`` when ``has_open_questions`` or ``human_bump``
      (the dispatch-time conservative bump, v2.1 amendment), else ``base_tier``.
    * ``deep_t2`` = ``(S >= 7) or (A >= 3)``.

    Returns the **final** tier (bump applied) and the :class:`TierBreakdown`. The
    pre-mortem=probe-first re-bump is a later-stage dispatcher step, **not** applied here.
    """
    c = _c_score(fields.change_class, weights.c_map)
    b = weights.b_buckets[_blast_band(fields.imports_touched_count)]
    p = weights.p_levels[fields.precedent_surprise]
    a = _anchor_score(fields.applicable_anchors_count)
    s = c + b + p

    structural = bool(_STRUCTURAL_CLASSES & set(fields.change_class))
    floor: FloorInt = _MAX_TIER if (structural or fields.applicable_anchors_count >= 1) else 0

    if s < weights.t1:
        tier_from_s = 0
    elif s < weights.t2:
        tier_from_s = 1
    else:
        tier_from_s = _MAX_TIER
    base_tier = max(floor, tier_from_s)

    if fields.has_open_questions or fields.human_bump:
        final_tier = min(_MAX_TIER, base_tier + 1)
    else:
        final_tier = base_tier

    deep_t2 = (s >= _DEEP_T2_S) or (a >= _DEEP_T2_A)
    breakdown = TierBreakdown(c=c, b=b, p=p, a=a, s=s, floor=floor, deep_t2=deep_t2)
    return cast("TierInt", final_tier), breakdown


# ── Outcome ledger datum (written at A's close) ──────────────────────────────


@dataclass(frozen=True, slots=True)
class PredictedBlock:
    """The dispatch-time prediction half of an :class:`OutcomeRecord` (plan §3 datum).

    Sourced from the persisted dispatch-time manifest (:class:`PredictedManifest`), never
    re-typed at close. ``tier`` is the **final** (post-dispatch-bump) tier the dispatcher used
    (v2.1 fix_persist), so ``tier_in_hindsight`` compares like-for-like. ``premortem_surprises`` /
    ``anchors_to_watch`` feed the report-only pre-mortem precision/recall and default empty.
    """

    tier: TierInt
    """The final predicted tier (the tier the dispatcher actually used)."""
    breakdown: TierBreakdown
    """The sub-score breakdown :func:`compute_tier` emitted at dispatch."""
    premortem_surprises: tuple[str, ...] = ()
    """The pre-mortem's predicted surprises (for precision/recall vs. what materialized)."""
    anchors_to_watch: tuple[str, ...] = ()
    """The anchors the pre-mortem flagged to watch."""

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> PredictedBlock:
        """Build a :class:`PredictedBlock` from a parsed mapping (rejects unexpected fields)."""
        unexpected = set(data) - _PREDICTED_BLOCK_KEYS
        if unexpected:
            msg = f"PredictedBlock has unexpected field(s) {sorted(unexpected)!r}"
            raise ValueError(msg)
        if "tier" not in data or "breakdown" not in data:
            msg = "PredictedBlock requires 'tier' and 'breakdown'"
            raise ValueError(msg)
        return cls(
            tier=_as_tier(data["tier"]),
            breakdown=_tier_breakdown_from_json(data["breakdown"]),
            premortem_surprises=tuple(_as_str_list(data.get("premortem_surprises"))),
            anchors_to_watch=tuple(_as_str_list(data.get("anchors_to_watch"))),
        )

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping — the inverse of :meth:`from_json`."""
        return {
            "tier": self.tier,
            "breakdown": self.breakdown.to_json(),
            "premortem_surprises": list(self.premortem_surprises),
            "anchors_to_watch": list(self.anchors_to_watch),
        }


@dataclass(frozen=True, slots=True)
class ActualBlock:
    """The merge-time ground-truth half of an :class:`OutcomeRecord` (plan §3 datum).

    Sourced from **human-confirmed gate facts + recorded run artifacts** at A's close — never
    Claude's self-assessment (plan §4 default 4). :meth:`from_json` rejects an unexpected field so
    no profile content can enter the ledger (plan §3 D9). These are the teeth
    :func:`genome.calibration.accuracy.tier_in_hindsight` reads.
    """

    gate_verdict: GateVerdict
    """The verify-gate verdict (a :data:`GateVerdict`: ``pass`` / ``blocked`` / ``escalate``)."""
    review_blockers: tuple[str, ...] = ()
    """Stage-3 review blockers that materialized."""
    surprises_materialized: tuple[str, ...] = ()
    """Pre-mortem surprises that actually happened (precision numerator)."""
    surprises_missed: tuple[str, ...] = ()
    """Surprises that happened but the pre-mortem did **not** predict (recall blind spots)."""
    anchors_moved_unexpected: tuple[str, ...] = ()
    """Real-data anchors that moved when they were not expected to."""
    revise_cycles: int = 0
    """Number of revise cycles the scope needed."""
    fix_first_cycles: int = 0
    """Number of fix-first cycles the scope needed."""
    needed_deep: bool = False
    """``True`` iff the scope actually needed deep review."""

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> ActualBlock:
        """Build an :class:`ActualBlock` from a parsed mapping (rejects unexpected fields)."""
        unexpected = set(data) - _ACTUAL_BLOCK_KEYS
        if unexpected:
            msg = f"ActualBlock has unexpected field(s) {sorted(unexpected)!r}"
            raise ValueError(msg)
        if "gate_verdict" not in data:
            msg = "ActualBlock requires 'gate_verdict'"
            raise ValueError(msg)
        gate_verdict = _as_str(data["gate_verdict"])
        if gate_verdict not in GATE_VERDICT_VOCAB:
            msg = (
                f"ActualBlock gate_verdict {gate_verdict!r} is not one of "
                f"{sorted(GATE_VERDICT_VOCAB)!r}"
            )
            raise ValueError(msg)
        return cls(
            gate_verdict=cast("GateVerdict", gate_verdict),
            review_blockers=tuple(_as_str_list(data.get("review_blockers"))),
            surprises_materialized=tuple(_as_str_list(data.get("surprises_materialized"))),
            surprises_missed=tuple(_as_str_list(data.get("surprises_missed"))),
            anchors_moved_unexpected=tuple(_as_str_list(data.get("anchors_moved_unexpected"))),
            revise_cycles=_as_int(data.get("revise_cycles", 0)),
            fix_first_cycles=_as_int(data.get("fix_first_cycles", 0)),
            needed_deep=_as_bool(data.get("needed_deep", False)),
        )

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping — the inverse of :meth:`from_json`."""
        return {
            "gate_verdict": self.gate_verdict,
            "review_blockers": list(self.review_blockers),
            "surprises_materialized": list(self.surprises_materialized),
            "surprises_missed": list(self.surprises_missed),
            "anchors_moved_unexpected": list(self.anchors_moved_unexpected),
            "revise_cycles": self.revise_cycles,
            "fix_first_cycles": self.fix_first_cycles,
            "needed_deep": self.needed_deep,
        }


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One append-only outcome-ledger datum (plan §3) — a ``predicted`` vs ``actual`` pair.

    Written at A's close (``/verify-and-merge`` Step 9), one JSONL line per merged scope. Every
    record **names its** ``risk_weights_version`` (sourced from the persisted manifest);
    :meth:`from_json` RAISES if it is missing (plan §3 D8) so the ratchet can never attribute an
    outcome to the wrong weights epoch.
    """

    scope_id: str
    """The dispatcher scope id (e.g. ``PR-6``)."""
    merged_sha: str
    """The squash-merge SHA the outcome is attributed to."""
    date: str
    """ISO date string of the close (metadata only — no datetime dependency)."""
    risk_weights_version: str
    """The ``weights_version`` in force at dispatch — from the persisted manifest, not re-typed."""
    predicted: PredictedBlock
    """The dispatch-time prediction."""
    actual: ActualBlock
    """The merge-time ground truth."""

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> OutcomeRecord:
        """Build an :class:`OutcomeRecord` from a parsed JSONL line.

        **Fail-closed**: a missing ``risk_weights_version`` raises :class:`ValueError` (plan §3
        D8); an unexpected top-level field raises (plan §3 D9 — PHI impossible).
        """
        unexpected = set(data) - _OUTCOME_RECORD_KEYS
        if unexpected:
            msg = f"OutcomeRecord has unexpected field(s) {sorted(unexpected)!r}"
            raise ValueError(msg)
        if "risk_weights_version" not in data:
            msg = "OutcomeRecord is missing required field 'risk_weights_version'"
            raise ValueError(msg)
        for required in ("predicted", "actual"):
            if required not in data:
                msg = f"OutcomeRecord is missing required field {required!r}"
                raise ValueError(msg)
        return cls(
            scope_id=_as_str(data.get("scope_id", "")),
            merged_sha=_as_str(data.get("merged_sha", "")),
            date=_as_str(data.get("date", "")),
            risk_weights_version=_as_str(data["risk_weights_version"]),
            predicted=PredictedBlock.from_json(_as_mapping(data["predicted"])),
            actual=ActualBlock.from_json(_as_mapping(data["actual"])),
        )

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping — the inverse of :meth:`from_json`."""
        return {
            "scope_id": self.scope_id,
            "merged_sha": self.merged_sha,
            "date": self.date,
            "risk_weights_version": self.risk_weights_version,
            "predicted": self.predicted.to_json(),
            "actual": self.actual.to_json(),
        }


@dataclass(frozen=True, slots=True)
class PredictedManifest:
    """The dispatch-time predicted store written by ``compute-tier --persist`` (plan §4 T5 / FIX-3).

    Persisted to ``data/calibration/manifests/<scope_id>.json`` at dispatch, then read back at A's
    close so ``write-outcome`` sources ``predicted.{tier, breakdown}`` + ``risk_weights_version``
    from what the dispatcher actually used — closing the write-hook loop. Holds exactly the
    predicted half plus the weights version (no ``actual`` facts).
    """

    scope_id: str
    """The scope id this manifest predicts (the persist filename stem)."""
    risk_weights_version: str
    """The ``weights_version`` in force when the manifest was written."""
    predicted: PredictedBlock
    """The predicted tier + breakdown the dispatcher used."""

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> PredictedManifest:
        """Build a :class:`PredictedManifest` from a parsed manifest file (rejects unexpected)."""
        unexpected = set(data) - _PREDICTED_MANIFEST_KEYS
        if unexpected:
            msg = f"PredictedManifest has unexpected field(s) {sorted(unexpected)!r}"
            raise ValueError(msg)
        for required in ("scope_id", "risk_weights_version", "predicted"):
            if required not in data:
                msg = f"PredictedManifest is missing required field {required!r}"
                raise ValueError(msg)
        return cls(
            scope_id=_as_str(data["scope_id"]),
            risk_weights_version=_as_str(data["risk_weights_version"]),
            predicted=PredictedBlock.from_json(_as_mapping(data["predicted"])),
        )

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping — the inverse of :meth:`from_json`."""
        return {
            "scope_id": self.scope_id,
            "risk_weights_version": self.risk_weights_version,
            "predicted": self.predicted.to_json(),
        }


# ── Ratchet verdict + audit row ──────────────────────────────────────────────


class Disposition(enum.Enum):
    """The four terminal dispositions :func:`genome.calibration.ratchet.propose_ratchet` assigns.

    :attr:`AUTO_COMMIT` is the *only* auto-applied outcome and is reachable **only** for a
    back-test-clean, unfloored-covered TIGHTEN (plan §3 D1/D2). :attr:`PARK_FOR_APPROVAL` holds a
    loosen *or* a clean-by-vacuity tighten for one-click human approval; :attr:`SUPPRESSED` is a
    back-test-failing tighten; :attr:`NO_OP` is the fail-closed default (kill-switch / thin-data /
    cadence / hysteresis not met).
    """

    AUTO_COMMIT = "auto_commit"
    PARK_FOR_APPROVAL = "park_for_approval"
    SUPPRESSED = "suppressed"
    NO_OP = "no_op"


class Direction(enum.Enum):
    """The semantic direction of a candidate weights change — by **tier delta**, never knob sign.

    :attr:`LOOSEN` iff the candidate makes ``compute_tier`` return a *lower* tier for ANY
    direction-witness ladder probe (a ``t1`` / ``t2`` raise is a LOOSEN, not a tighten — plan §4
    T3 FIX-2); :attr:`TIGHTEN` otherwise. A ``NO_OP`` decision carries ``direction=None``.
    """

    TIGHTEN = "tighten"
    LOOSEN = "loosen"


@dataclass(frozen=True, slots=True)
class RatchetDecision:
    """The verdict of one ratchet pass (plan §4 T3).

    Carries the disposition, the targeted knob + semantic direction, the version-bumped candidate
    weights, the two safety gates' results (back-test clean, knob unfloored-covered), the cited
    merged SHAs, the rationale, and the derived ``auto_applicable`` flag. **Invariant**:
    ``auto_applicable`` is ``True`` iff the disposition is :attr:`Disposition.AUTO_COMMIT`, which
    requires ``direction == TIGHTEN`` **and** ``backtest_clean`` **and** ``knob_covered``.
    """

    disposition: Disposition
    """The terminal disposition."""
    knob: str | None
    """The targeted knob (e.g. ``c_map.cli`` / ``t2``), or ``None`` for a ``NO_OP``."""
    direction: Direction | None
    """The tier-delta direction, or ``None`` for a ``NO_OP``."""
    candidate_weights: RiskWeights | None
    """The version-bumped candidate (``None`` for a ``NO_OP``)."""
    backtest_clean: bool
    """``True`` iff the candidate flipped no :data:`BACKTEST_ROWS` known-correct tier."""
    knob_covered: bool
    """``True`` iff the targeted knob has at least one UNFLOORED covering row
    (:data:`KNOB_COVERAGE`)."""
    cited_merged_shas: tuple[str, ...]
    """The merged SHAs whose outcomes drove this proposal (audit provenance)."""
    rationale: str
    """Human-readable justification rendered into the audit row + commit message."""
    auto_applicable: bool
    """Derived: ``disposition is AUTO_COMMIT`` (TIGHTEN ∧ backtest_clean ∧ knob_covered)."""

    def __post_init__(self) -> None:
        """Reject the illegal states at construction (the invariant the renderers rely on).

        An ``AUTO_COMMIT`` must carry ``candidate_weights`` (there is something to commit) and a
        ``NO_OP`` must not (there is nothing to apply) — so :func:`genome.calibration.commit_plan.
        render_commit_plan` never has to fall back on a missing candidate.
        """
        if self.disposition is Disposition.AUTO_COMMIT and self.candidate_weights is None:
            msg = "an AUTO_COMMIT RatchetDecision must carry candidate_weights"
            raise ValueError(msg)
        if self.disposition is Disposition.NO_OP and self.candidate_weights is not None:
            msg = "a NO_OP RatchetDecision must not carry candidate_weights"
            raise ValueError(msg)

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping (for the audit row + report)."""
        return {
            "disposition": self.disposition.value,
            "knob": self.knob,
            "direction": self.direction.value if self.direction is not None else None,
            "candidate_weights": (
                self.candidate_weights.to_json() if self.candidate_weights is not None else None
            ),
            "backtest_clean": self.backtest_clean,
            "knob_covered": self.knob_covered,
            "cited_merged_shas": list(self.cited_merged_shas),
            "rationale": self.rationale,
            "auto_applicable": self.auto_applicable,
        }


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One append-only ratchet-audit datum (plan §3 D8 / §4 T5).

    Every ratchet pass that proposes a change — auto-committed, parked, or suppressed — appends an
    :class:`AuditRow` to ``data/calibration/ratchet_audit.jsonl`` for periodic human review and
    easy revert. Wraps the :class:`RatchetDecision` with the close date and whether it was applied.
    """

    date: str
    """ISO date string of the ratchet pass."""
    applied: bool
    """``True`` iff the candidate weights were actually written (an applied AUTO_COMMIT)."""
    decision: RatchetDecision
    """The full ratchet verdict this row records."""

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> AuditRow:
        """Build an :class:`AuditRow` from a parsed JSONL line (rejects unexpected fields)."""
        unexpected = set(data) - _AUDIT_ROW_KEYS
        if unexpected:
            msg = f"AuditRow has unexpected field(s) {sorted(unexpected)!r}"
            raise ValueError(msg)
        for required in ("date", "applied", "decision"):
            if required not in data:
                msg = f"AuditRow is missing required field {required!r}"
                raise ValueError(msg)
        return cls(
            date=_as_str(data["date"]),
            applied=_as_bool(data["applied"]),
            decision=_ratchet_decision_from_json(_as_mapping(data["decision"])),
        )

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping — the inverse of :meth:`from_json`."""
        return {
            "date": self.date,
            "applied": self.applied,
            "decision": self.decision.to_json(),
        }


# ── Back-test fixture row ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BacktestRow:
    """One row of the frozen regression fixture (:data:`BACKTEST_ROWS`).

    Pairs a known historical scope's :class:`TierFields` with its known-correct tier. The
    back-test (:func:`genome.calibration.backtest.run_backtest`) re-scores every row under a
    candidate weights set and is *clean* iff no row's tier changes from :attr:`expected_tier` — the
    hard gate that rejects any change flipping a settled call.
    """

    scope_id: str
    """The historical scope id (e.g. ``PR-5a``)."""
    fields: TierFields
    """The raw inputs that reproduce the dispatcher's recorded sub-scores."""
    expected_tier: TierInt
    """The known-correct tier ``compute_tier(fields, SEED_RISK_WEIGHTS)`` must reproduce."""


# ── Strict JSON narrowing (copied from scope_split — this module owns its seam) ──


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


def _as_tier(value: object) -> TierInt:
    """Narrow a JSON scalar to a tier in ``{0, 1, 2}``; an out-of-range value is malformed."""
    tier = _as_int(value)
    if tier not in (0, 1, 2):
        msg = f"expected a tier in {{0, 1, 2}}, got {tier!r}"
        raise ValueError(msg)
    return cast("TierInt", tier)


def _as_floor(value: object) -> FloorInt:
    """Narrow a JSON scalar to a floor in ``{0, 2}`` or raise (the floor is 0 or Tier 2 only)."""
    floor = _as_int(value)
    if floor not in (0, 2):
        msg = f"expected a floor in {{0, 2}}, got {floor!r}"
        raise ValueError(msg)
    return cast("FloorInt", floor)


def _as_bool(value: object) -> bool:
    """Narrow a JSON scalar to ``bool`` or raise."""
    if isinstance(value, bool):
        return value
    msg = f"expected a boolean, got {type(value).__name__}: {value!r}"
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


def _as_int_map(value: object) -> dict[str, int]:
    """Narrow a JSON value to ``dict[str, int]`` (the ``c_map`` / ``b_buckets`` / ``p_levels``
    shape)."""
    out: dict[str, int] = {}
    for key, val in _as_mapping(value).items():
        out[key] = _as_int(val)
    return out


def _as_opt_str(value: object) -> str | None:
    """Narrow a JSON value to ``str`` or ``None`` (the nullable ``knob`` slot) or raise."""
    if value is None:
        return None
    return _as_str(value)


# ── Closed key-sets (the from_json fail-closed allow-lists — PHI impossible) ──

#: The only keys :meth:`RiskWeights.from_json` accepts (``floor`` is rejected separately).
_RISK_WEIGHTS_KEYS: frozenset[str] = frozenset(
    {
        "weights_version",
        "c_map",
        "b_buckets",
        "p_levels",
        "t1",
        "t2",
        "auto_tuning_enabled",
        "provenance",
    },
)
#: The only keys :meth:`TierFields.from_json` accepts.
_TIER_FIELDS_KEYS: frozenset[str] = frozenset(
    {
        "change_class",
        "imports_touched_count",
        "precedent_surprise",
        "applicable_anchors_count",
        "has_open_questions",
        "human_bump",
    },
)
#: The only keys :meth:`PredictedBlock.from_json` accepts.
_PREDICTED_BLOCK_KEYS: frozenset[str] = frozenset(
    {"tier", "breakdown", "premortem_surprises", "anchors_to_watch"},
)
#: The only keys :meth:`ActualBlock.from_json` accepts.
_ACTUAL_BLOCK_KEYS: frozenset[str] = frozenset(
    {
        "gate_verdict",
        "review_blockers",
        "surprises_materialized",
        "surprises_missed",
        "anchors_moved_unexpected",
        "revise_cycles",
        "fix_first_cycles",
        "needed_deep",
    },
)
#: The only top-level keys :meth:`OutcomeRecord.from_json` accepts.
_OUTCOME_RECORD_KEYS: frozenset[str] = frozenset(
    {"scope_id", "merged_sha", "date", "risk_weights_version", "predicted", "actual"},
)
#: The only keys :meth:`PredictedManifest.from_json` accepts.
_PREDICTED_MANIFEST_KEYS: frozenset[str] = frozenset(
    {"scope_id", "risk_weights_version", "predicted"},
)
#: The only keys :meth:`AuditRow.from_json` accepts.
_AUDIT_ROW_KEYS: frozenset[str] = frozenset({"date", "applied", "decision"})


def _tier_breakdown_from_json(value: object) -> TierBreakdown:
    """Rebuild a :class:`TierBreakdown` from the uppercase dispatcher ``risk_breakdown`` shape."""
    data = _as_mapping(value)
    return TierBreakdown(
        c=_as_int(data["C"]),
        b=_as_int(data["B"]),
        p=_as_int(data["P"]),
        a=_as_int(data["A"]),
        s=_as_int(data["S"]),
        floor=_as_floor(data["floor"]),
        deep_t2=_as_bool(data["deep_T2"]),
    )


def _ratchet_decision_from_json(data: Mapping[str, object]) -> RatchetDecision:
    """Rebuild a :class:`RatchetDecision` from a parsed audit-row ``decision`` mapping."""
    direction_raw = data.get("direction")
    candidate_raw = data.get("candidate_weights")
    return RatchetDecision(
        disposition=Disposition(_as_str(data["disposition"])),
        knob=_as_opt_str(data.get("knob")),
        direction=Direction(_as_str(direction_raw)) if direction_raw is not None else None,
        candidate_weights=(
            RiskWeights.from_json(_as_mapping(candidate_raw)) if candidate_raw is not None else None
        ),
        backtest_clean=_as_bool(data["backtest_clean"]),
        knob_covered=_as_bool(data["knob_covered"]),
        cited_merged_shas=tuple(_as_str_list(data.get("cited_merged_shas"))),
        rationale=_as_str(data.get("rationale", "")),
        auto_applicable=_as_bool(data["auto_applicable"]),
    )


# ── Seed weights (reconciliation-pinned to scope_split._C_MAP) ────────────────

#: The seed change-class map — a **copy** of :data:`genome.scope_split.model._C_MAP` (the
#: dispatcher C-map). The reconciliation test pins ``SEED_RISK_WEIGHTS.c_map == _C_MAP`` so the
#: two stay byte-equal at the seed; they are defined independently (copied, not imported) so the
#: equality is a test, not a tautology.
_SEED_C_MAP: Mapping[str, int] = {
    "docs": 0,
    "tests": 1,
    "cli": 1,
    "data-backfill": 2,
    "annotation-loader": 2,
    "analysis": 2,
    "insights": 2,
    "pipeline": 3,
    "schema": 4,
    "ddl": 4,
}

#: The seed blast-radius band scores (the dispatcher ``B`` term).
_SEED_B_BUCKETS: Mapping[str, int] = {"isolated": 0, "small": 1, "moderate": 2, "large": 3}

#: The seed precedent-surprise category scores (the dispatcher ``P`` term).
_SEED_P_LEVELS: Mapping[str, int] = {"clean": 0, "minor": 1, "correction": 2}

#: The seed provenance block — provenance-only, no parent (the first epoch).
_SEED_PROVENANCE: Mapping[str, object] = {
    "source": "seed",
    "rationale": (
        "Initial seed — mirrors scope_split._C_MAP and the scope-dispatcher.md additive "
        "S = C + B + P formula (finding-040 / Gate-1 D1). Report-only: auto_tuning_enabled is "
        "false until the deterministic loop-closure test + the three safety fixes are green and "
        "VSC-User confirms the tier_in_hindsight default."
    ),
    "cited_outcomes": [],
    "parent_version": None,
}

#: The seed risk weights — the immutable reconciliation baseline + the back-test reference. The
#: live ``risk_weights.json`` is seeded byte-equal to this; the ratchet mutates the *file*, never
#: this constant. ``t1 = 1``, ``t2 = 5``, ``auto_tuning_enabled = False`` (report-only ship).
SEED_RISK_WEIGHTS: RiskWeights = RiskWeights(
    weights_version="rw-1",
    c_map=_SEED_C_MAP,
    b_buckets=_SEED_B_BUCKETS,
    p_levels=_SEED_P_LEVELS,
    t1=1,
    t2=5,
    auto_tuning_enabled=False,
    provenance=_SEED_PROVENANCE,
)


# ── Back-test fixture: the 6 historical rows (reproduce {0,1,1,1,2,2}) ─────────

#: The frozen regression fixture: six historical scopes whose ``compute_tier(·, SEED)`` must
#: reproduce ``{PR-8: 0, PR-12: 1, PR-6: 1, PR-7: 1, PR-5a: 2, PR-3: 2}`` (the dispatcher
#: back-test table in ``scope-dispatcher.md``). PR-5a / PR-3 sit at Tier 2 by the **immutable
#: anchor floor** (``applicable_anchors_count >= 1``), so no candidate weights set can move them —
#: the floor-faithfulness anchor. The four unfloored rows (PR-8 / PR-12 / PR-6 / PR-7) are the
#: only ones a knob change can move, and they define the per-knob coverage map.
BACKTEST_ROWS: tuple[BacktestRow, ...] = (
    BacktestRow(
        scope_id="PR-8",
        fields=TierFields(
            change_class=("docs",),
            imports_touched_count=0,
            precedent_surprise="clean",
            applicable_anchors_count=0,
        ),
        expected_tier=0,
    ),
    BacktestRow(
        scope_id="PR-12",
        fields=TierFields(
            change_class=("cli", "tests"),
            imports_touched_count=1,
            precedent_surprise="clean",
            applicable_anchors_count=0,
        ),
        expected_tier=1,
    ),
    BacktestRow(
        scope_id="PR-6",
        fields=TierFields(
            change_class=("data-backfill",),
            imports_touched_count=3,
            precedent_surprise="clean",
            applicable_anchors_count=0,
        ),
        expected_tier=1,
    ),
    BacktestRow(
        scope_id="PR-7",
        fields=TierFields(
            change_class=("data-backfill",),
            imports_touched_count=3,
            precedent_surprise="minor",
            applicable_anchors_count=0,
        ),
        expected_tier=1,
    ),
    BacktestRow(
        scope_id="PR-5a",
        fields=TierFields(
            change_class=("pipeline",),
            imports_touched_count=10,
            precedent_surprise="correction",
            applicable_anchors_count=2,
        ),
        expected_tier=2,
    ),
    BacktestRow(
        scope_id="PR-3",
        fields=TierFields(
            change_class=("pipeline",),
            imports_touched_count=20,
            precedent_surprise="correction",
            applicable_anchors_count=3,
        ),
        expected_tier=2,
    ),
)


# ── Direction-witness ladder: BACKTEST_ROWS + synthetic unfloored band witnesses ──

#: Synthetic UNFLOORED witnesses at the band boundaries ``S ∈ {t1-1, t1, t2-1, t2}`` (seed:
#: ``{0, 1, 4, 5}``). **Critical**: no real row sits at ``S >= t2`` *unfloored* (PR-7 is S4;
#: PR-5a / PR-3 are floored), so without the ``S = 5`` witness a ``t2`` raise yields zero tier
#: deltas and the loosen-inversion is invisible (plan §4 T1f). The ``S = 5`` witness
#: (``pipeline`` + small footprint + minor precedent, **no** anchors) is unfloored Tier 2, so
#: raising ``t2`` drops it to Tier 1 → a visible LOOSEN.
_SYNTHETIC_WITNESSES: tuple[TierFields, ...] = (
    TierFields(  # S = 0 (t1 - 1): unfloored Tier 0
        change_class=("docs",),
        imports_touched_count=0,
        precedent_surprise="clean",
        applicable_anchors_count=0,
    ),
    TierFields(  # S = 1 (t1): unfloored Tier 1 — a t1 raise drops it to Tier 0 (LOOSEN)
        change_class=("cli",),
        imports_touched_count=0,
        precedent_surprise="clean",
        applicable_anchors_count=0,
    ),
    TierFields(  # S = 4 (t2 - 1): unfloored Tier 1
        change_class=("data-backfill",),
        imports_touched_count=3,
        precedent_surprise="minor",
        applicable_anchors_count=0,
    ),
    TierFields(  # S = 5 (t2): unfloored Tier 2 — a t2 raise drops it to Tier 1 (LOOSEN)
        change_class=("pipeline",),
        imports_touched_count=3,
        precedent_surprise="minor",
        applicable_anchors_count=0,
    ),
)

#: The direction-witness ladder — the union of every :data:`BACKTEST_ROWS` input and the four
#: synthetic unfloored band witnesses. :func:`genome.calibration.ratchet.propose_ratchet` labels a
#: candidate LOOSEN iff ``compute_tier`` returns a lower tier for **any** ladder probe (a ``t1`` /
#: ``t2`` raise loosens), else TIGHTEN — direction by tier delta, never knob numeric sign.
DIRECTION_WITNESS_LADDER: tuple[TierFields, ...] = (
    *(row.fields for row in BACKTEST_ROWS),
    *_SYNTHETIC_WITNESSES,
)


# ── Per-knob coverage map (UNFLOORED rows exercising each tunable knob) ────────

#: Per tunable knob, the UNFLOORED :data:`BACKTEST_ROWS` scope ids that exercise it. An
#: **empty** tuple marks a PARK-ONLY knob — a tighten of it is clean *by vacuity* (no unfloored
#: row constrains it), so the back-test cannot vouch for it and it must be human-gated, never
#: auto-committed (plan §4 T1g / FIX-1). The nine empty knobs today are
#: ``c_map.{annotation-loader, analysis, insights, pipeline, schema, ddl}`` (pipeline/schema/ddl
#: appear only on floored rows), ``b_buckets.{moderate, large}`` (only floored rows), and
#: ``p_levels.correction`` (only floored rows). ``t1`` / ``t2`` are thresholds, not additive
#: knobs — their coverage is the direction-witness ladder, not this map.
KNOB_COVERAGE: Mapping[str, tuple[str, ...]] = {
    "c_map.docs": ("PR-8",),
    "c_map.tests": ("PR-12",),
    "c_map.cli": ("PR-12",),
    "c_map.data-backfill": ("PR-6", "PR-7"),
    "c_map.annotation-loader": (),
    "c_map.analysis": (),
    "c_map.insights": (),
    "c_map.pipeline": (),
    "c_map.schema": (),
    "c_map.ddl": (),
    "b_buckets.isolated": ("PR-8", "PR-12"),
    "b_buckets.small": ("PR-6", "PR-7"),
    "b_buckets.moderate": (),
    "b_buckets.large": (),
    "p_levels.clean": ("PR-8", "PR-12", "PR-6"),
    "p_levels.minor": ("PR-7",),
    "p_levels.correction": (),
}

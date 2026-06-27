"""Frozen vocabulary + data model for the scope-split smart-cut detector (``finding-039``).

This module is the source of truth for the splitter's closed change-class vocabulary
(:data:`CHANGE_CLASS_VOCAB`), the schema-first topological ordering
(:data:`SCHEMA_FIRST_ORDER`), the tunable threshold constants, and the frozen records the
fail-closed splitter consumes and emits (:class:`ScopeManifestInput`, :class:`SubScope`,
:class:`CutQuality`, :class:`SplitResult`). It also re-implements the dispatcher's risk-tier
formula **locally** (:func:`scope_S`, :func:`tier_from_S`, :func:`est_risk_tier`) so a
proposed sub-scope can be re-scored without re-running Stage-0.

It is **import-side-effect-free** and has **no** dependency on :mod:`genome.db` or any
database driver (plan §3): ``python -c "import genome.scope_split.model"`` must not import
DuckDB or SQLCipher. The DB-free guarantee is carried by the package-local
``test_scope_split_no_db_import.py`` clean-subprocess test.

Two vocabulary decisions are load-bearing:

* :data:`CHANGE_CLASS_VOCAB` mirrors the **dispatcher C-map** (scope-dispatcher.md), not the
  :mod:`genome.verify_gate.model` gate vocabulary — the splitter partitions by the same
  change-class boundaries Stage-0 emits. A reconciliation test keeps it pinned.
* The :func:`scope_S` / :func:`tier_from_S` / :func:`est_risk_tier` trio replicates the
  dispatcher's additive ``S = C + B + P`` score, ``tier_from_S`` banding, and the
  conservative ``max(floor, tier_from_S)`` floor (schema|ddl OR any anchor → Tier 2). It is
  re-implemented locally so the no-DB guard stays GREEN-from-freeze.

The pure constructors / serializers (:meth:`ScopeManifestInput.from_json`,
:meth:`SplitResult.to_json`, the narrowing helpers, the S-formula) and the behavioral splitter
(``genome.scope_split.splitter``) are all **implemented** (``finding-039``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from collections.abc import Mapping

# ── Closed change-class vocabulary (dispatcher C-map, not verify_gate gate vocab) ──

#: The change-class labels the splitter partitions by — the dispatcher C-map
#: (scope-dispatcher.md step 3 / the risk-tier C term), **not**
#: :data:`genome.verify_gate.model.CHANGE_CLASS_VOCAB`. A reconciliation test pins this to the
#: dispatcher labels so the splitter cuts on the same boundaries Stage-0 emits.
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

#: The schema-first topological order over change classes (lowest rank runs first). A
#: structural change (``schema`` / ``ddl``) must precede the loaders / pipeline that depend on
#: it; ``cli`` / ``tests`` / ``docs`` trail. Used by the splitter's TOPO-ORDER step to rank
#: candidate sub-scopes; a change class absent from this tuple sorts last (defensive).
SCHEMA_FIRST_ORDER: tuple[str, ...] = (
    "schema",
    "ddl",
    "annotation-loader",
    "data-backfill",
    "analysis",
    "insights",
    "pipeline",
    "cli",
    "tests",
    "docs",
)

# ── Tunable threshold constants ──────────────────────────────────────────────

#: Coupling-veto edge threshold (DECISION 1): a proposed cut that would sever an inter-cluster
#: import edge whose weight exceeds this is VETOED (the two clusters are fused). Also the
#: undecidable-low floor for :meth:`CouplingGraph.cut_cost`.
MAX_CUT_COST: float = 0.25

#: Minimum per-sub-scope shrink the quality gate requires: every proposed sub-scope's estimated
#: footprint must be at most this fraction of the parent (i.e. a real decomposition, not a
#: rename). A cut where any sub-scope fails this is rejected → atomic.
MIN_SUBSCOPE_SHRINK: float = 0.34

#: The minimum number of separable clusters a manifest must yield to be splittable. Fewer than
#: this (the common case — a tight blob) → atomic ("not separable by manifest").
MIN_CLUSTERS: int = 2

#: Re-split recursion cap (plan §7 out-of-scope: no recursive re-split beyond this). A
#: ``propose_split`` call at depth at or above this returns atomic.
MAX_RESPLIT_DEPTH: int = 1

#: A module imported by at least this many footprint modules is treated as shared infra
#: (DECISION 1): it is dropped from / down-weighted to 0 in the coupling graph so a common
#: dependency does not fuse otherwise-independent clusters into one component.
SHARED_HELPER_FANIN: int = 3


# ── Closed risk-tier domain ──────────────────────────────────────────────────

#: The closed set of risk tiers the dispatcher S-formula bands to (scope-dispatcher.md): 0, 1, or
#: 2. A ``Literal`` alias so ``mypy --strict`` rejects a stray tier at the computed-tier surface
#: (:func:`tier_from_S` / :func:`est_risk_tier` / :attr:`SubScope.est_risk_tier`). The raw manifest
#: input :attr:`ScopeManifestInput.risk_tier` stays a plain ``int`` — narrowing it would add a new
#: fail-closed check at :meth:`ScopeManifestInput.from_json`, a behavior change.
RiskTier = Literal[0, 1, 2]


# ── JSON serialization shapes (the typed to_json() output contracts) ──────────


# The SubScope.to_json shape.
class SubScopeJSON(TypedDict):
    sub_scope_id: str
    origin_scope: str
    change_class: list[str]
    est_imports_touched: int
    applicable_anchors: list[str]
    est_risk_tier: int
    depends_on: list[str]
    rationale: str


# The CutQuality.to_json shape.
class CutQualityJSON(TypedDict):
    cut_cost: float
    max_tier_before: int
    max_tier_after: int
    min_subscope_shrink: float
    clean: bool


# The atomic-branch SplitResult.to_json shape — EXACTLY these two keys (the check --json contract).
class AtomicResultJSON(TypedDict):
    atomic: bool
    reason: str


# The split-branch SplitResult.to_json shape (the full proposal).
class SplitProposalJSON(TypedDict):
    atomic: bool
    reason: str
    sub_scopes: list[SubScopeJSON]
    order: list[str]
    cut_quality: CutQualityJSON | None


# The nested ``blast_radius`` sub-object of ScopeManifestInput.to_json.
class _BlastRadiusJSON(TypedDict):
    imports_touched: list[str]
    tests_covering: list[str]


# The nested ``risk_breakdown`` sub-object (the dispatcher's capital-``S`` additive score).
class _RiskBreakdownJSON(TypedDict):
    S: int | None


# The ScopeManifestInput.to_json shape (the flattened splitter manifest).
class ManifestJSON(TypedDict):
    scope_id: str
    title: str
    change_class: list[str]
    depends_on: list[str]
    blast_radius: _BlastRadiusJSON
    applicable_anchors: list[str]
    out_of_scope_candidates: list[str]
    precedent: list[str]
    freshness_flags: list[str]
    open_questions: list[str]
    risk_tier: int
    risk_breakdown: _RiskBreakdownJSON


# ── Frozen records ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ScopeManifestInput:
    """The Stage-0 dispatcher manifest, narrowed to the fields the splitter consumes.

    Built from the live ``scope-dispatcher`` manifest JSON via :meth:`from_json` (the
    canonical seam — the manifest is threaded as in-prompt JSON, never a file). The nested
    dispatcher shape (``blast_radius.imports_touched``, ``risk_breakdown.S``) is flattened here
    so the splitter reads a flat record. :meth:`from_json` is **fail-closed** on a missing
    required field (plan arch-2).
    """

    scope_id: str
    """The dispatcher scope id (e.g. ``PR-6``); the ``origin_scope`` for every sub-scope."""
    change_class: tuple[str, ...] = ()
    """The (possibly multi-label) change classes — the primary partition signal (DECISION 1)."""
    title: str = ""
    """Human-readable slot title (echoed into rendered output; not decision-bearing)."""
    depends_on: tuple[str, ...] = ()
    """Upstream scope ids this scope depends on (feeds the TOPO-ORDER step)."""
    imports_touched: tuple[str, ...] = ()
    """The footprint modules (from ``blast_radius.imports_touched``) — the coupling-graph nodes
    and the per-cluster footprint estimate."""
    tests_covering: tuple[str, ...] = ()
    """Tests covering the footprint (from ``blast_radius.tests_covering``)."""
    applicable_anchors: tuple[str, ...] = ()
    """Real-data anchor names the scope exposes; drives the Tier-2 floor (:func:`est_risk_tier`)."""
    out_of_scope_candidates: tuple[str, ...] = ()
    """Named separable slices Stage-0 flagged — each refines the primary partition (DECISION 1)."""
    precedent: tuple[str, ...] = ()
    """Nearest-precedent finding ids (carried for traceability; not decision-bearing)."""
    freshness_flags: tuple[str, ...] = ()
    """Reading-list freshness warnings (carried through; not decision-bearing)."""
    open_questions: tuple[str, ...] = ()
    """Open questions needing human judgment (carried through; not decision-bearing)."""
    risk_tier: int = 0
    """The dispatcher's computed risk tier for the whole scope (the ``max_tier_before`` input)."""
    risk_score_S: int | None = None  # noqa: N815 - mirrors the dispatcher's capital-S score name
    """The dispatcher's additive ``S`` score (from ``risk_breakdown.S``), or ``None`` when the
    manifest omitted it (allowed - the splitter re-derives S per sub-scope via :func:`scope_S`)."""

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> ScopeManifestInput:
        """Build a :class:`ScopeManifestInput` from a parsed dispatcher-manifest mapping.

        Flattens the nested ``blast_radius`` / ``risk_breakdown`` sub-objects and narrows every
        field with no ``Any`` leak. **Fail-closed** (plan arch-2): ``scope_id``,
        ``change_class``, and ``blast_radius.imports_touched`` are required — a missing one
        raises :class:`ValueError` rather than silently defaulting to ``()``. ``risk_breakdown.S``
        is allowed to be absent (→ ``None``); the rest default to ``()`` / their scalar default.
        """
        scope_id_raw = data.get("scope_id")
        if scope_id_raw is None:
            msg = "manifest is missing required field 'scope_id'"
            raise ValueError(msg)
        scope_id = _as_str(scope_id_raw)

        if "change_class" not in data:
            msg = "manifest is missing required field 'change_class'"
            raise ValueError(msg)
        change_class = tuple(_as_str_list(data.get("change_class")))
        unknown = [label for label in change_class if label not in CHANGE_CLASS_VOCAB]
        if unknown:
            msg = (
                f"manifest change_class has unknown label(s) {unknown!r}; expected a subset of "
                f"{sorted(CHANGE_CLASS_VOCAB)!r}"
            )
            raise ValueError(msg)

        blast = _as_mapping(data.get("blast_radius"))
        if "imports_touched" not in blast:
            msg = "manifest is missing required field 'blast_radius.imports_touched'"
            raise ValueError(msg)
        imports_touched = tuple(_as_str_list(blast.get("imports_touched")))
        for entry in imports_touched:
            _validate_footprint_name(entry)
        tests_covering = tuple(_as_str_list(blast.get("tests_covering")))

        risk_breakdown = _as_mapping(data.get("risk_breakdown"))

        return cls(
            scope_id=scope_id,
            change_class=change_class,
            title=_as_str(data.get("title", "")),
            depends_on=tuple(_as_str_list(data.get("depends_on"))),
            imports_touched=imports_touched,
            tests_covering=tests_covering,
            applicable_anchors=tuple(_as_anchor_names(data.get("applicable_anchors"))),
            out_of_scope_candidates=tuple(_as_str_list(data.get("out_of_scope_candidates"))),
            precedent=tuple(_as_precedent_ids(data.get("precedent"))),
            freshness_flags=tuple(_as_str_list(data.get("freshness_flags"))),
            open_questions=tuple(_as_str_list(data.get("open_questions"))),
            risk_tier=_as_int(data.get("risk_tier", 0)),
            risk_score_S=_as_opt_int(risk_breakdown.get("S")),
        )

    def to_json(self) -> ManifestJSON:
        """Serialize to a JSON-ready mapping in the flattened splitter shape.

        Not a byte-exact inverse of the nested dispatcher manifest (the splitter flattens
        ``blast_radius`` / ``risk_breakdown``); :meth:`from_json` accepts this flattened shape
        back as well as the nested dispatcher shape.
        """
        return {
            "scope_id": self.scope_id,
            "title": self.title,
            "change_class": list(self.change_class),
            "depends_on": list(self.depends_on),
            "blast_radius": {
                "imports_touched": list(self.imports_touched),
                "tests_covering": list(self.tests_covering),
            },
            "applicable_anchors": list(self.applicable_anchors),
            "out_of_scope_candidates": list(self.out_of_scope_candidates),
            "precedent": list(self.precedent),
            "freshness_flags": list(self.freshness_flags),
            "open_questions": list(self.open_questions),
            "risk_tier": self.risk_tier,
            "risk_breakdown": {"S": self.risk_score_S},
        }


@dataclass(frozen=True, slots=True)
class SubScope:
    """One proposed sub-scope of a split (plan §5).

    A mini-manifest the human can lift straight into ``/scope-run``: the placeholder id
    (``<origin>-s1..sN`` in topo order), the originating scope (provenance, locked decision #8),
    the cluster's change classes and estimated footprint, and the re-scored risk tier (via
    :func:`est_risk_tier` on the cluster slice).
    """

    sub_scope_id: str
    """Placeholder id ``<origin>-s1..sN`` (topo order); not a minted PR-N (plan §7)."""
    origin_scope: str
    """The parent :attr:`ScopeManifestInput.scope_id` this sub-scope was carved from (locked #8)."""
    change_class: tuple[str, ...]
    """The change classes assigned to this cluster (a subset of the parent's)."""
    est_imports_touched: int
    """Estimated footprint — the count of footprint modules landing in this cluster."""
    applicable_anchors: tuple[str, ...]
    """Anchor names this sub-scope exposes (drives its re-scored Tier-2 floor)."""
    est_risk_tier: RiskTier
    """Re-scored risk tier for this cluster slice (:func:`est_risk_tier`)."""
    depends_on: tuple[str, ...]
    """Other sub-scope ids (or parent ``depends_on``) this one must follow (topo order)."""
    rationale: str
    """Human-readable justification for carving this sub-scope (rendered in the proposal)."""

    def to_json(self) -> SubScopeJSON:
        """Serialize to a JSON-ready mapping."""
        return {
            "sub_scope_id": self.sub_scope_id,
            "origin_scope": self.origin_scope,
            "change_class": list(self.change_class),
            "est_imports_touched": self.est_imports_touched,
            "applicable_anchors": list(self.applicable_anchors),
            "est_risk_tier": self.est_risk_tier,
            "depends_on": list(self.depends_on),
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class CutQuality:
    """The quality metrics of a proposed cut (plan §5).

    Carried on a non-atomic :class:`SplitResult` so the proposal can show *why* the cut passed
    the quality gate. ``clean`` is the conjunction the gate enforces: the cut survived the
    coupling veto, every sub-scope shrank enough, and the max tier did not rise.
    """

    cut_cost: float
    """The fraction of total edge weight severed by the cut (:meth:`CouplingGraph.cut_cost`)."""
    max_tier_before: int
    """The parent scope's risk tier (the ceiling the split must not exceed)."""
    max_tier_after: int
    """The maximum re-scored tier across the proposed sub-scopes."""
    min_subscope_shrink: float
    """The smallest per-sub-scope shrink ratio achieved (must be at least
    :data:`MIN_SUBSCOPE_SHRINK`)."""
    clean: bool
    """``True`` iff the cut passed every quality-gate term."""

    def to_json(self) -> CutQualityJSON:
        """Serialize to a JSON-ready mapping."""
        return {
            "cut_cost": self.cut_cost,
            "max_tier_before": self.max_tier_before,
            "max_tier_after": self.max_tier_after,
            "min_subscope_shrink": self.min_subscope_shrink,
            "clean": self.clean,
        }


@dataclass(frozen=True, slots=True)
class SplitResult:
    """The terminal result of :func:`genome.scope_split.splitter.propose_split` (plan §5).

    Two shapes, discriminated by :attr:`atomic`:

    * **atomic** — the scope is one indivisible unit. :attr:`sub_scopes` / :attr:`order` are
      empty and :attr:`cut_quality` is ``None``; :attr:`reason` records *why* (fail-closed: any
      uncertainty lands here). :meth:`to_json` emits exactly ``{"atomic": true, "reason": str}``.
    * **split** — a clean cut was found. :attr:`sub_scopes` are the proposed mini-manifests,
      :attr:`order` is their topo order of ids, :attr:`cut_quality` is the metrics.

    The SAFETY INVARIANT (mirrored in the splitter docstring): a non-atomic result is produced
    **only** when a cut passed every gate; any uncertainty fails closed to atomic.
    """

    atomic: bool
    """``True`` when the scope cannot / should not be split (the fail-closed default)."""
    reason: str
    """Human-readable justification (the failing-metric reason on atomic; cut summary on split)."""
    sub_scopes: tuple[SubScope, ...] = ()
    """The proposed sub-scopes (empty when atomic)."""
    order: tuple[str, ...] = ()
    """The topo order of :attr:`sub_scopes` ids (empty when atomic)."""
    cut_quality: CutQuality | None = None
    """The cut-quality metrics (``None`` when atomic)."""

    def __post_init__(self) -> None:
        """Reject the illegal states the two-shape contract forbids (B2; fail-closed culture).

        An ``atomic`` result must be empty (no sub-scopes, no order, no cut quality) — the
        atomic-blob :meth:`to_json` contract emits exactly ``{"atomic": true, "reason": str}``, so
        carrying sub-scopes on an atomic result is a latent serialization lie. A non-atomic result
        must carry at least one sub-scope (a split with zero sub-scopes is not a split). Either
        violation is a constructor bug → :class:`ValueError` (raised once at construction; frozen).
        """
        if self.atomic and (self.sub_scopes or self.order or self.cut_quality is not None):
            msg = (
                "atomic SplitResult must be empty: sub_scopes/order/cut_quality must be unset "
                f"(got {len(self.sub_scopes)} sub_scopes, {len(self.order)} order entries, "
                f"cut_quality={'set' if self.cut_quality is not None else 'None'})"
            )
            raise ValueError(msg)
        if not self.atomic and not self.sub_scopes:
            msg = "non-atomic SplitResult must carry at least one sub-scope"
            raise ValueError(msg)

    def to_json(self) -> AtomicResultJSON | SplitProposalJSON:
        """Serialize to a JSON-ready mapping (two-branch, mech #3).

        Atomic → exactly ``{"atomic": true, "reason": str}`` with no other keys (the
        ``check --json`` atomic-blob contract). Split → the full mapping with ``sub_scopes`` /
        ``order`` / ``cut_quality``.
        """
        if self.atomic:
            return {"atomic": True, "reason": self.reason}
        return {
            "atomic": False,
            "reason": self.reason,
            "sub_scopes": [s.to_json() for s in self.sub_scopes],
            "order": list(self.order),
            "cut_quality": self.cut_quality.to_json() if self.cut_quality is not None else None,
        }


# ── Local risk-tier formula (replicates scope-dispatcher.md exactly) ──────────

#: The dispatcher C-map: per-change-class change-class sub-score (scope-dispatcher.md "C").
_C_MAP: Mapping[str, int] = {
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

#: At or above this many distinct code concerns the dispatcher adds +1 to the C term.
_MULTI_CONCERN_THRESHOLD: int = 3

#: ``docs`` is the one non-code concern — excluded from the +1 multi-concern count.
_NON_CODE_CONCERNS: frozenset[str] = frozenset({"docs"})


def _c_score(change_class: tuple[str, ...]) -> int:
    """The dispatcher C term: max class sub-score, +1 when at least 3 distinct code concerns."""
    if not change_class:
        return 0
    base = max(_C_MAP.get(label, 0) for label in change_class)
    code_concerns = {label for label in change_class if label not in _NON_CODE_CONCERNS}
    if len(code_concerns) >= _MULTI_CONCERN_THRESHOLD:
        return base + 1
    return base


def _b_score(imports_touched: int) -> int:
    """The dispatcher B term from ``|imports_touched|``: <=1->0, 2-5->1, 6-15->2, >15->3."""
    if imports_touched <= 1:
        return 0
    if imports_touched <= 5:  # noqa: PLR2004 - dispatcher band boundary (small 2-5)
        return 1
    if imports_touched <= 15:  # noqa: PLR2004 - dispatcher band boundary (moderate 6-15)
        return 2
    return 3


def scope_S(  # noqa: N802 - mirrors the dispatcher's capital-S score name
    change_class: tuple[str, ...],
    imports_touched: int,
    precedent_surprise: int,
) -> int:
    """The dispatcher additive score ``S = C + B + P`` (scope-dispatcher.md).

    ``C`` from :func:`_c_score`, ``B`` from :func:`_b_score`, ``P`` is the
    precedent-surprise sub-score (``clean 0 · minor/noted 1 · correction-class 2``) passed in
    by the caller (the splitter defaults it to 0 for a re-scored sub-scope, which carries no
    new precedent of its own). ``A`` (anchor exposure) folds in only via the Tier-2 floor in
    :func:`est_risk_tier`, never into ``S`` - matching the dispatcher.
    """
    return _c_score(change_class) + _b_score(imports_touched) + precedent_surprise


def tier_from_S(s: int) -> RiskTier:  # noqa: N802 - mirrors the dispatcher's capital-S score name
    """Band the additive score to a tier: ``0->0, 1-4->1, >=5->2`` (scope-dispatcher.md)."""
    if s <= 0:
        return 0
    if s <= 4:  # noqa: PLR2004 - dispatcher band boundary (1<=S<=4 -> Tier 1)
        return 1
    return 2


def est_risk_tier(
    change_class: tuple[str, ...],
    applicable_anchors: tuple[str, ...],
    imports_touched: int,
    precedent_surprise: int = 0,
) -> RiskTier:
    """Re-score a (sub-)scope's risk tier via the dispatcher floor formula (scope-dispatcher.md).

    ``floor = 2`` iff a structural change (``schema`` / ``ddl`` in ``change_class``) **or** any
    anchor exposure (``len(applicable_anchors) >= 1``); else 0. The tier is the **conservative**
    ``max(floor, tier_from_S(S))`` — never the min. The pre-mortem / open-question +1 bump the
    dispatcher applies is *not* re-applied here (a sub-scope inherits no open questions of its
    own); it is the dispatcher's job on the parent.
    """
    structural = bool({"schema", "ddl"} & set(change_class))
    banded = tier_from_S(scope_S(change_class, imports_touched, precedent_surprise))
    # The dispatcher tier is the conservative max(floor, banded) where floor is 2 (structural or
    # anchor-exposed) else 0. Since banded ∈ {0,1,2}: max(2, banded) is always 2 and max(0, banded)
    # is always banded — so this is exactly that max, written as a RiskTier-returning branch (a
    # literal 2 / the RiskTier ``banded``) so mypy --strict does not widen a ``max(...)`` to int.
    if structural or len(applicable_anchors) >= 1:
        return 2
    return banded


# ── Footprint-name validation (the git-pathspec safety guard, W2 / phi-1) ─────

#: A footprint module name (dotted module OR repo-relative source path) must match this safe
#: charset before it can reach the ``git grep`` pathspec. Letters, digits, ``_``, ``.``, ``/``,
#: ``-`` only — and the first character may not be ``-`` (a leading dash is option-injection) or
#: any pathspec-magic character (``:`` introduces a ``:(magic)`` pathspec). Anchored end-to-end.
_FOOTPRINT_NAME_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./-]*$")


def _validate_footprint_name(name: str) -> None:
    """Reject a footprint name that is unsafe to hand to a ``git grep`` pathspec (W2 / phi-1).

    A footprint entry is either a dotted module (``genome.scope_split.model``) or a repo-relative
    path (``backend/src/genome/scope_split/model.py``). Anything outside :data:`_FOOTPRINT_NAME_RE`
    — an empty string, a leading ``-`` (option injection), a ``:`` (pathspec magic), whitespace, a
    shell metacharacter — raises :class:`ValueError` at the :meth:`ScopeManifestInput.from_json`
    boundary, so an unvalidated string never reaches the subprocess. Direct construction (the test
    seam) is deliberately left free of this check (the contract validates the JSON ingress only).
    """
    if not _FOOTPRINT_NAME_RE.match(name):
        msg = (
            f"manifest imports_touched entry {name!r} is not a safe dotted-module or "
            f"repo-relative path (allowed charset [A-Za-z0-9_./-], no leading '-' or ':')"
        )
        raise ValueError(msg)


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


def _as_anchor_names(value: object) -> list[str]:
    """Narrow ``applicable_anchors`` to a list of anchor *names*.

    The dispatcher emits anchors as objects ``{"name": ..., "value": ..., "src": ...}``; this
    accepts that shape (pulling ``name``) and also a bare ``list[str]`` of names. A list item
    that is neither a string nor an object with a string ``name`` raises.
    """
    out: list[str] = []
    for item in _as_obj_list(value):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out.append(_as_str(_as_mapping(item).get("name")))
        else:
            msg = f"expected an anchor name string or {{'name': ...}} object, got {item!r}"
            raise TypeError(msg)
    return out


def _as_precedent_ids(value: object) -> list[str]:
    """Narrow ``precedent`` to a list of finding *ids*.

    The dispatcher emits precedent as objects ``{"finding": ..., "surprise": ...}``; this
    accepts that shape (pulling ``finding``) and also a bare ``list[str]`` of ids.
    """
    out: list[str] = []
    for item in _as_obj_list(value):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out.append(_as_str(_as_mapping(item).get("finding")))
        else:
            msg = f"expected a precedent id string or {{'finding': ...}} object, got {item!r}"
            raise TypeError(msg)
    return out

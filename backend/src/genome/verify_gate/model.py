"""Frozen vocabulary + data model for the agentic verify-and-merge gate (Sub Project A).

This module is the source of truth for the gate's two closed verdict axes
(:class:`Verdict`, :class:`StepStatus`), the change-class vocabulary, and the frozen
records that carry every decidable check off the bash skill and into a unit-tested,
fail-closed core. It is **import-side-effect-free** and has **no** dependency on
:mod:`genome.db` or any database driver (plan §3 / §4.1): ``python -c
"import genome.verify_gate.model"`` must not import DuckDB or SQLCipher. The skill is
faithful plumbing whose only gate is "core exited non-zero → stop"; everything decidable
lives here so it can be tested.

Fail-closed is the governing design rule (plan §4.2): every boolean flag on
:class:`IntegrityFlags` defaults to its **non-affirmative** value, ``rebuild_pending``
defaults to ``True``, and an unrecognized / missing signal resolves to
:attr:`StepStatus.UNKNOWN` (never silently dropped). The reduction in
:mod:`genome.verify_gate.verdict` then turns any non-affirmative input into
``BLOCKED`` / ``UNKNOWN`` rather than ``GREEN``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# ── Closed change-class vocabulary ───────────────────────────────────────────

CHANGE_CLASS_VOCAB: frozenset[str] = frozenset({"core", "schema", "pipeline", "annotation"})
"""The four change classes the gate recognizes (plan §4.1). A single change may carry more
than one label (a schema change is also a pipeline change), so :data:`ChangeClass` is a
*set* of these, not a single tag. Membership is validated by :func:`assemble_check_set`."""

#: A change's class is the (possibly multi-label) subset of :data:`CHANGE_CLASS_VOCAB` it
#: belongs to — e.g. ``frozenset({"schema", "pipeline"})``. Pinned as ``frozenset[str]`` to
#: mirror the ``STATUS_VOCAB``/``KIND_VOCAB`` idiom in :mod:`genome.docs.model` and to let a
#: change declare several classes at once. The empty set is the "no class" (N/A) case.
ChangeClass = frozenset[str]

# ── Per-class required steps + anchor names (the verification.md capture columns) ──

#: The always-run dev-loop step labels — the tail of ``scripts/verify.sh`` every change runs.
#: Real verify.sh labels (interface, not implementation): a change of any class must clear
#: these before it is GREEN-eligible.
_DEV_LOOP_STEPS: tuple[str, ...] = (
    "uv sync",
    "pytest",
    "ruff check",
    "ruff format --check",
    "mypy --strict backend/src",
    "genome docs check",
)

#: Real-data anchor columns a ``pipeline`` change must capture — the headline ``genome merge``
#: outputs locked in verification.md's pipeline section / CLAUDE.md obs #3 (referenced by
#: column name only, never a transcribed magnitude).
_MERGE_ANCHORS: tuple[str, ...] = (
    "consensus_total",
    "both_concordant",
    "single_source",
    "imputed_only",
    "disagreement_resolved",
    "unresolvable",
)

#: Real-data anchor columns an ``annotation`` change must capture — the headline
#: ``genome annotate refresh-index`` outputs locked in verification.md's index section /
#: CLAUDE.md obs #4.
_INDEX_ANCHORS: tuple[str, ...] = (
    "gnomad_matches",
    "clinvar_matches",
    "gwas_matches",
    "pharmgkb_matches",
    "row_count",
    "curated_count",
)


# ── Closed verdict axes ──────────────────────────────────────────────────────


class Verdict(enum.Enum):
    """The gate's three-valued top-level verdict (plan §4.3).

    ``UNKNOWN`` dominates ``BLOCKED`` dominates ``GREEN``: an undecidable signal can never
    be reported as a clean pass, and a decided failure can never be masked by a pass. Only a
    fully-affirmative evidence package reduces to :attr:`GREEN`.
    """

    GREEN = "green"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class StepStatus(enum.Enum):
    """Outcome of one verification step, keyed on its process **exit code** (plan §4.2).

    ``0`` → :attr:`PASS`, any positive code → :attr:`FAIL`, a missing / non-numeric code →
    :attr:`UNKNOWN`. The status is never inferred from a stdout substring (a step that
    prints ``FAILED`` but exits ``0`` is a :attr:`PASS`); an unrecognized signal is surfaced
    as :attr:`UNKNOWN`, never dropped.
    """

    PASS = "pass"  # noqa: S105 — a pass/fail step status, not a credential
    FAIL = "fail"
    UNKNOWN = "unknown"


# ── Frozen records ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AnchorCheck:
    """One real-data anchor comparison (e.g. ``gnomad_matches`` expected vs captured).

    A check is GREEN-eligible only when it is **not** ``deferred`` and both ``expected`` and
    ``actual`` are present and equal. ``deferred`` marks an anchor that legitimately cannot
    be evaluated this run (a schema change pending a DB rebuild — plan §4.5); a ``None`` on
    either side of a non-deferred anchor (DB absent / stale) is undecidable and drives the
    verdict to :attr:`Verdict.UNKNOWN`, never to a fabricated pass.
    """

    name: str
    """Anchor identifier — a real captured column name (``gnomad_matches``, ``row_count``,
    ``consensus_total``), referenced by pointer, never a transcribed magnitude."""
    expected: str | None
    """The locked expected value (as a string), or ``None`` when no expectation is known."""
    actual: str | None
    """The captured value (as a string), or ``None`` when the DB was absent / not captured."""
    deferred: bool = False
    """``True`` when this anchor is intentionally not evaluated this run (schema rebuild
    pending). Defaults to ``False`` — an un-flagged anchor is held to the full match."""


@dataclass(frozen=True, slots=True)
class IntegrityFlags:
    """The non-anchor integrity signals the gate folds into the verdict (plan §4.2).

    **Every boolean defaults to its non-affirmative value** — this is the fail-closed
    contract. ``changelog_present`` / ``docs_check_clean`` default ``False`` (absence of
    proof is not proof); ``weakened_or_removed_test`` / ``gate_fill_survivor`` default
    ``True`` (assume the worst until a scan clears them). A package constructed with no
    arguments is therefore maximally un-GREEN.
    """

    changelog_present: bool = False
    """A ``[Unreleased]`` CHANGELOG entry was added for this change. Default ``False``."""
    docs_check_clean: bool = False
    """``genome docs check`` exited ``0`` (ledger + frontmatter valid). Default ``False``."""
    weakened_or_removed_test: bool = True
    """A test assertion was weakened or a test removed (scan result). Default ``True`` —
    fail-closed: a clean scan must explicitly set this ``False``."""
    gate_fill_survivor: bool = True
    """A deliberate ``GATE-FILL`` / placeholder sentinel survived into the diff. Default
    ``True`` — a clean grep must explicitly set this ``False``."""
    test_count_before: int | None = None
    """Collected test count before the change. ``None`` (default) is undecidable →
    :attr:`Verdict.UNKNOWN`."""
    test_count_after: int | None = None
    """Collected test count after the change. The delta must be non-negative (no net test
    loss); ``None`` (default) is undecidable → :attr:`Verdict.UNKNOWN`."""


@dataclass(frozen=True, slots=True)
class EvidencePackage:
    """The complete evidence bundle the gate reduces to a single :class:`Verdict`.

    Assembled Python-side by ``genome verify-gate assemble`` from flat primitive CLI args
    (the bash skill never builds nested JSON — plan §4.5 / R1), serialized to ``evidence.json``,
    and re-read by the ``verdict`` / ``format`` commands via :meth:`from_json`.
    """

    change_class: ChangeClass
    """The change's (possibly multi-label) class set; drives :func:`assemble_check_set`."""
    steps: tuple[tuple[str, StepStatus], ...]
    """Ordered ``(step_name, status)`` pairs — one per verification step that ran."""
    anchors: tuple[AnchorCheck, ...]
    """The real-data anchor comparisons. An empty tuple is the N/A path (this change-class
    has no applicable anchors) and is GREEN-eligible when every other signal is affirmative."""
    integrity: IntegrityFlags
    """The non-anchor integrity signals (CHANGELOG, docs check, test scan, count delta)."""
    rebuild_pending: bool = True
    """``True`` (default) when a schema rebuild is still owed before the numbers can be
    trusted — fail-closed, drives :attr:`Verdict.UNKNOWN` until explicitly cleared."""

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> EvidencePackage:
        """Build an :class:`EvidencePackage` from a parsed-JSON mapping (plan §4.5 / R1).

        Performs explicit per-field narrowing with no ``Any`` leak — strings are coerced
        back into :class:`StepStatus` members and the frozen records are reconstructed so
        ``mypy --strict`` stays clean across the serialization seam. The accepted shape is the
        one :func:`to_json` produces.
        """
        change_class: ChangeClass = frozenset(_as_str_list(data.get("change_class")))

        steps: list[tuple[str, StepStatus]] = []
        for item in _as_obj_list(data.get("steps")):
            pair = _as_obj_list(item)
            expected_pair_len = 2
            if len(pair) != expected_pair_len:
                msg = f"each step must be a [name, status] pair, got {item!r}"
                raise ValueError(msg)
            steps.append((_as_str(pair[0]), StepStatus(_as_str(pair[1]))))

        anchors: list[AnchorCheck] = []
        for item in _as_obj_list(data.get("anchors")):
            amap = _as_mapping(item)
            anchors.append(
                AnchorCheck(
                    name=_as_str(amap.get("name")),
                    expected=_as_opt_str(amap.get("expected")),
                    actual=_as_opt_str(amap.get("actual")),
                    deferred=_as_bool(amap.get("deferred", False)),
                ),
            )

        imap = _as_mapping(data.get("integrity"))
        integrity = IntegrityFlags(
            changelog_present=_as_bool(imap.get("changelog_present", False)),
            docs_check_clean=_as_bool(imap.get("docs_check_clean", False)),
            weakened_or_removed_test=_as_bool(imap.get("weakened_or_removed_test", True)),
            gate_fill_survivor=_as_bool(imap.get("gate_fill_survivor", True)),
            test_count_before=_as_opt_int(imap.get("test_count_before")),
            test_count_after=_as_opt_int(imap.get("test_count_after")),
        )

        return cls(
            change_class=change_class,
            steps=tuple(steps),
            anchors=tuple(anchors),
            integrity=integrity,
            rebuild_pending=_as_bool(data.get("rebuild_pending", True)),
        )

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping — the inverse of :meth:`from_json`.

        Enum members are written as their ``.value`` strings so the round-trip through
        ``json.dumps`` / ``json.loads`` is lossless and ``from_json`` can coerce them back.
        """
        return {
            "change_class": sorted(self.change_class),
            "steps": [[name, status.value] for name, status in self.steps],
            "anchors": [
                {
                    "name": a.name,
                    "expected": a.expected,
                    "actual": a.actual,
                    "deferred": a.deferred,
                }
                for a in self.anchors
            ],
            "integrity": {
                "changelog_present": self.integrity.changelog_present,
                "docs_check_clean": self.integrity.docs_check_clean,
                "weakened_or_removed_test": self.integrity.weakened_or_removed_test,
                "gate_fill_survivor": self.integrity.gate_fill_survivor,
                "test_count_before": self.integrity.test_count_before,
                "test_count_after": self.integrity.test_count_after,
            },
            "rebuild_pending": self.rebuild_pending,
        }


# ── Check-set assembly ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CheckSet:
    """The set of checks a given :data:`ChangeClass` requires (plan §4.1).

    Produced by :func:`assemble_check_set`: it pins which verification steps must run, which
    anchor names must be captured, and whether a schema rebuild is owed for this class — the
    template the skill fills in and the verdict reduction grades against.
    """

    change_class: ChangeClass
    """The change class this set was assembled for (echoed back for traceability)."""
    required_steps: tuple[str, ...] = ()
    """Names of the verification steps that must run and PASS for this class."""
    required_anchors: tuple[str, ...] = ()
    """Anchor names (real column names) that must be captured for this class. Empty = the
    N/A path (no real-data anchors apply)."""
    rebuild_required: bool = False
    """``True`` when this class (e.g. ``schema``) owes a DB rebuild before its anchors can be
    trusted — the assembler sets ``rebuild_pending`` and defers the anchors accordingly."""


def parse_step(name: str, exit_code: int | None) -> StepStatus:
    """Map a verification step's process exit code to a :class:`StepStatus` (plan §4.2).

    ``0`` → :attr:`StepStatus.PASS`, any positive code → :attr:`StepStatus.FAIL`, ``None``
    (step did not run / no numeric code) → :attr:`StepStatus.UNKNOWN`. Keyed on the exit code
    only — never on a stdout substring — so a step that prints ``FAILED`` but exits ``0`` is
    a PASS. ``name`` is carried for the diagnostic message only and does not affect the
    mapping.
    """
    # ``name`` is intentionally not consulted — the mapping is exit-code-only.
    _ = name
    if exit_code is None:
        return StepStatus.UNKNOWN
    return StepStatus.PASS if exit_code == 0 else StepStatus.FAIL


def assemble_check_set(change_class: ChangeClass) -> CheckSet:
    """Assemble the :class:`CheckSet` required by a change's class (plan §4.1).

    Validates every label against :data:`CHANGE_CLASS_VOCAB`, then folds the per-class
    requirements (steps, anchors, rebuild-owed) into one set. A ``schema`` class marks its
    anchors deferred and ``rebuild_required`` true; a class with no applicable anchors yields
    the N/A path (empty ``required_anchors``).
    """
    unknown = sorted(change_class - CHANGE_CLASS_VOCAB)
    if unknown:
        msg = f"unknown change class(es) {unknown}; valid classes are {sorted(CHANGE_CLASS_VOCAB)}"
        raise ValueError(msg)

    # Every change, whatever its class, must clear the dev-loop tail. Anchors and the
    # rebuild-owed flag are unioned across the labels in the (possibly multi-label) class.
    anchors: list[str] = []
    rebuild_required = False
    for label in sorted(change_class):
        if label == "pipeline":
            anchors.extend(a for a in _MERGE_ANCHORS if a not in anchors)
        elif label == "annotation":
            anchors.extend(a for a in _INDEX_ANCHORS if a not in anchors)
        elif label == "schema":
            # A schema change owes a DB rebuild before any number can be trusted; its own
            # anchors are deferred (re-derived only after the rebuild), so it contributes the
            # rebuild flag, not a capture set.
            rebuild_required = True
    return CheckSet(
        change_class=change_class,
        required_steps=_DEV_LOOP_STEPS,
        required_anchors=tuple(anchors),
        rebuild_required=rebuild_required,
    )


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

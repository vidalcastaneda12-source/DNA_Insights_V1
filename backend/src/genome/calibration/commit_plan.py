"""The git-write CommitPlan emitter — pure argv, never a subprocess (``finding-040``; T4).

git-WRITE has zero precedent in this codebase, so the Python core **never runs git**: it emits a
tested :class:`CommitPlan` whose ``argv_add`` / ``argv_commit`` are **both pathspec-scoped** to the
single git-tracked weights file (``git add -- <weights>`` / ``git commit -F - -- <weights>``) —
never ``-A`` / ``-u`` / ``.`` / a bare commit (plan §3 git-write-correction). The skill runs git
gated on the CLI exit code, asserts a clean index + on-C1-branch, then applies the plan. This
keeps the blast radius of an auto-commit to exactly one file by construction.

**No** :mod:`genome.db`, **no** :mod:`genome.config`, and **no** ``subprocess`` import — the
emitter is pure data, so it stays runnable on a fresh checkout and can never shell out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from genome.calibration.model import Disposition

if TYPE_CHECKING:
    from genome.calibration.model import RatchetDecision

#: The repo-relative pathspec of the one git-tracked weights file. Both ``argv_add`` and
#: ``argv_commit`` scope to exactly this path (after the ``--`` separator), so an auto-commit can
#: never stage anything else — the structural guard the commit-plan test pins.
WEIGHTS_PATHSPEC: str = "backend/src/genome/calibration/risk_weights.json"


@dataclass(frozen=True, slots=True)
class CommitPlan:
    """A pathspec-scoped git add + commit plan for an auto-committed weights change (plan §4 T4).

    Pure data: the skill executes these argv only after asserting a clean index + on-C1-branch.
    Both argv terminate the option list with ``--`` and name only :data:`WEIGHTS_PATHSPEC`, so the
    commit is provably single-file. ``message`` is the commit body (rationale + cited outcomes +
    back-test diff) piped via ``-F -``.
    """

    argv_add: tuple[str, ...]
    """``("git", "add", "--", WEIGHTS_PATHSPEC)`` — stage only the weights file."""
    argv_commit: tuple[str, ...]
    """``("git", "commit", "-F", "-", "--", WEIGHTS_PATHSPEC)`` — commit only the weights file,
    body on stdin."""
    message: str
    """The commit message body (rationale + cited-outcomes + back-test-diff) for ``-F -``."""

    def to_json(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping (for the CLI's machine output)."""
        return {
            "argv_add": list(self.argv_add),
            "argv_commit": list(self.argv_commit),
            "message": self.message,
        }


def render_commit_plan(decision: RatchetDecision) -> CommitPlan:
    """Render the pathspec-scoped :class:`CommitPlan` for an AUTO_COMMIT decision (plan §4 T4).

    Pure — runs **no** subprocess. Builds ``argv_add`` / ``argv_commit`` scoped to
    :data:`WEIGHTS_PATHSPEC` and a ``message`` from the decision's rationale + cited merged SHAs +
    new ``weights_version``. Only meaningful for a :attr:`~genome.calibration.model.Disposition.
    AUTO_COMMIT` decision; calling it on any other disposition raises :class:`ValueError` (the
    skill must never commit a parked / suppressed / no-op decision).
    """
    if decision.disposition is not Disposition.AUTO_COMMIT:
        msg = (
            f"render_commit_plan only renders an AUTO_COMMIT decision; got "
            f"{decision.disposition.value}"
        )
        raise ValueError(msg)
    # RatchetDecision.__post_init__ guarantees AUTO_COMMIT ⟹ candidate_weights is not None; this
    # narrows it for the type checker rather than falling back on a placeholder version.
    if decision.candidate_weights is None:  # pragma: no cover - unreachable by the invariant
        msg = "AUTO_COMMIT decision is missing candidate_weights"
        raise ValueError(msg)
    version = decision.candidate_weights.weights_version
    cited = ", ".join(decision.cited_merged_shas) if decision.cited_merged_shas else "(none)"
    message = (
        f"chore(calibration): auto-tune {decision.knob} → {version}\n\n"
        f"{decision.rationale}\n\n"
        f"cited outcomes: {cited}\n"
        f"back-test: clean · knob coverage: vetted\n"
        f"Reversible: revert this commit to restore the prior risk_weights.json."
    )
    return CommitPlan(
        argv_add=("git", "add", "--", WEIGHTS_PATHSPEC),
        argv_commit=("git", "commit", "-F", "-", "--", WEIGHTS_PATHSPEC),
        message=message,
    )

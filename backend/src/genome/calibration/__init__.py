"""Cross-run learning calibrator — the ``genome calibrate`` surface (``finding-040``).

The learning half of the per-scope agent team: it closes the ``predict → flag → confirm →
record`` loop with ``→ learn``. :func:`compute_tier` is the **single deterministic risk-tier
source of truth** (Gate-1 = Option D1) the dispatcher RUNS via
``genome calibrate compute-tier --manifest -`` and consumes; the outcome ledger
(:class:`OutcomeRecord`) records what actually happened at each merge; the asymmetric L3 ratchet
(:func:`propose_ratchet`) auto-applies a back-test-clean, unfloored-covered **tighten** and parks
every **loosen** (or clean-by-vacuity tighten) for one-click human approval. The trip-wire floors
(``schema | ddl`` or any anchor → Tier 2) are hard-coded and immutable — not representable in the
tunable :class:`RiskWeights`.

Ships **report-only**: ``SEED_RISK_WEIGHTS.auto_tuning_enabled`` is ``False`` so the machinery is
tested but the auto-tuning is dark until an auditable signoff.

**DB-free invariant.** This package imports **no** :mod:`genome.db` **and no** :mod:`genome.config`
— ``python -c "import genome.calibration"`` must run on a fresh checkout with no DuckDB / SQLCipher
built and no ``Settings`` loaded. The ``data/calibration/`` paths are hard-coded in
:mod:`genome.calibration.persistence` (mirroring ``fast_follow``), never sourced from
``get_settings``; the kill switch lives in ``risk_weights.json``, not ``Settings``. The guarantee
is carried by the package-local ``test_calibration_no_db_import.py`` clean-subprocess test, not by
lazy import — the ``genome`` root CLI registers ``calibration_app`` eagerly.
"""

from __future__ import annotations

from genome.calibration.accuracy import (
    per_knob_tally,
    premortem_precision_recall,
    tier_in_hindsight,
)
from genome.calibration.backtest import BacktestResult, run_backtest
from genome.calibration.cli import calibration_app
from genome.calibration.commit_plan import WEIGHTS_PATHSPEC, CommitPlan, render_commit_plan
from genome.calibration.formatter import (
    INSUFFICIENT_DATA_SENTINEL,
    format_calibration_report,
    format_ratchet_decision,
)
from genome.calibration.model import (
    BACKTEST_ROWS,
    BLAST_BAND_NAMES,
    DIRECTION_WITNESS_LADDER,
    KNOB_COVERAGE,
    PRECEDENT_SURPRISE_VOCAB,
    SEED_RISK_WEIGHTS,
    ActualBlock,
    AuditRow,
    BacktestRow,
    Direction,
    Disposition,
    OutcomeRecord,
    PredictedBlock,
    PredictedManifest,
    RatchetDecision,
    RiskWeights,
    TierBreakdown,
    TierFields,
    compute_tier,
)
from genome.calibration.persistence import (
    DEFAULT_AUDIT_PATH,
    DEFAULT_LEDGER_PATH,
    DEFAULT_MANIFEST_DIR,
    DEFAULT_WEIGHTS_PATH,
    append_audit,
    append_outcome,
    load_audit,
    load_outcomes,
    read_manifest,
    read_weights,
    write_manifest,
    write_weights,
)
from genome.calibration.ratchet import (
    CADENCE_MIN_MERGES,
    HYSTERESIS_MIN_RUNS,
    THIN_DATA_MIN_OUTCOMES,
    propose_ratchet,
)

__all__ = [
    "BACKTEST_ROWS",
    "BLAST_BAND_NAMES",
    "CADENCE_MIN_MERGES",
    "DEFAULT_AUDIT_PATH",
    "DEFAULT_LEDGER_PATH",
    "DEFAULT_MANIFEST_DIR",
    "DEFAULT_WEIGHTS_PATH",
    "DIRECTION_WITNESS_LADDER",
    "HYSTERESIS_MIN_RUNS",
    "INSUFFICIENT_DATA_SENTINEL",
    "KNOB_COVERAGE",
    "PRECEDENT_SURPRISE_VOCAB",
    "SEED_RISK_WEIGHTS",
    "THIN_DATA_MIN_OUTCOMES",
    "WEIGHTS_PATHSPEC",
    "ActualBlock",
    "AuditRow",
    "BacktestResult",
    "BacktestRow",
    "CommitPlan",
    "Direction",
    "Disposition",
    "OutcomeRecord",
    "PredictedBlock",
    "PredictedManifest",
    "RatchetDecision",
    "RiskWeights",
    "TierBreakdown",
    "TierFields",
    "append_audit",
    "append_outcome",
    "calibration_app",
    "compute_tier",
    "format_calibration_report",
    "format_ratchet_decision",
    "load_audit",
    "load_outcomes",
    "per_knob_tally",
    "premortem_precision_recall",
    "propose_ratchet",
    "read_manifest",
    "read_weights",
    "render_commit_plan",
    "run_backtest",
    "tier_in_hindsight",
    "write_manifest",
    "write_weights",
]

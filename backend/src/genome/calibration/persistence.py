"""Append-only ledger / audit / manifest I/O + the live-weights reader-writer (``finding-040``).

All calibration state lives on the filesystem — **no** calibration tables in either DB (plan §3
D1-two-db). The outcome ledger and ratchet audit are append-only JSONL under the gitignored
``data/calibration/``; the dispatch-time predicted store is one JSON file per scope under
``data/calibration/manifests/``; the tunable weights are the **git-tracked**
``risk_weights.json`` beside this package.

Paths are **hard-coded** ``Path('data/...')`` (and a package-relative weights path), mirroring
``genome.fast_follow.persistence`` — this module **never** calls ``get_settings`` and imports
neither :mod:`genome.db` nor :mod:`genome.config`, so the calibrator stays importable on a fresh
checkout. ``data/`` is the gitignored runtime-state home (CLAUDE.md); the relative paths assume a
repo-root cwd, the same assumption ``fast_follow`` makes (now spanning the dispatch → close two
stages — plan v2.1 minor_2).

Semantics (mirroring ``fast_follow``): loads are **empty-on-absent** (a missing ledger is not an
error — first run); a **malformed** file RAISES rather than silently returning empty (which would
lose history); appends **mkdir the parent** then write. :func:`write_weights` is the **only**
writer of the live config and bumps the version + provenance.

All eight I/O functions are implemented; the hard-coded ``data/...`` paths + the package-relative
weights path are the frozen contract the plan-blind tests are written against.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from genome.calibration.model import (
    AuditRow,
    OutcomeRecord,
    PredictedManifest,
    RiskWeights,
)

logger = structlog.get_logger(__name__)

#: The append-only outcome ledger (plan §4 T5) — the source of truth the ratchet reduces.
DEFAULT_LEDGER_PATH: Path = Path("data/calibration/outcomes.jsonl")

#: The append-only ratchet audit log (plan §3 D8) — every auto-change / park / suppress, reviewable.
DEFAULT_AUDIT_PATH: Path = Path("data/calibration/ratchet_audit.jsonl")

#: The dispatch-time predicted store (plan §4 T5 / FIX-3) — one ``<scope_id>.json`` per dispatch,
#: read back at A's close so ``write-outcome`` sources the predicted block the dispatcher used.
DEFAULT_MANIFEST_DIR: Path = Path("data/calibration/manifests")

#: The **git-tracked** live weights config (plan §3 D1-two-db). Package-relative (``__file__``), not
#: cwd-relative, because it ships with the source; the ratchet's CommitPlan stages the repo-relative
#: ``backend/src/genome/calibration/risk_weights.json`` pathspec.
DEFAULT_WEIGHTS_PATH: Path = Path(__file__).resolve().parent / "risk_weights.json"


def load_outcomes(path: Path = DEFAULT_LEDGER_PATH) -> list[OutcomeRecord]:
    """Read the append-only outcome ledger into a list of records (plan §4 T5).

    Returns an empty list when the file is absent (first run). One JSON object per line; a
    malformed line raises (via :meth:`~genome.calibration.model.OutcomeRecord.from_json`) rather
    than silently dropping history.
    """
    if not path.exists():
        logger.info("calibration.outcomes.absent", path=str(path))
        return []
    records = [
        OutcomeRecord.from_json(json.loads(line))
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip())
    ]
    logger.info("calibration.outcomes.loaded", path=str(path), count=len(records))
    return records


def append_outcome(record: OutcomeRecord, path: Path = DEFAULT_LEDGER_PATH) -> None:
    """Append one :class:`~genome.calibration.model.OutcomeRecord` as a JSONL line (plan §4 T5).

    Creates the parent directory if needed, then appends ``record.to_json()`` as one line. Never
    rewrites or truncates the ledger.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_json()) + "\n")
    logger.info("calibration.outcomes.appended", path=str(path), scope_id=record.scope_id)


def load_audit(path: Path = DEFAULT_AUDIT_PATH) -> list[AuditRow]:
    """Read the append-only ratchet audit log into a list of rows (plan §3 D8).

    Empty-on-absent; a malformed line raises rather than silently dropping audit history.
    """
    if not path.exists():
        logger.info("calibration.audit.absent", path=str(path))
        return []
    rows = [
        AuditRow.from_json(json.loads(line))
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip())
    ]
    logger.info("calibration.audit.loaded", path=str(path), count=len(rows))
    return rows


def append_audit(row: AuditRow, path: Path = DEFAULT_AUDIT_PATH) -> None:
    """Append one :class:`~genome.calibration.model.AuditRow` as a JSONL line (plan §3 D8)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row.to_json()) + "\n")
    logger.info("calibration.audit.appended", path=str(path), applied=row.applied)


def write_manifest(
    manifest: PredictedManifest,
    manifest_dir: Path = DEFAULT_MANIFEST_DIR,
) -> Path:
    """Persist the dispatch-time predicted manifest to ``<manifest_dir>/<scope_id>.json`` (FIX-3).

    Creates ``manifest_dir`` if needed and writes ``manifest.to_json()``. Returns the written
    path. This is the loop-closure + write-hook-feed seam: ``compute-tier --persist`` writes it at
    dispatch; ``write-outcome`` reads it at close.
    """
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / f"{manifest.scope_id}.json"
    path.write_text(json.dumps(manifest.to_json(), indent=2) + "\n", encoding="utf-8")
    logger.info("calibration.manifest.wrote", path=str(path), scope_id=manifest.scope_id)
    return path


def read_manifest(
    scope_id: str,
    manifest_dir: Path = DEFAULT_MANIFEST_DIR,
) -> PredictedManifest | None:
    """Read back the dispatch-time predicted manifest for ``scope_id`` (plan §4 T5 / FIX-3).

    Returns the parsed :class:`~genome.calibration.model.PredictedManifest`, or ``None`` when no
    manifest was persisted for this scope — the **visible-drop** signal ``write-outcome`` turns
    into a stderr warning + ``exit 0`` + no ledger append (never a corrupt row).
    """
    path = manifest_dir / f"{scope_id}.json"
    if not path.exists():
        logger.info("calibration.manifest.absent", path=str(path), scope_id=scope_id)
        return None
    return PredictedManifest.from_json(json.loads(path.read_text(encoding="utf-8")))


def read_weights(path: Path = DEFAULT_WEIGHTS_PATH) -> RiskWeights:
    """Read the live tunable weights from the git-tracked ``risk_weights.json`` (plan §4 T5).

    The single reader the dispatcher's ``compute-tier`` and ``show-weights`` consume. A malformed
    config (including a forbidden ``"floor"`` key) raises via
    :meth:`~genome.calibration.model.RiskWeights.from_json`.
    """
    return RiskWeights.from_json(json.loads(path.read_text(encoding="utf-8")))


def write_weights(weights: RiskWeights, path: Path = DEFAULT_WEIGHTS_PATH) -> None:
    """Write the live weights config — the **only** writer of the live knobs (plan §4 T5).

    Serializes ``weights.to_json()`` to ``path``. The caller (the ratchet apply path) is
    responsible for having bumped ``weights_version`` + recorded provenance; this function does
    not mutate the weights, only persists them.
    """
    path.write_text(json.dumps(weights.to_json(), indent=2) + "\n", encoding="utf-8")
    logger.info("calibration.weights.wrote", path=str(path), version=weights.weights_version)

"""TopMed Imputation Server interactions, built on the audited HTTP client.

TopMed's free tier does not provide a programmatic upload API for user data —
that step happens through their web UI. This module covers everything else:

* Polling a job's status from the URL the user copies out of the web UI.
* Downloading the encrypted result archive once the job completes.

Both flow through :class:`genome.privacy.external_client.ExternalClient`, so
every interaction lands in ``audit_log``.

Status URL format
-----------------

TopMed (Cloudgene-based) jobs expose an API endpoint of the form
``https://imputation.biodatacatalyst.nhlbi.nih.gov/api/v2/jobs/<job_id>``.
The user lifts this URL from the web UI's "job details" page. We accept it
verbatim.

Response shape (per the Cloudgene API):
``{"id": ..., "state": ..., "currentStep": ..., "completedAt": ..., ...}``.
``state`` is the field we map to our ``ImputationStatus`` enum:

* ``1`` / ``2`` / ``3`` → ``processing`` (queued / running / running)
* ``4`` → ``completed``
* ``5`` / ``6`` → ``failed``

The exact values are documented in Cloudgene's source. We treat unknown values
as ``failed`` and log loudly so a TopMed API change does not silently advance
a job past completion.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self

import structlog

from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.imputation.archive import ImputationArchive, restrict_file
from genome.imputation.runs import (
    ImputationStatus,
    fetch_run,
    record_download,
    update_status,
)
from genome.privacy.external_client import (
    ExternalCallError,
    ExternalClient,
)

if TYPE_CHECKING:
    from pathlib import Path

    import httpx


logger = structlog.get_logger(__name__)

TOPMED_ENDPOINT_LABEL: Final[str] = "topmed"
"""Used for ``audit_log.external_endpoint`` on every TopMed call."""

TOPMED_PANEL: Final[str] = "topmed_r3"


# Cloudgene state codes. Mapping is authoritative for the Cloudgene 2.x
# series that TopMed runs as of writing; we re-evaluate when the API ever
# returns a value outside this set (see ``_state_to_status`` for the
# unknown-value path).
_CLOUDGENE_STATE_PROCESSING: Final[frozenset[int]] = frozenset({1, 2, 3})
_CLOUDGENE_STATE_COMPLETED: Final[int] = 4
_CLOUDGENE_STATE_FAILED: Final[frozenset[int]] = frozenset({5, 6})


@dataclass(frozen=True, slots=True)
class TopMedStatus:
    """Parsed status response, normalized to our enum."""

    status: ImputationStatus
    raw_state: int | str | None
    job_id: str | None
    completed_at: str | None
    """ISO-8601 timestamp if TopMed reports one, else None."""


def _state_to_status(state: int | str | None) -> ImputationStatus:  # noqa: PLR0911 — explicit branches map TopMed states to our enum
    """Map TopMed's ``state`` field to our enum.

    Treats unknown integer values and unknown string values as ``failed`` so
    a silently-changed server doesn't advance our state machine past
    completion. The caller logs the raw value either way so the user can
    investigate.
    """
    if state is None:
        return "failed"
    try:
        as_int = int(state)
    except (TypeError, ValueError):
        # TopMed has historically returned both integer codes and string labels.
        normalized = str(state).strip().lower()
        if normalized in {"running", "queued", "pending"}:
            return "processing"
        if normalized in {"success", "completed", "ok"}:
            return "completed"
        return "failed"
    if as_int in _CLOUDGENE_STATE_PROCESSING:
        return "processing"
    if as_int == _CLOUDGENE_STATE_COMPLETED:
        return "completed"
    if as_int in _CLOUDGENE_STATE_FAILED:
        return "failed"
    return "failed"


class TopMedClient:
    """Thin wrapper around :class:`ExternalClient` for TopMed-shaped calls.

    Construct with ``TopMedClient(...)`` for production, or pass an injected
    ``httpx.Client`` for tests (paired with ``httpx.MockTransport``).
    """

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        profile_id: int | None = None,
    ) -> None:
        self._client = ExternalClient(
            endpoint_label=TOPMED_ENDPOINT_LABEL,
            client=http_client,
            profile_id=profile_id,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch_status(self, status_url: str, *, imputation_id: int) -> TopMedStatus:
        """GET the status URL and return a parsed :class:`TopMedStatus`.

        Network / HTTP / parse errors raise
        :class:`genome.privacy.external_client.ExternalCallError` (or a subclass);
        the audit row is already written. The caller (see :func:`check_status`)
        decides whether to mark the run as failed in the DB on a parse error,
        or whether to leave it alone and let the user retry.
        """
        response = self._client.request(
            "GET",
            status_url,
            resource_type="imputation_run",
            resource_id=str(imputation_id),
            action_type="read",
        )
        return _parse_status_response(response)

    def download_archive(
        self,
        download_url: str,
        dest: Path,
        *,
        imputation_id: int,
    ) -> str:
        """Stream the encrypted result archive to ``dest`` and return its SHA-256."""
        return self._client.download(
            download_url,
            str(dest),
            resource_type="imputation_run",
            resource_id=str(imputation_id),
            action_type="export",
        )


def _parse_status_response(response: httpx.Response) -> TopMedStatus:
    """Extract ``status``, ``raw_state``, ``job_id``, and ``completed_at`` from one response.

    Defensive against shape changes: any missing field is ``None``. The only
    field that drives the state machine is ``state`` (or ``status``), and that
    has its own unknown-value path in :func:`_state_to_status`.
    """
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        msg = f"could not parse TopMed status response as JSON: {exc}"
        raise ExternalCallError(msg) from exc

    if not isinstance(payload, dict):
        msg = f"TopMed status response was not a JSON object: {type(payload).__name__}"
        raise ExternalCallError(msg)

    raw_state = payload.get("state")
    if raw_state is None:
        # Some Cloudgene installs return the label in `status` instead.
        raw_state = payload.get("status")

    job_id = payload.get("id")
    completed_at = payload.get("completedAt") or payload.get("completed_at")

    coerced_state: int | str | None
    coerced_state = raw_state if isinstance(raw_state, (int, str)) else None

    return TopMedStatus(
        status=_state_to_status(coerced_state),
        raw_state=coerced_state,
        job_id=None if job_id is None else str(job_id),
        completed_at=None if completed_at is None else str(completed_at),
    )


def check_status(
    imputation_id: int,
    *,
    status_url: str,
    duckdb_path: Path | None = None,
    client: TopMedClient | None = None,
) -> TopMedStatus:
    """Poll TopMed for a job and update ``imputation_runs.status`` accordingly.

    Idempotence: re-running on a run that's already ``completed`` or ``failed``
    just re-fetches and re-applies the same status (no-op update). The user can
    safely re-run this command repeatedly without side effects.

    Transitions written to the DB:

    * Any non-failed response moves the run to ``processing`` if it wasn't
      already and stamps ``submitted_at``.
    * ``completed`` stamps ``completed_at``.
    * ``failed`` keeps the failure reason in audit_log; the DB row's status
      is updated but the failure detail lives only in the audit row.
    """
    settings = get_settings()
    db_path = duckdb_path or settings.genome_duckdb_path

    with duckdb_connection(db_path) as conn:
        run = fetch_run(conn, imputation_id)
        if run is None:
            msg = f"imputation_id {imputation_id} not found"
            raise ValueError(msg)

    log = logger.bind(imputation_id=imputation_id, current_status=run.status)
    log.info("imputation.status.poll")

    owned_client = client is None
    cli = client if client is not None else TopMedClient()
    try:
        status = cli.fetch_status(status_url, imputation_id=imputation_id)
    finally:
        if owned_client:
            cli.close()

    log.info("imputation.status.response", new_status=status.status, raw_state=status.raw_state)

    with duckdb_connection(db_path) as conn:
        # Once the user has supplied a status URL we know they've submitted to
        # TopMed — stamp submitted_at if it isn't set. completed_at is only
        # stamped on a 'completed' transition.
        update_status(
            conn,
            imputation_id,
            status=status.status,
            set_submitted=True,
            set_completed=status.status == "completed",
        )
    return status


def download_result(  # noqa: PLR0913 — narrow surface, every parameter is needed
    imputation_id: int,
    *,
    download_url: str,
    password: str,  # noqa: ARG001 — accepted for runbook symmetry; see body
    duckdb_path: Path | None = None,
    archive_root: Path | None = None,
    client: TopMedClient | None = None,
) -> Path:
    """Download the encrypted result archive to the run's archive directory.

    ``password`` is accepted (and required) so the CLI surface mirrors the
    runbook step — the user passes the TopMed-supplied AES-256 password.
    Decryption itself is a manual step the user performs locally (with 7zip
    or unzip-aes); the password never leaves this process via an audit row.
    Storing it in memory while the download runs is unavoidable. The runbook
    documents the decryption workflow.

    Idempotence: if the encrypted archive already exists and its SHA-256
    matches what's stored on ``imputation_runs.output_file_hash_sha256``, the
    download is skipped and the existing path is returned.

    Pre-conditions:

    * Run must be in ``status='completed'``. Calling on any other status
      raises ``RuntimeError`` — the runbook calls out the status-check step
      before download for exactly this reason.
    """
    settings = get_settings()
    db_path = duckdb_path or settings.genome_duckdb_path
    archive_root = archive_root or settings.archive_path

    with duckdb_connection(db_path) as conn:
        run = fetch_run(conn, imputation_id)
        if run is None:
            msg = f"imputation_id {imputation_id} not found"
            raise ValueError(msg)

    if run.status != "completed":
        msg = (
            f"imputation_id {imputation_id} is in status {run.status!r}; "
            f"only 'completed' runs can be downloaded. Run "
            f"`genome imputation status {imputation_id}` first."
        )
        raise RuntimeError(msg)

    archive = ImputationArchive.for_run(archive_root, imputation_id)
    archive.ensure_layout()
    dest = archive.encrypted_archive

    if dest.exists() and run.output_file_hash_sha256:
        # Re-check the existing file's hash; if it matches, skip the download.
        from genome.privacy.external_client import _hash_file as _hash_file_fn  # noqa: PLC0415

        existing_hash = _hash_file_fn(str(dest))
        if existing_hash == run.output_file_hash_sha256:
            logger.info(
                "imputation.download.skip",
                imputation_id=imputation_id,
                reason="archive already downloaded with matching hash",
            )
            return dest

    owned_client = client is None
    cli = client if client is not None else TopMedClient()
    try:
        digest = cli.download_archive(download_url, dest, imputation_id=imputation_id)
    except ExternalCallError:
        # Remove partial file so a retry starts fresh; ignore missing file.
        with contextlib.suppress(FileNotFoundError):
            dest.unlink()
        raise
    finally:
        if owned_client:
            cli.close()
    restrict_file(dest)

    with duckdb_connection(db_path) as conn:
        record_download(
            conn,
            imputation_id,
            output_file_path=str(dest),
            output_file_hash_sha256=digest,
        )

    # Store the SHA-256 alongside for convenience; the password is intentionally
    # NOT stored — the user keeps it themselves per the runbook.
    archive.download_metadata.write_text(
        f"sha256: {digest}\n"
        f"archive_path: {dest}\n"
        "decrypt the archive with the AES-256 password TopMed emailed you.\n",
    )
    restrict_file(archive.download_metadata)
    return dest

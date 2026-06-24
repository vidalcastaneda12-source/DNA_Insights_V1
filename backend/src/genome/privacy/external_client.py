"""Audited external HTTP client.

Every external network call this app performs flows through here. The client:

* Enforces the master switch ``user_preferences.external_calls_enabled``.
* Hashes the request body (SHA-256) and stores the hash, **never the body**, in
  ``audit_log``. Per `CLAUDE.md`: "Never store the body of an external request —
  only the hash."
* Writes one ``audit_log`` row per attempt (success **or** failure). Retries
  appear as additional rows; never as updates.
* Returns the parsed response to the caller. Network / HTTP / parse errors
  raise :class:`ExternalCallError`.

The client is intentionally generic. Phase 4 used it for the (now-removed)
TopMed flow and currently sees use only for the Phase 4 reference-panel
downloads (Beagle JAR, PLINK genetic map, 1000G Phase 3 panel VCFs); Phase 5
will use it for MyVariant.info, PubMed, and any other external lookups.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final, Literal, Self

import httpx
import structlog

from genome.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from sqlite3 import Connection

logger = structlog.get_logger(__name__)

HttpMethod = Literal["GET", "POST", "PUT", "DELETE", "HEAD"]
"""HTTP verbs the client supports. Add more as new external endpoints arrive."""

CallStatus = Literal["success", "failure", "blocked"]

_DEFAULT_TIMEOUT_S: Final[float] = 30.0
_HASH_CHUNK_SIZE: Final[int] = 1 << 20  # 1 MiB


class ExternalCallError(RuntimeError):
    """Raised when an external call fails for any reason.

    The audit log already records the failure with ``operation_details``
    containing the cause; this exception is what the caller sees. Callers may
    retry — each retry is a fresh call and produces its own audit row.
    """


class ExternalCallsDisabledError(ExternalCallError):
    """Raised when an external call is attempted while the master switch is off.

    The message is intentionally actionable: it names the preference key and
    the CLI command that toggles it.
    """

    def __init__(self) -> None:
        super().__init__(
            "External calls are disabled. To enable, run "
            "`genome config set external_calls_enabled true` "
            "(or update `user_preferences.external_calls_enabled` to 'true').",
        )


def _hash_bytes(payload: bytes) -> str:
    """SHA-256 hex of arbitrary bytes."""
    return hashlib.sha256(payload).hexdigest()


def _hash_file(path: str) -> str:
    """SHA-256 hex of a file's contents, streamed to avoid loading large files."""
    h = hashlib.sha256()
    with open(path, "rb") as f:  # noqa: PTH123 — explicit open keeps the type narrow
        while True:
            chunk = f.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _serialize_payload(
    body: bytes | str | Mapping[str, Any] | None,
    *,
    json_body: Mapping[str, Any] | None,
) -> bytes:
    """Reduce the caller's body to bytes for hashing. Empty body hashes the empty string."""
    if json_body is not None:
        return json.dumps(json_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(dict(body), sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_external_calls_enabled(conn: Connection) -> bool:
    """Read the master switch out of ``user_preferences``.

    Falls back to ``False`` if the row is missing — fail-closed is the right
    default for any privacy-relevant setting.
    """
    row = conn.execute(
        "SELECT pref_value FROM user_preferences WHERE pref_key = ?",
        ("external_calls_enabled",),
    ).fetchone()
    if row is None:
        return False
    return str(row[0]).strip().lower() == "true"


def _insert_audit_row(  # noqa: PLR0913 — schema fields are not collapsible
    conn: Connection,
    *,
    action_type: str,
    resource_type: str,
    resource_id: str | None,
    operation_details: Mapping[str, Any],
    external_endpoint: str,
    external_payload_hash: str,
    profile_id: int | None,
) -> int:
    """Insert one row into ``audit_log`` and return its ``log_id``.

    ``external_call`` is always ``1`` here — this helper is reserved for the
    audited HTTP path. Non-network audit rows (config changes, snapshots, etc.)
    should use a sibling helper rather than passing ``external_call=False``,
    so the schema's privacy intent stays obvious at the call site.
    """
    cur = conn.execute(
        """
        INSERT INTO audit_log (
            profile_id, action_type, resource_type, resource_id,
            operation_details, external_call, external_endpoint,
            external_payload_hash
        )
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        [
            profile_id,
            action_type,
            resource_type,
            resource_id,
            json.dumps(dict(operation_details), sort_keys=True),
            external_endpoint,
            external_payload_hash,
        ],
    )
    conn.commit()
    log_id = cur.lastrowid
    if log_id is None:
        msg = "audit_log insert returned no lastrowid"
        raise RuntimeError(msg)
    return int(log_id)


def write_config_change_audit(
    *,
    pref_key: str,
    old_value: str | None,
    new_value: str,
    profile_id: int | None = None,
) -> int:
    """Record a ``user_preferences`` change in ``audit_log``.

    The audit row's ``external_call`` is ``0`` — a config change is local and
    does not transmit data. ``operation_details`` carries the key plus old and
    new values so the change is reversible from the log alone.
    """
    payload = json.dumps(
        {"pref_key": pref_key, "old_value": old_value, "new_value": new_value},
        sort_keys=True,
    )
    from genome.db.sqlite_conn import sqlcipher_connection  # noqa: PLC0415

    with sqlcipher_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO audit_log (
                profile_id, action_type, resource_type, resource_id,
                operation_details, external_call
            )
            VALUES (?, 'config_change', 'user_preference', ?, ?, 0)
            """,
            [profile_id, pref_key, payload],
        )
        conn.commit()
        log_id = cur.lastrowid
    if log_id is None:
        msg = "audit_log insert returned no lastrowid"
        raise RuntimeError(msg)
    return int(log_id)


@contextmanager
def _open_audit_db() -> Iterator[Connection]:
    """Open the audit DB for the duration of one call attempt.

    Each call attempt opens, writes its row(s), and closes. Keeping the
    connection scoped to a single call means a long-running HTTP request never
    holds a lock on ``app.db``.
    """
    from genome.db.sqlite_conn import sqlcipher_connection  # noqa: PLC0415

    with sqlcipher_connection() as conn:
        yield conn


@contextmanager
def _audited_attempt(  # noqa: PLR0913 — every parameter is meaningful audit metadata
    *,
    endpoint: str,
    payload_hash: str,
    resource_type: str,
    resource_id: str | None,
    method: HttpMethod,
    action_type: str,
    profile_id: int | None,
) -> Iterator[None]:
    """Wrap one HTTP attempt with an intent-then-result audit pair.

    On entry: writes an ``intent`` audit row, then checks the master switch.
    If external calls are disabled, writes a second ``blocked`` result row and
    raises :class:`ExternalCallsDisabledError`. Recording the intent *before*
    the enabled-check means blocked attempts — arguably the most important
    privacy events to capture — leave a database trace, not just stdout.

    On exit (when the call ran): writes a second audit row recording
    ``success`` or ``failure`` with the exception text (when applicable). Both
    rows share the same payload hash, so a log reader can group an intent /
    outcome pair by ``(endpoint, payload_hash, timestamp)``.

    The two-row pattern means an attempt that crashes mid-flight (e.g. process
    SIGKILL) still leaves the intent row, so the user can see *something* was
    attempted even if no completion row landed.
    """
    started = time.monotonic()
    with _open_audit_db() as conn:
        _insert_audit_row(
            conn,
            action_type=action_type,
            resource_type=resource_type,
            resource_id=resource_id,
            operation_details={"method": method, "phase": "intent"},
            external_endpoint=endpoint,
            external_payload_hash=payload_hash,
            profile_id=profile_id,
        )
        enabled = _read_external_calls_enabled(conn)

    if not enabled:
        duration_ms = int((time.monotonic() - started) * 1000)
        with _open_audit_db() as conn:
            _insert_audit_row(
                conn,
                action_type=action_type,
                resource_type=resource_type,
                resource_id=resource_id,
                operation_details={
                    "method": method,
                    "phase": "result",
                    "status": "blocked",
                    "blocked": True,
                    "duration_ms": duration_ms,
                },
                external_endpoint=endpoint,
                external_payload_hash=payload_hash,
                profile_id=profile_id,
            )
        raise ExternalCallsDisabledError

    error: BaseException | None = None
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 — we re-raise after recording
        error = exc

    duration_ms = int((time.monotonic() - started) * 1000)
    status: CallStatus = "failure" if error is not None else "success"
    details: dict[str, Any] = {
        "method": method,
        "phase": "result",
        "status": status,
        "duration_ms": duration_ms,
    }
    if error is not None:
        details["error_type"] = type(error).__name__
        details["error"] = str(error)[:500]

    with _open_audit_db() as conn:
        _insert_audit_row(
            conn,
            action_type=action_type,
            resource_type=resource_type,
            resource_id=resource_id,
            operation_details=details,
            external_endpoint=endpoint,
            external_payload_hash=payload_hash,
            profile_id=profile_id,
        )

    if error is not None:
        raise error


class ExternalClient:
    """Audited HTTP client. Constructable from settings or with an injected ``httpx.Client``.

    Inject a custom ``httpx.Client`` in tests (paired with ``httpx.MockTransport``)
    to exercise success / failure paths without touching the network. In
    production code the default constructor is right.
    """

    def __init__(
        self,
        endpoint_label: str,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        profile_id: int | None = None,
    ) -> None:
        """Build a client bound to one logical ``endpoint_label`` (e.g. ``'1000g_panel'``).

        ``endpoint_label`` is what lands in ``audit_log.external_endpoint`` —
        keep it short and stable. The full URL goes into the request line and
        is not stored.
        """
        self._endpoint_label = endpoint_label
        self._timeout_s = timeout_s
        self._profile_id = profile_id
        self._client = client if client is not None else httpx.Client(timeout=timeout_s)
        self._owns_client = client is None

    @property
    def endpoint_label(self) -> str:
        """The label used for ``audit_log.external_endpoint`` on every audited call."""
        return self._endpoint_label

    def close(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request(  # noqa: PLR0913 — narrow surface, every parameter is meaningful
        self,
        method: HttpMethod,
        url: str,
        *,
        body: bytes | str | Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str | int] | None = None,
        resource_type: str,
        resource_id: str | None = None,
        action_type: str = "read",
    ) -> httpx.Response:
        """Perform one audited HTTP call.

        The body (or ``json_body``) is hashed and recorded; the raw value never
        leaves this function. The response is returned verbatim to the caller.

        Raises :class:`ExternalCallError` (or a subclass) on any failure;
        callers retry by calling :meth:`request` again, which records a new
        audit pair.
        """
        payload_bytes = _serialize_payload(body, json_body=json_body)
        payload_hash = _hash_bytes(payload_bytes)

        log = logger.bind(
            endpoint=self._endpoint_label,
            method=method,
            payload_hash=payload_hash[:12],
        )
        log.info("external.attempt")

        with _audited_attempt(
            endpoint=self._endpoint_label,
            payload_hash=payload_hash,
            resource_type=resource_type,
            resource_id=resource_id,
            method=method,
            action_type=action_type,
            profile_id=self._profile_id,
        ):
            try:
                response = self._client.request(
                    method=method,
                    url=url,
                    content=payload_bytes or None,
                    headers=dict(headers) if headers else None,
                    params=dict(params) if params else None,
                )
            except httpx.HTTPError as exc:
                msg = f"network error contacting {self._endpoint_label}: {exc}"
                raise ExternalCallError(msg) from exc

            if response.status_code >= 400:  # noqa: PLR2004 — HTTP error band starts at 400
                msg = (
                    f"HTTP {response.status_code} from {self._endpoint_label} "
                    f"({method} {url}): {response.text[:200]}"
                )
                raise ExternalCallError(msg)

        log.info("external.success", status=response.status_code)
        return response

    def download(  # noqa: PLR0913 — narrow keyword-only surface mirrors `request`
        self,
        url: str,
        dest: str,
        *,
        resource_type: str,
        resource_id: str | None = None,
        action_type: str = "read",
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """Stream a binary response to ``dest`` and return its SHA-256.

        Used for large artifacts (per-chromosome 1000G Phase 3 reference VCFs
        and the PLINK genetic-map archive are each hundreds of MB to GBs).
        Memory usage stays constant — bytes are written straight to disk.
        Returns the hex-encoded SHA-256 of the saved file so the caller can
        record provenance.
        """
        log = logger.bind(endpoint=self._endpoint_label, url_hash=_hash_bytes(url.encode())[:12])
        log.info("external.download.start")
        url_hash = _hash_bytes(url.encode("utf-8"))

        with _audited_attempt(
            endpoint=self._endpoint_label,
            payload_hash=url_hash,
            resource_type=resource_type,
            resource_id=resource_id,
            method="GET",
            action_type=action_type,
            profile_id=self._profile_id,
        ):
            try:
                with self._client.stream(
                    "GET",
                    url,
                    headers=dict(headers) if headers else None,
                ) as response:
                    if response.status_code >= 400:  # noqa: PLR2004 — HTTP error band
                        # The streaming context defers body consumption, so
                        # ``response.text`` raises ``ResponseNotRead`` until
                        # ``response.read()`` runs. Drain explicitly before
                        # building the message so the error surfaces the
                        # actual body snippet rather than masking it with
                        # an unrelated exception.
                        try:
                            response.read()
                            body_snippet = response.text[:200]
                        except Exception:  # noqa: BLE001 — defensive: never let snippet capture mask the HTTP error
                            body_snippet = "<unavailable>"
                        msg = (
                            f"HTTP {response.status_code} downloading from "
                            f"{self._endpoint_label}: {body_snippet}"
                        )
                        raise ExternalCallError(msg)
                    with open(dest, "wb") as out:  # noqa: PTH123
                        out.writelines(response.iter_bytes(chunk_size=_HASH_CHUNK_SIZE))
            except httpx.HTTPError as exc:
                msg = f"network error downloading from {self._endpoint_label}: {exc}"
                raise ExternalCallError(msg) from exc

        digest = _hash_file(dest)
        log.info("external.download.complete", sha256=digest[:12])
        return digest


def is_external_enabled() -> bool:
    """Return the live value of ``user_preferences.external_calls_enabled``.

    Useful for CLI commands that want to fail early with a helpful message
    rather than letting an :class:`ExternalCallsDisabledError` bubble up from
    deep inside a workflow.

    Note: ``Settings.external_calls_enabled`` reflects the ``.env`` value at
    process startup; this function reads ``user_preferences`` which is the
    authoritative runtime source.
    """
    # Ensure settings is loaded so the app.db path is resolved correctly.
    get_settings()
    from genome.db.sqlite_conn import sqlcipher_connection  # noqa: PLC0415

    with sqlcipher_connection() as conn:
        return _read_external_calls_enabled(conn)


__all__ = [
    "CallStatus",
    "ExternalCallError",
    "ExternalCallsDisabledError",
    "ExternalClient",
    "HttpMethod",
    "is_external_enabled",
    "write_config_change_audit",
]

"""Privacy-sensitive infrastructure.

:mod:`genome.privacy.external_client` is the single audited path for every
network call this app makes. Every external request goes through it; every
request writes ``audit_log`` rows with a payload hash (never the payload).
"""

from genome.privacy.external_client import (
    ExternalCallError,
    ExternalCallsDisabledError,
    ExternalClient,
    is_external_enabled,
    write_config_change_audit,
)

__all__ = [
    "ExternalCallError",
    "ExternalCallsDisabledError",
    "ExternalClient",
    "is_external_enabled",
    "write_config_change_audit",
]

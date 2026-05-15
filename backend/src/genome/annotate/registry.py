"""Per-source loader registry.

5.0 ships the empty registry. 5.1+ each register one loader by importing
its module so the side-effect ``register_loader(...)`` call lands at
import time. ``genome annotate refresh --source X`` looks the loader up
here.

Registration is idempotent on ``(source_db, fn)`` — registering the same
function twice for the same source is a no-op so module imports stay
safe. Registering a *different* function for an already-registered
source raises :class:`RuntimeError`; that almost always means two
modules are trying to claim the same source label.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import structlog

from genome.annotate.source_versions import KNOWN_SOURCE_DBS

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Outcome of one loader's ``refresh`` call."""

    source_db: str
    source_version_id: int
    version: str
    record_count: int
    was_already_current: bool


RefreshFn = Callable[[bool], RefreshResult]
"""Per-source refresh callable.

The single ``bool`` argument is ``force``. Loaders are free to honour
or ignore it depending on their semantics; ``force=True`` typically
means "re-download the upstream artifact and reload regardless of the
cached state".
"""

_LOADERS: dict[str, RefreshFn] = {}


def register_loader(source_db: str, fn: RefreshFn) -> None:
    """Register a per-source loader.

    * Rejects ``source_db`` not in :data:`KNOWN_SOURCE_DBS`.
    * Idempotent on ``(source_db, fn)``: registering the same function
      twice is a no-op so import-time registration is safe across
      repeated imports.
    * Conflicting registration of two *different* functions for the
      same ``source_db`` raises :class:`RuntimeError`.
    """
    if source_db not in KNOWN_SOURCE_DBS:
        msg = f"unknown source_db {source_db!r}; expected one of {sorted(KNOWN_SOURCE_DBS)}"
        raise ValueError(msg)
    existing = _LOADERS.get(source_db)
    if existing is fn:
        return
    if existing is not None:
        msg = (
            f"loader for source_db {source_db!r} is already registered "
            f"to a different function ({existing!r})"
        )
        raise RuntimeError(msg)
    _LOADERS[source_db] = fn
    logger.debug("annotate.registry.register", source_db=source_db)


def get_loader(source_db: str) -> RefreshFn | None:
    """Return the registered loader for ``source_db`` or ``None``."""
    return _LOADERS.get(source_db)


def known_loaders() -> frozenset[str]:
    """Return the set of currently-registered ``source_db`` labels.

    Empty in 5.0; one entry per 5.1+ source added thereafter.
    """
    return frozenset(_LOADERS)


def _clear_loaders_for_testing() -> None:
    """Reset the registry to empty. Test-only helper.

    Tests that register/unregister loaders should use this in teardown
    (or via ``monkeypatch.setattr`` on the module's ``_LOADERS`` dict)
    so other tests run against a clean slate.
    """
    _LOADERS.clear()


__all__ = [
    "RefreshFn",
    "RefreshResult",
    "get_loader",
    "known_loaders",
    "register_loader",
]

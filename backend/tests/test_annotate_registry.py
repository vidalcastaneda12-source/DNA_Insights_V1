"""Tests for :mod:`genome.annotate.registry`.

Verifies the per-source loader registry — empty in 5.0, but exercised
here with a fake :class:`RefreshFn` so the contract (register, get,
list, idempotence, conflict rejection, unknown-source rejection) is
locked in before 5.1+ start populating it.
"""

from __future__ import annotations

import pytest

from genome.annotate.registry import (
    RefreshFn,
    RefreshResult,
    _clear_loaders_for_testing,
    get_loader,
    known_loaders,
    register_loader,
)


def _make_fake_loader(label: str) -> RefreshFn:
    def _refresh(
        force: bool,  # noqa: ARG001, FBT001 — protocol signature
        skip_if_same_version: bool,  # noqa: ARG001, FBT001 — protocol signature
    ) -> RefreshResult:
        return RefreshResult(
            source_db=label,
            source_version_id=1,
            version="2026_05_15",
            record_count=0,
            was_already_current=False,
        )

    return _refresh


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Reset the loader registry between tests so order doesn't matter."""
    _clear_loaders_for_testing()


def test_register_and_get_loader_round_trip() -> None:
    fn = _make_fake_loader("clinvar")
    register_loader("clinvar", fn)
    assert get_loader("clinvar") is fn
    assert known_loaders() == frozenset({"clinvar"})


def test_get_loader_returns_none_for_unregistered_source() -> None:
    assert get_loader("clinvar") is None


def test_register_loader_is_idempotent_for_same_function() -> None:
    fn = _make_fake_loader("clinvar")
    register_loader("clinvar", fn)
    register_loader("clinvar", fn)  # second call is a no-op
    assert get_loader("clinvar") is fn
    assert known_loaders() == frozenset({"clinvar"})


def test_register_loader_rejects_conflicting_function_for_same_source() -> None:
    register_loader("clinvar", _make_fake_loader("clinvar"))
    with pytest.raises(RuntimeError, match="already registered"):
        register_loader("clinvar", _make_fake_loader("different"))


def test_register_loader_rejects_unknown_source_db() -> None:
    with pytest.raises(ValueError, match="unknown source_db"):
        register_loader("not_a_real_source", _make_fake_loader("x"))


def test_known_loaders_is_frozenset_of_registered_labels() -> None:
    register_loader("clinvar", _make_fake_loader("clinvar"))
    register_loader("gwas_catalog", _make_fake_loader("gwas_catalog"))
    result = known_loaders()
    assert isinstance(result, frozenset)
    assert result == frozenset({"clinvar", "gwas_catalog"})


def test_known_loaders_is_empty_at_baseline() -> None:
    """5.0 ships no registered loaders.

    The autouse ``_reset_registry`` fixture mimics a fresh process, so
    this assertion documents the 5.0 baseline rather than the runtime
    state during a real session.
    """
    assert known_loaders() == frozenset()

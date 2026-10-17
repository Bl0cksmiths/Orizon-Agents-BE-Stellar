"""The guard's own `version()` read, for a client build without `sc.escrow_version`.

`escrow_version` prefers the client's shared read; these pin the fallback that
runs when it is absent: the same contract (a missing function is 1, a definite
answer is cached, anything else is a 503 that is never remembered) and the
exact warning each refusal logs. Faked at `sc.simulate_read`; nothing reaches
testnet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from escrow_fakes import ESCROW, MISSING_FUNCTION, FakeEscrow, install, use_escrow

from app.services import authorization_guard as guard
from app.stellar import client as sc


@pytest.fixture(autouse=True)
def without_shared_read(monkeypatch: pytest.MonkeyPatch) -> Any:
    use_escrow(monkeypatch, ESCROW)
    monkeypatch.delattr(sc, "escrow_version", raising=False)
    guard.forget_versions()
    yield
    guard.forget_versions()


def refused(caplog: pytest.LogCaptureFixture) -> str:
    with caplog.at_level(logging.WARNING, logger=guard.__name__), pytest.raises(guard.AuthorizationRefused) as exc:
        asyncio.run(guard.escrow_version())
    assert (exc.value.status_code, exc.value.code) == (503, "authorization_unreadable")
    assert guard._versions == {}
    return caplog.records[-1].getMessage()


def test_a_missing_function_is_version_one_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    escrow = install(monkeypatch, FakeEscrow(version=None))
    assert asyncio.run(guard.escrow_version()) == 1
    assert asyncio.run(guard.escrow_version()) == 1
    assert escrow.calls == [(ESCROW, "version")]
    assert guard._versions == {ESCROW: 1}


def test_a_definite_version_is_returned_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    escrow = install(monkeypatch, FakeEscrow(version=2))
    assert asyncio.run(guard.escrow_version()) == 2
    assert asyncio.run(guard.escrow_version()) == 2
    assert escrow.calls == [(ESCROW, "version")]
    assert guard._versions == {ESCROW: 2}


def test_a_cached_version_is_not_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    escrow = install(monkeypatch, FakeEscrow(version=RuntimeError("never read")))
    guard._versions[ESCROW] = 3
    assert asyncio.run(guard.escrow_version()) == 3
    assert escrow.calls == []


def test_a_simulation_error_that_is_not_a_missing_function_is_503(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install(monkeypatch, FakeEscrow(version=RuntimeError("simulate failed: HostError: Error(Budget, ExceededLimit)")))
    assert refused(caplog) == (
        f"authorization unreadable: PaymentEscrow {ESCROW} version() failed: HostError: Error(Budget, ExceededLimit)"
    )


def test_missing_value_without_the_missing_function_cause_is_503(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    head_only = MISSING_FUNCTION.split("\n")[0]
    install(monkeypatch, FakeEscrow(version=RuntimeError(head_only)))
    assert refused(caplog).endswith("version() failed: HostError: Error(WasmVm, MissingValue)")


def test_the_missing_function_cause_without_its_head_is_503(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install(monkeypatch, FakeEscrow(version=RuntimeError("simulate failed: non-existent contract function")))
    assert refused(caplog).endswith("version() failed: non-existent contract function")


@pytest.mark.parametrize("failure", [ConnectionError("rpc down"), TimeoutError()])
def test_a_failed_read_is_503_naming_only_its_type(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    install(monkeypatch, FakeEscrow(version=failure))
    assert refused(caplog) == (
        f"authorization unreadable: PaymentEscrow {ESCROW} version() failed: {type(failure).__name__}"
    )


@pytest.mark.parametrize("value", [0, -2, True, "2", None, 1.0])
def test_a_nonsense_version_is_503(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: Any
) -> None:
    monkeypatch.setattr(sc, "simulate_read", lambda *a, **k: value)
    assert refused(caplog) == f"authorization unreadable: PaymentEscrow {ESCROW} version() returned {value!r}"

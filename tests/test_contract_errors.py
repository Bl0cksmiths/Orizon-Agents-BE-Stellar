"""A contract's own rejection, carried as a code rather than as text.

When ReputationLedger refuses a rating it answers with a value of its `Error`
enum — Unauthorized, Replay, OutOfRange, NotFound — and the simulation error
that reports it opens with "HostError: Error(Contract, #N)" before running on
into the full diagnostic event log. The code is what a caller needs; the text
is what it must never repeat. Nothing here touches the network.
"""

from __future__ import annotations

from app.stellar import client as sc

# Verbatim head of a real testnet simulation of ReputationLedger.submit from a
# caller that is not the Scorer (public chain data, truncated in the middle of
# the event log exactly as far as a caller would ever need).
UNAUTHORIZED_SIMULATION_ERROR = (
    "HostError: Error(Contract, #1)\n\nEvent log (newest first):\n   0: [Diagnostic Event] "
    "contract:CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT, topics:[error, Error(Contract, #1)], "
    'data:"escalating Ok(ScErrorType::Contract) frame-exit to Err"\n   1: [Diagnostic Event] '
    "topics:[fn_call, CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT, submit], data:[GD6K…]"
)


# ── reading the code off a simulation error ─────────────────────


def test_the_live_unauthorized_rejection_parses_to_its_code():
    assert sc._contract_error_code(UNAUTHORIZED_SIMULATION_ERROR) == 1


def test_every_ledger_error_code_parses():
    for code in (1, 2, 7, 100):
        assert sc._contract_error_code(f"HostError: Error(Contract, #{code})\n\nEvent log") == code


def test_a_host_error_that_is_not_the_contracts_has_no_code():
    """A budget overrun or a trap is the host's failure, not a contract
    verdict, and must not be named as one."""
    assert sc._contract_error_code("HostError: Error(Budget, ExceededLimit)\n\nEvent log") is None
    assert sc._contract_error_code("HostError: Error(WasmVm, InvalidAction)") is None


def test_only_the_head_names_the_failure():
    """The event log beneath the head can quote other contract errors; the
    call failed with the one at the top, so a code quoted further down is
    not evidence of anything."""
    assert sc._contract_error_code("transaction simulation failed; see Error(Contract, #7) in log") is None


def test_no_error_text_has_no_code():
    assert sc._contract_error_code(None) is None
    assert sc._contract_error_code("") is None

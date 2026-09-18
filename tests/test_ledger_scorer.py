"""Reading ReputationLedger's stored Scorer off the chain.

`ReputationLedger.submit` accepts a rating only from its Scorer, and the
contract has no view that returns it — so whether this deployment can write
ratings at all turns on a value that lives only in the contract's instance
storage. These pin the decode of that entry, against the real testnet entry
as well as built ones, and the one RPC call that fetches it. Nothing here
touches the network.
"""

from __future__ import annotations

from stellar_sdk import Address, Keypair, StrKey, scval, xdr
from stellar_sdk.xdr import SCVal, SCValType

from app.stellar import client as sc

# The live testnet ReputationLedger's instance entry, exactly as
# getLedgerEntries returned it (public chain data): Admin and Scorer are both
# GA7AI5…5OQV. Kept verbatim so the decoder is proven against the encoding
# soroban-sdk actually produced, not only against one this file built.
TESTNET_LEDGER_INSTANCE_XDR = (
    "AAAABgAAAAAAAAABxScElc0fDNemrwflYPkS2mtpV8f5Gh5Z2TNLl3wb7c0AAAAUAAAAAQAAABMAAAAAL8SWWtQY2mEDINxpxUT3fOy5357Z"
    "MMWRuMIZGyfHQ5sAAAABAAAAAgAAABAAAAABAAAAAQAAAA8AAAAFQWRtaW4AAAAAAAASAAAAAAAAAAA+BHZgSTINfR73hySLjKJwElqYa3Nn"
    "0/HTmg+jMKu9HgAAABAAAAABAAAAAQAAAA8AAAAGU2NvcmVyAAAAAAASAAAAAAAAAAA+BHZgSTINfR73hySLjKJwElqYa3Nn0/HTmg+jMKu9"
    "Hg=="
)
TESTNET_SCORER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"

LEDGER_ID = StrKey.encode_contract(b"\x07" * 32)
ADMIN = Keypair.from_raw_ed25519_seed(b"\x01" * 32).public_key
SCORER = Keypair.from_raw_ed25519_seed(b"\x02" * 32).public_key


def _key(variant: str) -> SCVal:
    return scval.to_vec([scval.to_symbol(variant)])


def _instance_entry(storage: list[tuple[SCVal, SCVal]] | None) -> str:
    """A contract-instance LedgerEntryData, base64, with the given storage."""
    instance = xdr.SCContractInstance(
        executable=xdr.ContractExecutable(
            xdr.ContractExecutableType.CONTRACT_EXECUTABLE_WASM, wasm_hash=xdr.Hash(b"\x00" * 32)
        ),
        storage=None if storage is None else xdr.SCMap([xdr.SCMapEntry(k, v) for k, v in storage]),
    )
    return _contract_data(SCVal(SCValType.SCV_CONTRACT_INSTANCE, instance=instance))


def _contract_data(val: SCVal) -> str:
    return xdr.LedgerEntryData(
        xdr.LedgerEntryType.CONTRACT_DATA,
        contract_data=xdr.ContractDataEntry(
            ext=xdr.ExtensionPoint(0),
            contract=Address(LEDGER_ID).to_xdr_sc_address(),
            key=SCVal(SCValType.SCV_LEDGER_KEY_CONTRACT_INSTANCE),
            durability=xdr.ContractDataDurability.PERSISTENT,
            val=val,
        ),
    ).to_xdr()


# ── the decode ──────────────────────────────────────────────────


def test_the_live_testnet_entry_decodes_to_its_scorer():
    assert sc._instance_storage_address(TESTNET_LEDGER_INSTANCE_XDR, sc._SCORER_STORAGE_KEY) == TESTNET_SCORER


def test_the_scorer_is_read_under_its_own_key_not_the_admins():
    """Admin sits first in storage and is also an address. A decode that
    took the first address it met would report the admin as the scorer."""
    entry = _instance_entry([(_key("Admin"), scval.to_address(ADMIN)), (_key("Scorer"), scval.to_address(SCORER))])
    assert sc._instance_storage_address(entry, sc._SCORER_STORAGE_KEY) == SCORER

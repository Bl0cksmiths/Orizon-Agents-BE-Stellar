"""Exit codes, network constants and the kind / deliverable vocabulary of the evidence sheet."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# The only network the video's evidence may come from (SOW §3.6).
TESTNET = "testnet"
TESTNET_PASSPHRASE = "Test SDF Network ; September 2015"
TESTNET_RPC = "https://soroban-testnet.stellar.org"
TESTNET_HORIZON = "https://horizon-testnet.stellar.org"
EXPLORER_TX = "https://stellar.expert/explorer/testnet/tx/{}"

# The lifecycle harness's evidence file (`scripts/lifecycle/evidence.py` JSONL_NAME).
JSONL_NAME = "lifecycle.jsonl"
SHEET_NAME = "evidence-sheet.md"
DESCRIPTION_NAME = "description.txt"
JSON_NAME = "evidence.json"

TX_HASH = re.compile(r"^[0-9a-f]{64}$")

# The frozen `kind` vocabulary of evidence.json, which the frontend's /demo page reads.
KINDS: tuple[str, ...] = ("register", "authorize", "settle", "seal", "rating", "dispute_rating", "refund", "other")
DELIVERABLES: tuple[str, ...] = ("D1", "D2", "D3", "D4")

# The harness's evidence `event` for each transaction row -> the kind it is.
# `charge` is v1's settlement and `authorize_unknown` an authorize whose submit
# outcome was lost and read back from the ledger; both are what they settle as.
EVENT_KINDS: dict[str, str] = {
    "register": "register",
    "authorize": "authorize",
    "authorize_unknown": "authorize",
    "settle": "settle",
    "charge": "settle",
    "seal": "seal",
    "rating": "rating",
    "dispute_rating": "dispute_rating",
    "refund": "refund",
}

# SOW §6.1's four deliverables, by what each kind of transaction proves:
#   D1 permissionless registration; D2 reputation-gated routing, fed by the
#   on-chain ratings; D3 the dispute window and the partial-credit refund; D4
#   the ecosystem validation — a workflow routed to an external agent,
#   authorized, settled and sealed.
KIND_DELIVERABLE: dict[str, str] = {
    "register": "D1",
    "rating": "D2",
    "dispute_rating": "D3",
    "refund": "D3",
    "authorize": "D4",
    "settle": "D4",
    "seal": "D4",
    "other": "D4",
}

KIND_LABEL: dict[str, str] = {
    "register": "Agent registration",
    "authorize": "Buyer authorization",
    "settle": "Settlement",
    "seal": "Attestation seal",
    "rating": "Reputation rating",
    "dispute_rating": "Dispute rating",
    "refund": "Partial-credit refund",
    "other": "Transaction",
}

KIND_PROVES: dict[str, str] = {
    "register": "AgentRegistry registered the agent from its operator's own wallet",
    "authorize": "the buyer's wallet authorized the plan's spend on PaymentEscrow",
    "settle": "PaymentEscrow paid the agent's owner for the delivered step",
    "seal": "AttestationRegistry sealed the job, tying the payout to the work",
    "rating": "ReputationLedger recorded the run's rating, which the routing floor reads",
    "dispute_rating": "the upheld dispute's kind=dispute rating landed on the ReputationLedger",
    "refund": "the settler paid the buyer the partial credit for the disputed step",
    "other": "a transaction the recorded run produced",
}

# Exit codes, so a wrapper can tell a refusal from a failed hash without
# parsing prose. 1 is an unhandled traceback and 2 is argparse.
EXIT_OK = 0  # every hash re-verified SUCCESS
EXIT_REFUSED = 3  # wrong network, unreadable input, no transaction rows: nothing written
EXIT_NOT_SUCCESS = 5  # a hash is FAILED, NOT_FOUND or malformed on the ledger; it is left out of evidence.json
EXIT_UNREADABLE = 8  # nothing contradicted, but a read failed, so a hash is unverified; rerun


@dataclass(frozen=True)
class RunConfig:
    inputs: tuple[Path, ...]
    out_dir: Path
    rpc_url: str
    horizon_url: str
    title: str
    disclosures: tuple[str, ...] = ()

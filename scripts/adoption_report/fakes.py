"""An in-memory adoption API, Soroban RPC and Horizon, for the hermetic suite.

`FakeWorld` holds a registry, a set of funded accounts, an escrow's
authorizations and a ledger of settlement transactions, and answers the three
services from them through one `httpx.MockTransport`. The API's answer is a
plain dict the test edits to tell a lie; the chain stays honest, so every
forged claim has to be caught by the verifier reading the chain.

Transactions are real XDR: the envelope is a `settle` (or any function) built
with the SDK, and each `charged` event is a real `ContractEvent`, so the
verifier's decoding is exercised exactly as it runs against testnet.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from stellar_sdk import Account, Address, InvokeHostFunction, Keypair, StrKey, TransactionBuilder, scval
from stellar_sdk import TransactionEnvelope as SdkEnvelope
from stellar_sdk import xdr as stellar_xdr

from .config import EXPLORER_ACCOUNT, EXPLORER_TX, TESTNET_PASSPHRASE, usdc_to_stroops

API = "https://orizon.test"
RPC = "https://rpc.test"
HORIZON = "https://horizon.test"
MAINNET_PASSPHRASE = "Public Global Stellar Network ; September 2015"


def _contract(tag: str) -> str:
    return StrKey.encode_contract(hashlib.sha256(tag.encode()).digest())


def _account(tag: str) -> str:
    return Keypair.from_raw_ed25519_seed(hashlib.sha256(tag.encode()).digest()).public_key


ESCROW = _contract("escrow")
REGISTRY = _contract("registry")
OTHER_CONTRACT = _contract("some-other-contract")

OP1 = _account("operator-1")
OP2 = _account("operator-2")
TEAM = _account("team-lead")
PLATFORM = _account("platform-admin")
BUYER = _account("buyer")
SETTLER = _account("settler")


def _b64(val: stellar_xdr.SCVal) -> str:
    return str(val.to_xdr())


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


@dataclass
class FakeTx:
    status: str
    ledger: int
    envelope_xdr: str
    events_xdr: list[str]
    rpc_visible: bool = True


@dataclass
class FakeWorld:
    rpc_passphrase: str = TESTNET_PASSPHRASE
    horizon_passphrase: str = TESTNET_PASSPHRASE
    api_status: int = 200
    rpc_down: bool = False
    registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    accounts: set[str] = field(default_factory=set)
    authorizations: dict[str, str] = field(default_factory=dict)
    txs: dict[str, FakeTx] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    ledger: int = 5_000_000

    # ── building the world ──────────────────────────────────────
    def register_agent(self, agent_id: str, owner: str, *, active: bool = True, name: str | None = None) -> None:
        self.registry[agent_id] = {"owner": owner, "active": active, "name": name or agent_id}
        self.accounts.add(owner)

    def settle(
        self,
        agent_id: str,
        amount_stroops: int,
        job_hex: str,
        *,
        payer: str = BUYER,
        contract: str = ESCROW,
        function: str = "settle",
        status: str = "SUCCESS",
        charged_agent: str | None = None,
        rpc_visible: bool = True,
    ) -> str:
        """A settlement transaction on the ledger; returns its hash."""
        self.ledger += 7
        auth_hex = hashlib.sha256(f"auth-{job_hex}".encode()).hexdigest()[:32]
        self.authorizations[auth_hex] = payer
        payouts = scval.to_vec(
            [
                scval.to_map(
                    {
                        scval.to_symbol("agent_id"): scval.to_symbol(agent_id),
                        scval.to_symbol("amount"): scval.to_int128(amount_stroops),
                    }
                )
            ]
        )
        args = [
            scval.to_address(SETTLER),
            scval.to_bytes(bytes.fromhex(auth_hex)),
            scval.to_bytes(bytes.fromhex(job_hex)),
            payouts,
        ]
        tx = (
            TransactionBuilder(Account(SETTLER, self.ledger), network_passphrase=TESTNET_PASSPHRASE, base_fee=100)
            .append_invoke_contract_function_op(contract_id=contract, function_name=function, parameters=args)
            .set_timeout(30)
            .build()
        )
        envelope = tx.to_xdr()
        tx_hash = hashlib.sha256(envelope.encode()).hexdigest()
        events = []
        if status == "SUCCESS":
            event = stellar_xdr.ContractEvent(
                ext=stellar_xdr.ExtensionPoint(0),
                contract_id=stellar_xdr.ContractID(stellar_xdr.Hash(StrKey.decode_contract(contract))),
                type=stellar_xdr.ContractEventType.CONTRACT,
                body=stellar_xdr.ContractEventBody(
                    v=0,
                    v0=stellar_xdr.ContractEventV0(
                        topics=[scval.to_symbol("charged"), scval.to_symbol(charged_agent or agent_id)],
                        data=scval.to_vec(
                            [
                                scval.to_bytes(hashlib.sha256(tx_hash.encode()).digest()[:16]),
                                scval.to_bytes(bytes.fromhex(auth_hex)),
                                scval.to_int128(amount_stroops),
                                scval.to_bytes(bytes.fromhex(job_hex)),
                            ]
                        ),
                    ),
                ),
            )
            events.append(str(event.to_xdr()))
        self.txs[tx_hash] = FakeTx(status, self.ledger, envelope, events, rpc_visible)
        return tx_hash

    @staticmethod
    def write_register(path: Path, accounts: list[str] | None = None) -> Path:
        """A register in the shape Lane P is expected to commit; the verifier reads any shape."""
        wallets = [{"address": a, "role": "team"} for a in (accounts if accounts is not None else [TEAM, PLATFORM])]
        path.write_text(json.dumps({"wallets": wallets}))
        return path

    # ── the API's claims ────────────────────────────────────────
    @staticmethod
    def workflow(tx_hash: str, job_hex: str, amount_usdc: float, payer: str = BUYER) -> dict[str, Any]:
        return {
            "job_id_hex": job_hex,
            "tx_hash": tx_hash,
            "explorer": EXPLORER_TX.format(tx_hash),
            "amount_usdc": amount_usdc,
            "payer": payer,
            "settled_at": 1_790_000_000,
        }

    @staticmethod
    def agent(agent_id: str, workflows: list[dict[str, Any]], *, active: bool = True) -> dict[str, Any]:
        return {"agent_id": agent_id, "name": agent_id, "active": active, "bound": True, "settled_workflows": workflows}

    @staticmethod
    def operator(owner: str, agents: list[dict[str, Any]]) -> dict[str, Any]:
        return {"owner": owner, "owner_explorer": EXPLORER_ACCOUNT.format(owner), "agents": agents}

    def claim(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)

    # ── transport ───────────────────────────────────────────────
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(API):
            self.calls.append(f"api {request.url.path}")
            if request.url.path == "/api/ecosystem/adoption":
                if self.api_status != 200:
                    return _json(self.api_status, {"error": "no"})
                return _json(200, self.payload)
            return _json(404, {"error": "not found"})
        if url.startswith(RPC):
            payload = json.loads(request.content)
            self.calls.append(f"rpc {payload.get('method')}")
            if self.rpc_down:
                return _json(503, {"error": "unavailable"})
            return self.rpc(payload)
        if url.startswith(HORIZON):
            self.calls.append(f"horizon {request.url.path}")
            return self.horizon(request.url.path)
        raise AssertionError(f"unexpected request {request.method} {url}")

    def rpc(self, payload: dict[str, Any]) -> httpx.Response:
        method, params = payload.get("method"), payload.get("params") or {}

        def ok(result: Any) -> httpx.Response:
            return _json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "result": result})

        if method == "getNetwork":
            return ok({"passphrase": self.rpc_passphrase, "protocolVersion": 23})
        if method == "getTransaction":
            tx = self.txs.get(params["hash"])
            if tx is None or not tx.rpc_visible:
                return ok({"status": "NOT_FOUND", "latestLedger": self.ledger})
            return ok(
                {
                    "status": tx.status,
                    "ledger": tx.ledger,
                    "envelopeXdr": tx.envelope_xdr,
                    "events": {"transactionEventsXdr": [], "contractEventsXdr": [tx.events_xdr]},
                }
            )
        if method == "simulateTransaction":
            return ok(self.simulate(params["transaction"]))
        return _json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32601, "message": "no"}})

    def simulate(self, tx_xdr: str) -> dict[str, Any]:
        env = SdkEnvelope.from_xdr(tx_xdr, TESTNET_PASSPHRASE)
        op = env.transaction.operations[0]
        assert isinstance(op, InvokeHostFunction)
        invoke = op.host_function.invoke_contract
        assert invoke is not None
        contract = Address.from_xdr_sc_address(invoke.contract_address).address
        fn = invoke.function_name.sc_symbol.decode()
        args: list[Any] = [scval.to_native(a) for a in invoke.args]

        def value(v: stellar_xdr.SCVal) -> dict[str, Any]:
            return {"results": [{"xdr": _b64(v), "auth": []}], "latestLedger": self.ledger}

        def fail(msg: str) -> dict[str, Any]:
            return {"error": msg, "latestLedger": self.ledger}

        if contract == REGISTRY and fn in ("owner_of", "get"):
            record = self.registry.get(str(args[0]))
            if record is None:
                return fail("HostError: Error(Contract, #2)\n\nEvent log (newest first): ...")
            if fn == "owner_of":
                return value(scval.to_address(record["owner"]))
            return value(
                scval.to_map(
                    {
                        scval.to_symbol("active"): scval.to_bool(record["active"]),
                        scval.to_symbol("id"): scval.to_symbol(str(args[0])),
                        scval.to_symbol("name"): scval.to_string(record["name"]),
                        scval.to_symbol("owner"): scval.to_address(record["owner"]),
                    }
                )
            )
        if contract == ESCROW and fn == "authorization":
            payer = self.authorizations.get(bytes(args[0]).hex())
            if payer is None:
                return fail("HostError: Error(Contract, #2)")
            return value(
                scval.to_map(
                    {
                        scval.to_symbol("payer"): scval.to_address(payer),
                        scval.to_symbol("settled"): scval.to_bool(True),
                    }
                )
            )
        return fail(f"HostError: Error(WasmVm, MissingValue) no fake for {contract}.{fn}")

    def horizon(self, path: str) -> httpx.Response:
        if path == "/":
            return _json(200, {"network_passphrase": self.horizon_passphrase})
        if path.startswith("/accounts/"):
            account = path.split("/")[2]
            return _json(200, {"id": account}) if account in self.accounts else _json(404, {"status": 404})
        if path.startswith("/transactions/"):
            tx = self.txs.get(path.split("/")[2])
            if tx is None:
                return _json(404, {"status": 404})
            return _json(
                200, {"successful": tx.status == "SUCCESS", "ledger": tx.ledger, "envelope_xdr": tx.envelope_xdr}
            )
        return _json(404, {"status": 404})


JOB_A = "a1" * 16
JOB_B = "b2" * 16
JOB_C = "c3" * 16


def healthy_world() -> FakeWorld:
    """Two external operators, one agent each, three settled workflows — all true, all MET."""
    world = FakeWorld()
    world.register_agent("alpha", OP1)
    world.register_agent("beta", OP2)
    world.register_agent("orizon_batch", PLATFORM)
    world.accounts |= {TEAM, BUYER}
    amount = 0.01
    stroops = usdc_to_stroops(amount)
    tx_a = world.settle("alpha", stroops, JOB_A)
    tx_b = world.settle("alpha", stroops, JOB_B)
    tx_c = world.settle("beta", stroops, JOB_C)
    world.payload = {
        "network": "testnet",
        "generated_at": 1_790_000_100,
        "targets": {"external_agents": 2, "unique_operator_wallets": 2, "settled_external_workflows": 3},
        "totals": {"external_agents": 2, "unique_operator_wallets": 2, "settled_external_workflows": 3},
        "met": {"external_agents": True, "unique_operator_wallets": True, "settled_external_workflows": True},
        "operators": [
            FakeWorld.operator(
                OP1,
                [
                    FakeWorld.agent(
                        "alpha",
                        [FakeWorld.workflow(tx_a, JOB_A, amount), FakeWorld.workflow(tx_b, JOB_B, amount)],
                    )
                ],
            ),
            FakeWorld.operator(OP2, [FakeWorld.agent("beta", [FakeWorld.workflow(tx_c, JOB_C, amount)])]),
        ],
        "excluded": [
            {
                "owner": PLATFORM,
                "owner_explorer": EXPLORER_ACCOUNT.format(PLATFORM),
                "reason": "platform_key",
                "role": "admin and settler",
                "agent_ids": ["orizon_batch"],
            }
        ],
        "degraded": False,
        "unreadable_agents": [],
    }
    return world

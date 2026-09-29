"""An in-memory deployment for the hermetic suite: API, backend host, RPC, Horizon and frontend.

`FakeWorld` answers all five through one `httpx.MockTransport`. `healthy_world()`
is a deployment in which every check passes; each test breaks exactly one fact
and asserts the pre-flight names it. Escrow views are answered with real XDR
built by the SDK, so the pre-flight's simulation decoding runs as it does
against testnet.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from stellar_sdk import Address, InvokeHostFunction, Keypair, StrKey, scval
from stellar_sdk import TransactionEnvelope as SdkEnvelope
from stellar_sdk import xdr as stellar_xdr

from .config import FRONTEND_PAGES, TESTNET_PASSPHRASE

API = "https://orizon.test"
BACKEND = "https://backend.test"
FRONTEND = "https://front.test"
RPC = "https://rpc.test"
HORIZON = "https://horizon.test"
MAINNET_PASSPHRASE = "Public Global Stellar Network ; September 2015"


def _contract(tag: str) -> str:
    return StrKey.encode_contract(hashlib.sha256(tag.encode()).digest())


def _account(tag: str) -> str:
    return Keypair.from_raw_ed25519_seed(hashlib.sha256(tag.encode()).digest()).public_key


ESCROW = _contract("escrow-v2")
REGISTRY = _contract("registry")

SIGNER = _account("signer")
OTHER_SIGNER = _account("someone-else")
OP1 = _account("operator-1")
OP2 = _account("operator-2")
BUYER = _account("buyer")
TEAM = _account("team-lead")
TEAM_BUYER = _account("team-buyer")

FLOOR = 5500


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


def ready_steps(**overrides: str) -> list[dict[str, Any]]:
    keys = ("registered", "active", "bound", "reachable", "routable", "first_run", "first_settlement")
    steps = []
    for key in keys:
        status = overrides.get(key, "done")
        steps.append(
            {
                "key": key,
                "status": status,
                "detail": f"{key} is {status}",
                "action": None if status == "done" else f"fix {key}",
                "evidence": None,
            }
        )
    return steps


def agent_row(agent_id: str, owner: str | None, *, bound: bool | None = True, source: str = "onchain") -> dict:
    return {
        "id": agent_id,
        "name": agent_id,
        "skills": ["x"],
        "price": 0.01,
        "rep": 0.7,
        "status": "online",
        "runs": 0,
        "real": False,
        "owner": owner,
        "source": source,
        "bound": bound,
    }


def rep(agent_id: str, lower: int, *, degraded: bool = False, stale: bool = False) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "smoothed_bps": lower + 300,
        "lower_bound_bps": lower,
        "avg_bps": lower,
        "count": 3,
        "weight": 1,
        "disputed": 1,
        "dispute_rate_bps": 3333,
        "source": "onchain",
        "degraded": degraded,
        "stale": stale,
        "stale_age_seconds": None,
    }


@dataclass
class FakeWorld:
    rpc_passphrase: str = TESTNET_PASSPHRASE
    horizon_passphrase: str = TESTNET_PASSPHRASE
    api_passphrase: str = TESTNET_PASSPHRASE
    health_failures: int = 0  # health probes that fail before the first 200
    health_down: bool = False
    rpc_down: bool = False
    simulate_down: bool = False  # the RPC answers getNetwork, then fails every simulation with a 503
    escrow_version: int = 2
    settler: str = SIGNER
    network: dict[str, Any] = field(default_factory=dict)
    readiness: dict[str, Any] | None = field(default_factory=dict)
    adoption: dict[str, Any] | None = field(default_factory=dict)  # None: the route 404s
    readiness_route: bool = True
    agent_readiness: dict[str, dict[str, Any]] = field(default_factory=dict)
    agents: list[dict[str, Any]] = field(default_factory=list)
    reputations: dict[str, dict[str, Any]] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    pages: dict[str, int] = field(default_factory=dict)
    decompose_body: dict[str, Any] = field(default_factory=dict)
    decompose_status: int = 200
    calls: list[str] = field(default_factory=list)
    _health_seen: int = 0

    # ── building the world ──────────────────────────────────────
    def fund(self, account: str, xlm: float, *, subentries: int = 0) -> None:
        self.accounts[account] = {
            "id": account,
            "subentry_count": subentries,
            "num_sponsoring": 0,
            "num_sponsored": 0,
            "balances": [{"asset_type": "native", "balance": f"{xlm:.7f}", "selling_liabilities": "0.0000000"}],
        }

    @staticmethod
    def write_register(path: Path, accounts: list[str] | None = None) -> Path:
        wallets = [
            {"address": a, "role": f"role of {a[:4]}"} for a in (accounts if accounts is not None else [TEAM, SIGNER])
        ]
        path.write_text(json.dumps({"wallets": wallets}))
        return path

    # ── transport ───────────────────────────────────────────────
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        if url.startswith(API):
            self.calls.append(f"api {request.method} {path}")
            return self.api(request.method, path)
        if url.startswith(BACKEND):
            self.calls.append(f"backend {request.method} {path}")
            if path == "/readiness" and self.readiness is not None:
                status = 200 if self.readiness.get("status") == "ready" else 503
                return _json(status, self.readiness)
            return _json(404, {"detail": "Not Found"})
        if url.startswith(FRONTEND):
            self.calls.append(f"frontend {request.method} {path}")
            status = self.pages.get(path, 404)
            if status in (301, 302, 307, 308):
                return httpx.Response(status, headers={"location": f"{FRONTEND}/login"})
            return httpx.Response(status, text="<html></html>")
        if url.startswith(RPC):
            payload = json.loads(request.content)
            self.calls.append(f"rpc {payload.get('method')}")
            if self.rpc_down:
                return _json(503, {"error": "unavailable"})
            return self.rpc(payload)
        if url.startswith(HORIZON):
            self.calls.append(f"horizon {request.method} {path}")
            return self.horizon(path)
        raise AssertionError(f"unexpected request {request.method} {url}")

    def api(self, method: str, path: str) -> httpx.Response:
        not_found = _json(404, {"detail": "Not Found", "error": {"code": "not_found"}})
        if method == "POST":
            if path == "/api/orchestrator/decompose":
                return _json(self.decompose_status, self.decompose_body)
            return not_found
        if path == "/api/health":
            self._health_seen += 1
            if self.health_down or self._health_seen <= self.health_failures:
                return _json(503, {"error": "waking"})
            return _json(200, {"status": "ok"})
        if path == "/api/stellar/network":
            return _json(200, {**self.network, "network_passphrase": self.api_passphrase})
        if path == "/api/ecosystem/adoption":
            return not_found if self.adoption is None else _json(200, self.adoption)
        if path == "/api/agents":
            return _json(200, self.agents)
        if path.startswith("/api/agents/") and path.endswith("/readiness"):
            if not self.readiness_route:
                return not_found
            agent_id = path.split("/")[3]
            body = self.agent_readiness.get(agent_id)
            if body is None:
                body = {"agent_id": agent_id, "checked_at": 1, "ready": False, "steps": ready_steps(registered="todo")}
            return _json(200, body)
        if path == "/api/stellar/reputation":
            return _json(200, {"reputations": self.reputations, "floor_bps": FLOOR, "prior_bps": 7000})
        if path == "/api/stellar/reputation/params":
            return _json(200, self.params)
        return not_found

    def rpc(self, payload: dict[str, Any]) -> httpx.Response:
        method, params = payload.get("method"), payload.get("params") or {}

        def ok(result: Any) -> httpx.Response:
            return _json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "result": result})

        if method == "getNetwork":
            return ok({"passphrase": self.rpc_passphrase, "protocolVersion": 23})
        if method == "simulateTransaction":
            if self.simulate_down:
                return _json(503, {"error": "unavailable"})
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

        def value(v: stellar_xdr.SCVal) -> dict[str, Any]:
            return {"results": [{"xdr": str(v.to_xdr()), "auth": []}], "latestLedger": 1}

        def fail(msg: str) -> dict[str, Any]:
            return {"error": msg, "latestLedger": 1}

        if contract == ESCROW and fn == "version":
            if self.escrow_version < 2:
                return fail("HostError: Error(WasmVm, MissingValue)\n\nEvent log: function not found")
            return value(scval.to_uint32(self.escrow_version))
        if contract == ESCROW and fn == "settler":
            if self.escrow_version < 2:
                return fail("HostError: Error(WasmVm, MissingValue)")
            return value(scval.to_address(self.settler))
        return fail(f"no fake for {contract}.{fn}")

    def horizon(self, path: str) -> httpx.Response:
        if path == "/":
            return _json(200, {"network_passphrase": self.horizon_passphrase})
        if path.startswith("/accounts/"):
            record = self.accounts.get(path.split("/")[2])
            return _json(200, record) if record is not None else _json(404, {"status": 404})
        return _json(404, {"status": 404})

    def adoption_payload(self, operators: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "network": "testnet",
            "generated_at": 1_790_000_000,
            "targets": {"external_agents": 2, "unique_operator_wallets": 2, "settled_external_workflows": 3},
            "totals": {"external_agents": 0, "unique_operator_wallets": 0, "settled_external_workflows": 0},
            "met": {"external_agents": False, "unique_operator_wallets": False, "settled_external_workflows": False},
            "operators": [
                {
                    "owner": owner,
                    "owner_explorer": f"https://stellar.expert/explorer/testnet/account/{owner}",
                    "agents": [
                        {"agent_id": a, "name": a, "active": True, "bound": True, "settled_workflows": []}
                        for a in agent_ids
                    ],
                }
                for owner, agent_ids in operators.items()
            ],
            "excluded": [],
            "degraded": False,
            "unreadable_agents": [],
        }

    def copy(self) -> FakeWorld:
        return copy.deepcopy(self)


def healthy_world() -> FakeWorld:
    """A deployment ready to record: every check passes."""
    world = FakeWorld()
    world.network = {
        "network": "testnet",
        "rpc_url": RPC,
        "admin": TEAM,
        "dispatch_signer": TEAM,
        "asset": "native",
        "asset_sac": _contract("sac"),
        "contracts": {
            "agent_registry": REGISTRY,
            "reputation_ledger": _contract("ledger"),
            "payment_escrow": ESCROW,
            "attestation_registry": _contract("attest"),
        },
    }
    world.readiness = {
        "status": "ready",
        "llm": "ok",
        "stellar": "configured",
        "signer": "configured",
        "pdax": "unconfigured",
        "cold_start": {"routable": True, "lower_bound_bps": 5677, "floor_bps": FLOOR, "margin_bps": 177},
        "ratings": {"writer": "scorer", "signer": SIGNER, "scorer": SIGNER},
        "disputes": {
            "store": "postgres",
            "reconcile": {
                "enabled": True,
                "running": True,
                "last_run_at": None,
                "last_skipped": None,
                "last_outcomes": {},
            },
        },
        "escrow": {"contract": ESCROW, "version": 2},
    }
    world.agents = [
        agent_row("agt_01h8", None, bound=None, source="seeded"),
        agent_row("alpha", OP1),
        agent_row("beta", OP2),
        agent_row("lowrep", OP2),
    ]
    world.adoption = world.adoption_payload({OP1: ["alpha"], OP2: ["beta", "lowrep"]})
    world.agent_readiness = {
        a: {"agent_id": a, "checked_at": 1, "ready": True, "steps": ready_steps(first_settlement="todo")}
        for a in ("alpha", "beta")
    }
    # Below the floor, so `routable` fails: the exclusion scene's subject.
    world.agent_readiness["lowrep"] = {
        "agent_id": "lowrep",
        "checked_at": 1,
        "ready": False,
        "steps": ready_steps(routable="failed", first_settlement="todo"),
    }
    world.reputations = {
        "agt_01h8": rep("agt_01h8", 5677),
        "alpha": rep("alpha", 6100),
        "beta": rep("beta", 6000),
        "lowrep": rep("lowrep", 4100),
    }
    world.params = {"enabled": True, "floor_bps": FLOOR, "prior_bps": 7000, "network": "testnet"}
    world.fund(SIGNER, 50)
    world.fund(BUYER, 20)
    world.fund(OP1, 5)
    world.fund(TEAM_BUYER, 20)
    world.fund(TEAM, 20)
    world.pages = {p: 200 for p in FRONTEND_PAGES}
    world.decompose_body = {
        "plan_id": "plan_1",
        "intent": "x",
        "steps": [],
        "total_usdc": 0.02,
        "total_eta": 3,
        "floor_bps": FLOOR,
        "notices": [
            {
                "kind": "excluded",
                "agent_id": "lowrep",
                "reason": "below routing floor (4100 < 5500 bps)",
                "reason_code": "below_floor",
                "lower_bound_bps": 4100,
                "floor_bps": FLOOR,
            }
        ],
    }
    return world

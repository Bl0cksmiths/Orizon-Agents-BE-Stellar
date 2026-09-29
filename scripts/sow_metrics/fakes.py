"""An in-memory testnet and deployment for the hermetic suite: RPC, Horizon, API, backend, frontend and GitHub.

`FakeWorld` answers all six through one `httpx.MockTransport`. `met_world()`
is a sprint in which every one of the eleven metrics is met; each test breaks
exactly one fact and asserts the metric that guards it misses, and says why.
Contract views and instance storage are answered with real XDR built by the
SDK, and Horizon histories carry real base64 SCVal parameters, so the
generator's decoding runs exactly as it does against testnet.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from stellar_sdk import Address, InvokeHostFunction, Keypair, StrKey, scval
from stellar_sdk import TransactionEnvelope as SdkEnvelope
from stellar_sdk import xdr as stellar_xdr

from .config import (
    DEMO_PAGE,
    DISPUTE_ROUTES,
    GUIDE_PAGE,
    KNOWN_ESCROWS,
    REGISTER_PAGE,
    REGISTER_ROUTE,
    REPOSITORIES,
    TESTNET_PASSPHRASE,
)
from .metrics import dispute_job_id

API = "https://orizon.test"
BACKEND = "https://backend.test"
FRONTEND = "https://front.test"
RPC = "https://rpc.test"
HORIZON = "https://horizon.test"
GITHUB = "https://github-api.test"
MAINNET_PASSPHRASE = "Public Global Stellar Network ; September 2015"


def _contract(tag: str) -> str:
    return StrKey.encode_contract(hashlib.sha256(tag.encode()).digest())


def _account(tag: str) -> str:
    return Keypair.from_raw_ed25519_seed(hashlib.sha256(tag.encode()).digest()).public_key


REGISTRY = _contract("registry")
LEDGER = _contract("ledger")
ATTEST = _contract("attest")
SAC = _contract("native-sac")
ESCROW_V2 = _contract("escrow-v2")
ESCROW_V1 = KNOWN_ESCROWS[0]

# The team register (see `write_register`).
ADMIN = _account("admin")  # contract admin, registry admin, v1 escrow settler
TEAM_OP = _account("team-operator")
TEAM_BUYER = _account("team-buyer")
# Platform keys that are NOT in the register: only the runtime reads name them.
SIGNER = _account("signer")  # ratings signer and scorer, sealer, v2 settler
DISPATCH = _account("dispatch")
# Outside operators and buyers.
OP1 = _account("operator-1")
OP2 = _account("operator-2")
BUYER1 = _account("buyer-1")
BUYER2 = _account("buyer-2")

REGISTER_ROLES = {
    ADMIN: "contract admin and v1 escrow settler",
    TEAM_OP: "QA throwaway operator key",
    TEAM_BUYER: "QA throwaway buyer key",
}


def ts(date: str) -> int:
    """`2026-09-20` or `2026-09-20T10:00:00` (UTC) as unix seconds."""
    text = date if "T" in date else date + "T12:00:00"
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())


def iso(unix: int) -> str:
    return datetime.fromtimestamp(unix, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def job(tag: str) -> str:
    """A 16-byte job id, as hex."""
    return hashlib.sha256(tag.encode()).digest()[:16].hex()


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


def _sym(value: str) -> stellar_xdr.SCVal:
    return scval.to_symbol(value)


def _map(fields: dict[str, stellar_xdr.SCVal]) -> stellar_xdr.SCVal:
    return scval.to_map({_sym(k): v for k, v in sorted(fields.items())})


@dataclass
class Escrow:
    version: int
    settler: str
    admin: str
    # One entry per id the Nonce issued: ("auth", fields) | ("receipt", fields) | ("gone", {}).
    ids: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def demo_html(state: str | None, seconds: int | None = None) -> str:
    marker = f' data-demo="{state}"' if state else ""
    duration = ""
    if seconds is not None:
        duration = f'<time dateTime="PT{seconds // 60}M{seconds % 60}S">{seconds // 60}:{seconds % 60:02d}</time>'
    return (
        f'<html><body><main><article{marker} class="x"><time dateTime="2026-09-30">30 Sep</time>'
        f"{duration}</article></main></body></html>"
    )


@dataclass
class FakeWorld:
    rpc_passphrase: str = TESTNET_PASSPHRASE
    horizon_passphrase: str = TESTNET_PASSPHRASE
    api_passphrase: str = TESTNET_PASSPHRASE
    api_network: str = "testnet"
    registry_admin: str = ADMIN
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    escrows: dict[str, Escrow] = field(default_factory=dict)
    live_escrow: str = ESCROW_V2
    ledger_roles: dict[str, str] = field(default_factory=lambda: {"Admin": ADMIN, "Scorer": SIGNER})
    attest_roles: dict[str, str] = field(default_factory=lambda: {"Admin": ADMIN, "Sealer": SIGNER})
    disputed: dict[str, int] = field(default_factory=dict)
    histories: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    readiness: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    routes: set[str] = field(default_factory=set)
    pages: dict[str, tuple[int, str]] = field(default_factory=dict)
    repos: dict[str, tuple[int, str | None]] = field(default_factory=dict)
    # Failure switches.
    rpc_down: bool = False  # every RPC call answers 503
    simulate_down: bool = False  # getNetwork answers; every simulation answers 503
    simulate_down_for: set[tuple[str, str]] = field(default_factory=set)  # (contract, function) answering 503
    ledger_entries_down: bool = False
    horizon_down_for: set[str] = field(default_factory=set)  # accounts whose history answers 503
    api_down: set[str] = field(default_factory=set)  # paths answering 503
    github_status: int | None = None  # every repository read answers this
    calls: list[str] = field(default_factory=list)
    marks: dict[str, str] = field(default_factory=dict)  # a name -> the tx hash of a notable transaction
    _n: int = 0

    # ── building the world ──────────────────────────────────────
    def _next(self) -> int:
        self._n += 1
        return self._n

    def tx(self) -> str:
        return hashlib.sha256(f"tx-{self._next()}".encode()).hexdigest()

    def _op(
        self,
        account: str,
        *,
        source: str,
        at: int,
        contract: str,
        function: str,
        args: list[stellar_xdr.SCVal],
        tx_hash: str | None = None,
        changes: list[dict[str, Any]] | None = None,
        also: tuple[str, ...] = (),
    ) -> str:
        tx_hash = tx_hash or self.tx()
        op_id = str(1_000_000 + self._next())
        record = {
            "id": op_id,
            "paging_token": op_id,
            "transaction_successful": True,
            "source_account": source,
            "type": "invoke_host_function",
            "created_at": iso(at),
            "transaction_hash": tx_hash,
            "function": "HostFunctionTypeHostFunctionTypeInvokeContract",
            "parameters": [
                {"value": scval.to_address(contract).to_xdr(), "type": "Address"},
                {"value": _sym(function).to_xdr(), "type": "Sym"},
                *({"value": a.to_xdr(), "type": "Val"} for a in args),
            ],
            "asset_balance_changes": changes or [],
        }
        for who in (account, *also):
            self.histories.setdefault(who, []).append(copy.deepcopy(record))
        return tx_hash

    def drop(self, tx_hash: str) -> None:
        """Remove a transaction from every history, as if it had never been sent."""
        for account, records in self.histories.items():
            self.histories[account] = [r for r in records if r["transaction_hash"] != tx_hash]

    def add_agent(self, agent_id: str, owner: str, *, at: str = "2026-09-10", signer: str | None = None) -> str:
        when = ts(at)
        self.agents[agent_id] = {"owner": owner, "name": agent_id.title(), "active": True, "registered_at": when}
        args = [
            scval.to_address(owner),
            _sym(agent_id),
            scval.to_string(agent_id),
            scval.to_vec([]),
            scval.to_int128(0),
        ]
        return self._op(owner, source=signer or owner, at=when, contract=REGISTRY, function="register", args=args)

    def add_escrow(self, contract: str, *, version: int, settler: str, admin: str = ADMIN) -> None:
        self.escrows[contract] = Escrow(version=version, settler=settler, admin=admin)

    def _new_id(self, contract: str, kind: str, fields: dict[str, Any]) -> bytes:
        escrow = self.escrows[contract]
        escrow.ids.append((kind, fields))
        return (len(escrow.ids) - 1).to_bytes(16, "big")

    def authorize(self, contract: str, payer: str, agent_id: str, max_amount: int) -> bytes:
        fields = {"payer": payer, "agent_id": agent_id, "max_amount": max_amount, "spent": 0}
        return self._new_id(contract, "auth", fields)

    def _receipt(self, contract: str, auth_id: bytes, agent_id: str, amount: int, job_hex: str, at: int) -> bytes:
        index = int.from_bytes(auth_id, "big")
        self.escrows[contract].ids[index][1]["spent"] += amount
        fields = {"auth_id": auth_id, "agent_id": agent_id, "amount": amount, "job_id": job_hex, "settled_at": at}
        return self._new_id(contract, "receipt", fields)

    def charge_v1(
        self, payer: str, agent_id: str, amount: int, job_hex: str, *, at: str, contract: str = ESCROW_V1
    ) -> str:
        """A v1 `charge`: one receipt, signed by the escrow's settler."""
        when = ts(at)
        auth_id = self.authorize(contract, payer, agent_id, amount * 2)
        self._receipt(contract, auth_id, agent_id, amount, job_hex, when)
        settler = self.escrows[contract].settler
        args = [
            scval.to_address(settler),
            scval.to_bytes(auth_id),
            scval.to_int128(amount),
            scval.to_bytes(bytes.fromhex(job_hex)),
        ]
        return self._op(settler, source=settler, at=when, contract=contract, function="charge", args=args)

    def settle_v2(
        self, payer: str, payouts: list[tuple[str, int]], job_hex: str, *, at: str, contract: str = ESCROW_V2
    ) -> str:
        """A v2 `settle`: one receipt per payout, signed by the escrow's settler."""
        when = ts(at)
        auth_id = self.authorize(contract, payer, payouts[0][0], sum(a for _, a in payouts) + 1)
        for agent_id, amount in payouts:
            self._receipt(contract, auth_id, agent_id, amount, job_hex, when)
        settler = self.escrows[contract].settler
        payout_vals = [_map({"agent_id": _sym(a), "amount": scval.to_int128(n)}) for a, n in payouts]
        args = [
            scval.to_address(settler),
            scval.to_bytes(auth_id),
            scval.to_bytes(bytes.fromhex(job_hex)),
            scval.to_vec(payout_vals),
        ]
        return self._op(settler, source=settler, at=when, contract=contract, function="settle", args=args)

    def rate(
        self,
        agent_id: str,
        job_hex: str,
        payer: str,
        *,
        kind: str = "auto",
        rating: int = 90,
        at: str = "2026-09-20",
        scorer: str = SIGNER,
        also: tuple[str, ...] = (),
    ) -> str:
        if kind == "dispute":
            self.disputed[agent_id] = self.disputed.get(agent_id, 0) + 1
        args = [
            scval.to_address(scorer),
            _sym(agent_id),
            scval.to_bytes(bytes.fromhex(job_hex)),
            scval.to_uint32(rating),
            scval.to_int128(100_000),
            scval.to_address(payer),
            _sym(kind),
        ]
        return self._op(scorer, source=scorer, at=ts(at), contract=LEDGER, function="submit", args=args, also=also)

    def dispute(self, agent_id: str, job_hex: str, payer: str, *, step: int = 0, at: str = "2026-09-21") -> str:
        """The rating an upheld dispute writes, under the derived job id."""
        derived = dispute_job_id(bytes.fromhex(job_hex), step).hex()
        return self.rate(agent_id, derived, payer, kind="dispute", rating=0, at=at)

    def transfer(
        self,
        source: str,
        destination: str,
        amount: int,
        *,
        at: str,
        muxed: str | None = None,
        also: tuple[str, ...] = (),
    ) -> str:
        """An asset-contract transfer signed by `source`, as Horizon reports it."""
        change: dict[str, Any] = {
            "asset_type": "native",
            "type": "transfer",
            "from": source,
            "to": destination,
            "amount": f"{amount / 10_000_000:.7f}",
        }
        if muxed is not None:
            change["destination_muxed_id"] = muxed
        args = [scval.to_address(source), scval.to_address(destination), scval.to_int128(amount)]
        return self._op(
            source,
            source=source,
            at=ts(at),
            contract=SAC,
            function="transfer",
            args=args,
            changes=[change],
            also=also,
        )

    @staticmethod
    def write_register(path: Path, roles: dict[str, str] | None = None) -> Path:
        wallets = [{"address": a, "role": r} for a, r in (roles if roles is not None else REGISTER_ROLES).items()]
        path.write_text(json.dumps({"wallets": wallets}))
        return path

    # ── transport ───────────────────────────────────────────────
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        if request.method != "GET" and not url.startswith(RPC):
            raise AssertionError(f"the generator must only read, but sent {request.method} {url}")
        if url.startswith(API):
            self.calls.append(f"api {path}")
            return self.api(path)
        if url.startswith(BACKEND):
            self.calls.append(f"backend {path}")
            if path in self.api_down:
                return _json(503, {"error": "down"})
            if path == "/readiness":
                return _json(200, self.readiness)
            if path == "/openapi.json":
                paths: dict[str, dict[str, Any]] = {}
                for route in self.routes:
                    method, route_path = route.split(" ", 1)
                    paths.setdefault(route_path, {})[method.lower()] = {}
                return _json(200, {"openapi": "3.1.0", "paths": paths})
            return _json(404, {"detail": "Not Found"})
        if url.startswith(FRONTEND):
            self.calls.append(f"frontend {path}")
            status, body = self.pages.get(path, (404, "<html>not found</html>"))
            if status in (301, 302, 307, 308):
                return httpx.Response(status, headers={"location": f"{FRONTEND}/login"})
            return httpx.Response(status, text=body, headers={"content-type": "text/html"})
        if url.startswith(GITHUB):
            self.calls.append(f"github {path}")
            name = path.removeprefix("/repos/")
            if self.github_status is not None:
                return _json(self.github_status, {"message": "API rate limit exceeded"})
            status, spdx = self.repos.get(name, (404, None))
            if status != 200:
                return _json(status, {"message": "Not Found"})
            license_ = None if spdx is None else {"spdx_id": spdx, "key": spdx.lower()}
            return _json(200, {"full_name": name, "private": False, "license": license_})
        if url.startswith(RPC):
            payload = json.loads(request.content)
            self.calls.append(f"rpc {payload.get('method')}")
            if self.rpc_down:
                return _json(503, {"error": "unavailable"})
            return self.rpc(payload)
        if url.startswith(HORIZON):
            self.calls.append(f"horizon {path}")
            return self.horizon(request)
        raise AssertionError(f"unexpected request {request.method} {url}")

    def api(self, path: str) -> httpx.Response:
        if path in self.api_down:
            return _json(503, {"error": "down"})
        if path == "/api/stellar/network":
            return _json(
                200,
                {
                    "network": self.api_network,
                    "rpc_url": RPC,
                    "network_passphrase": self.api_passphrase,
                    "admin": ADMIN,
                    "dispatch_signer": DISPATCH,
                    "asset": "native",
                    "asset_sac": SAC,
                    "contracts": {
                        "agent_registry": REGISTRY,
                        "reputation_ledger": LEDGER,
                        "payment_escrow": self.live_escrow,
                        "attestation_registry": ATTEST,
                    },
                },
            )
        if path == "/api/stellar/reputation/params":
            return _json(200, self.params)
        return _json(404, {"detail": "Not Found"})

    # ── RPC ─────────────────────────────────────────────────────
    def rpc(self, payload: dict[str, Any]) -> httpx.Response:
        method, params = payload.get("method"), payload.get("params") or {}

        def ok(result: Any) -> httpx.Response:
            return _json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "result": result})

        if method == "getNetwork":
            return ok({"passphrase": self.rpc_passphrase, "protocolVersion": 23})
        if method == "simulateTransaction":
            if self.simulate_down:
                return _json(503, {"error": "unavailable"})
            return self.simulate(params["transaction"], ok)
        if method == "getLedgerEntries":
            if self.ledger_entries_down:
                return _json(503, {"error": "unavailable"})
            key = stellar_xdr.LedgerKey.from_xdr(params["keys"][0]).contract_data
            assert key is not None
            contract = Address.from_xdr_sc_address(key.contract).address
            storage = self.instance(contract)
            if storage is None:
                return ok({"entries": [], "latestLedger": 1})
            return ok({"entries": [{"key": params["keys"][0], "xdr": _instance_xdr(contract, storage)}]})
        return _json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32601, "message": "no"}})

    def instance(self, contract: str) -> dict[str, stellar_xdr.SCVal] | None:
        if contract == LEDGER:
            return {k: scval.to_address(v) for k, v in self.ledger_roles.items()}
        if contract == ATTEST:
            return {k: scval.to_address(v) for k, v in self.attest_roles.items()}
        if contract in self.escrows:
            e = self.escrows[contract]
            return {
                "Admin": scval.to_address(e.admin),
                "Nonce": scval.to_uint64(len(e.ids)),
                "Registry": scval.to_address(REGISTRY),
                "Settler": scval.to_address(e.settler),
                "Usdc": scval.to_address(SAC),
            }
        if contract == REGISTRY:
            return {"Admin": scval.to_address(self.registry_admin)}
        return None

    def simulate(self, tx_xdr: str, ok: Any) -> httpx.Response:
        env = SdkEnvelope.from_xdr(tx_xdr, TESTNET_PASSPHRASE)
        op = env.transaction.operations[0]
        assert isinstance(op, InvokeHostFunction)
        invoke = op.host_function.invoke_contract
        assert invoke is not None
        contract = Address.from_xdr_sc_address(invoke.contract_address).address
        fn = invoke.function_name.sc_symbol.decode()
        args = invoke.args
        if (contract, fn) in self.simulate_down_for:
            return _json(503, {"error": "unavailable"})

        def value(v: stellar_xdr.SCVal) -> httpx.Response:
            return ok({"results": [{"xdr": v.to_xdr(), "auth": []}], "latestLedger": 1})

        def fail(msg: str) -> httpx.Response:
            return ok({"error": msg, "latestLedger": 1})

        if contract == REGISTRY:
            if fn == "list_ids":
                return value(scval.to_vec([_sym(a) for a in self.agents]))
            if fn == "admin":
                return value(scval.to_address(self.registry_admin))
            if fn == "get":
                agent_id = scval.from_symbol(args[0])
                a = self.agents.get(agent_id)
                if a is None:
                    return fail("HostError: Error(Contract, #2)")
                return value(
                    _map(
                        {
                            "id": _sym(agent_id),
                            "owner": scval.to_address(a["owner"]),
                            "name": scval.to_string(a["name"]),
                            "skills": scval.to_vec([_sym("x")]),
                            "price": scval.to_int128(100_000),
                            "active": scval.to_bool(a["active"]),
                            "registered_at": scval.to_uint64(a["registered_at"]),
                        }
                    )
                )
        if contract in self.escrows:
            e = self.escrows[contract]
            if fn == "version":
                if e.version < 2:
                    return fail("HostError: Error(WasmVm, MissingValue)\n\nEvent log: function not found")
                return value(scval.to_uint32(e.version))
            if fn in ("receipt", "authorization"):
                index = int.from_bytes(scval.from_bytes(args[0]), "big")
                kind, fields = e.ids[index] if index < len(e.ids) else ("gone", {})
                if (fn == "receipt" and kind != "receipt") or (fn == "authorization" and kind != "auth"):
                    return fail("HostError: Error(Contract, #2)")
                if kind == "receipt":
                    return value(
                        _map(
                            {
                                "auth_id": scval.to_bytes(fields["auth_id"]),
                                "agent_id": _sym(fields["agent_id"]),
                                "amount": scval.to_int128(fields["amount"]),
                                "job_id": scval.to_bytes(bytes.fromhex(fields["job_id"])),
                                "settled_at": scval.to_uint64(fields["settled_at"]),
                            }
                        )
                    )
                return value(
                    _map(
                        {
                            "payer": scval.to_address(fields["payer"]),
                            "agent_id": _sym(fields["agent_id"]),
                            "max_amount": scval.to_int128(fields["max_amount"]),
                            "spent": scval.to_int128(fields["spent"]),
                            "expires_at": scval.to_uint64(1),
                            "revoked": scval.to_bool(False),
                            **({"settled": scval.to_bool(fields["spent"] > 0)} if e.version >= 2 else {}),
                        }
                    )
                )
        if contract == LEDGER and fn == "rep_state":
            agent_id = scval.from_symbol(args[0])
            return value(
                _map(
                    {
                        "count": scval.to_uint32(3),
                        "disputed": scval.to_uint32(self.disputed.get(agent_id, 0)),
                        "last_epoch": scval.to_uint64(1),
                        "sum_w": scval.to_int128(1),
                        "weight": scval.to_int128(1),
                    }
                )
            )
        return fail(f"HostError: no fake for {contract}.{fn}")

    # ── Horizon ─────────────────────────────────────────────────
    def horizon(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/":
            return _json(200, {"network_passphrase": self.horizon_passphrase})
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "accounts" and parts[2] == "operations":
            account = parts[1]
            if account in self.horizon_down_for:
                return _json(503, {"status": 503})
            if account not in self.histories:
                return _json(404, {"status": 404})
            query = parse_qs(urlparse(str(request.url)).query)
            limit = int(query.get("limit", ["200"])[0])
            cursor = query.get("cursor", [""])[0]
            records = self.histories[account]
            if cursor:
                records = [r for r in records if int(r["paging_token"]) > int(cursor)]
            return _json(200, {"_embedded": {"records": records[:limit]}})
        return _json(404, {"status": 404})

    def copy(self) -> FakeWorld:
        return copy.deepcopy(self)


def _instance_xdr(contract: str, storage: dict[str, stellar_xdr.SCVal]) -> str:
    entries = [stellar_xdr.SCMapEntry(key=scval.to_vec([_sym(k)]), val=v) for k, v in sorted(storage.items())]
    instance = stellar_xdr.SCContractInstance(
        executable=stellar_xdr.ContractExecutable(
            stellar_xdr.ContractExecutableType.CONTRACT_EXECUTABLE_WASM, wasm_hash=stellar_xdr.Hash(b"\0" * 32)
        ),
        storage=stellar_xdr.SCMap(entries),
    )
    data = stellar_xdr.LedgerEntryData(
        type=stellar_xdr.LedgerEntryType.CONTRACT_DATA,
        contract_data=stellar_xdr.ContractDataEntry(
            ext=stellar_xdr.ExtensionPoint(0),
            contract=Address(contract).to_xdr_sc_address(),
            key=stellar_xdr.SCVal(stellar_xdr.SCValType.SCV_LEDGER_KEY_CONTRACT_INSTANCE),
            durability=stellar_xdr.ContractDataDurability.PERSISTENT,
            val=stellar_xdr.SCVal(stellar_xdr.SCValType.SCV_CONTRACT_INSTANCE, instance=instance),
        ),
    )
    return data.to_xdr()


# Amounts, in stroops.
A1, A2, A3 = 1_000_000, 2_000_000, 500_000
JOB1, JOB2, JOB3 = job("job-1"), job("job-2"), job("job-3")


def met_world() -> FakeWorld:
    """A sprint in which every SOW §6.3 metric is met, with every kind of exclusion present too."""
    w = FakeWorld()
    w.add_escrow(ESCROW_V2, version=2, settler=SIGNER)
    w.add_escrow(ESCROW_V1, version=1, settler=ADMIN)

    # The registry: three agents run by two outside operators, and the team's own.
    w.add_agent("house_agent", ADMIN, at="2026-04-20")
    w.add_agent("qa_agent", TEAM_OP, at="2026-09-17")
    w.add_agent("signer_agent", SIGNER, at="2026-09-18")  # a platform key, not in the register
    w.add_agent("alpha", OP1, at="2026-09-19")
    w.add_agent("beta", OP2, at="2026-09-20")
    w.add_agent("gamma", OP2, at="2026-09-20")

    # v1 history: the admin paying itself before the sprint (excluded twice over).
    w.charge_v1(ADMIN, "house_agent", 1_140_000, job("v1-a"), at="2026-05-13")
    # v2 sprint settlements: three workflows to outside operators' agents, four charges.
    w.marks["settle1"] = w.settle_v2(BUYER1, [("alpha", A1)], JOB1, at="2026-09-21T10:00:00")
    w.marks["settle2"] = w.settle_v2(BUYER2, [("beta", A2)], JOB2, at="2026-09-22T10:00:00")
    w.marks["settle3"] = w.settle_v2(BUYER1, [("gamma", A3), ("alpha", A3)], JOB3, at="2026-09-23T10:00:00")

    # Ratings: automatic ones, and one upheld dispute on job 1 refunded to its payer.
    for agent_id, job_hex, payer in (("alpha", JOB1, BUYER1), ("beta", JOB2, BUYER2), ("gamma", JOB3, BUYER1)):
        w.rate(agent_id, job_hex, payer, at="2026-09-23")
    # The refund is also seen from the dispatch signer's history: it must count once.
    w.marks["dispute"] = w.dispute("alpha", JOB1, BUYER1, at="2026-09-24T09:00:00")
    w.marks["refund"] = w.transfer(SIGNER, BUYER1, A1 // 2, at="2026-09-24T09:05:00", also=(DISPATCH,))
    # The 4.01 drill: the admin paying a team key, with no dispute behind it.
    w.marks["drill"] = w.transfer(ADMIN, TEAM_OP, 540_000, at="2026-09-12")

    w.readiness = {
        "status": "ready",
        "ratings": {"writer": "scorer", "signer": SIGNER, "scorer": SIGNER},
        "disputes": {"store": "postgres", "reconcile": {"enabled": True, "running": True}},
        "escrow": {"contract": ESCROW_V2, "version": 2},
    }
    w.params = {"enabled": True, "floor_bps": 5500, "prior_bps": 7000, "network": "testnet"}
    w.routes = {REGISTER_ROUTE, *DISPUTE_ROUTES, "GET /readiness"}
    w.pages = {
        REGISTER_PAGE: (200, "<html>register</html>"),
        GUIDE_PAGE: (200, "<html>guide</html>"),
        DEMO_PAGE: (200, demo_html("published", 222)),
    }
    w.repos = {name: (200, "MIT") for name, _ in REPOSITORIES}
    return w

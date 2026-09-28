"""An in-memory Orizon API, Soroban RPC and Horizon, for the hermetic suite.

`FakeWorld.transport()` is an `httpx.MockTransport` that answers all three the
way the deployed services do: the API's routes and bodies as the backend
serves them (see `api.py` for each route's source), the RPC's JSON-RPC methods
with real XDR, and Horizon's transaction records. It keeps the ledger honest
enough to verify against — a signed authorize must carry a valid buyer
signature over the right network, a dispute signature must verify as SEP-53
against the payer, v2 custody moves balances exactly as the interface says —
so a harness that signs the wrong thing, or checks the wrong event, fails here
the way it would fail on testnet.

Knobs on the world turn on the failure paths: a sleeping backend, a submit or
an uphold whose answer is lost, a restart that forgets the task, a step that
does not deliver, mainnet.

Not a production module. It lives in the package only because the suite's
several test files share it, and it imports nothing from `app/`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx
from stellar_sdk import Account, Address, Keypair, TransactionBuilder, TransactionEnvelope, scval
from stellar_sdk import xdr as stellar_xdr
from stellar_sdk.operation import InvokeHostFunction

from .config import TESTNET_PASSPHRASE
from .signing import usdc_to_stroops

API = "https://api.fake"
RPC = "https://rpc.fake"
HORIZON = "https://horizon-testnet.stellar.org"

ESCROW = "CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI"
REGISTRY = "CAPHXWU53UZUZJGV7IAE57NNMH3YYB5MTWO6YA53KKMXSFVLOITBJ3GQ"
LEDGER = "CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT"
ATTEST = "CBYUZKOET43UXTBXZUJIBBJW5ODGD2J2AZVVXCR3QONGOCAHOXQQHEGK"
SAC = "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC"
MAINNET_PASSPHRASE = "Public Global Stellar Network ; September 2015"

AGENT = "ext_agent"
AGENT_NAME = "External Agent"
SEEDED = "agt_writer"
SEEDED_NAME = "Writer"
FEE = 100


def _b64(val: stellar_xdr.SCVal) -> str:
    return str(val.to_xdr())


def _json(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def _err(status: int, code: str, message: str = "") -> httpx.Response:
    return _json(status, {"detail": code, "error": {"code": code, "message": message or code, "request_id": "req_x"}})


@dataclass
class FakeWorld:
    buyer: str
    adjudicator_key: str = "operator-key-0123456789"
    escrow_version: int = 2
    passphrase: str = TESTNET_PASSPHRASE
    rpc_passphrase: str | None = None
    owner: str = field(default_factory=lambda: Keypair.random().public_key)
    settler: str = field(default_factory=lambda: Keypair.random().public_key)
    agent_bound: bool = True
    task_auth_required: bool = True
    d067: bool = True  # the read-grant routes exist
    v2_receipt_fields: bool = True  # settle lane's paid_usdc / receipt_id_hex
    undelivered: set[str] = field(default_factory=set)
    agent_price: float = 0.05
    seeded_price: float = 0.02
    polls_to_finish: int = 2

    # failure knobs
    health_failures: int = 0
    submit_mode: str = "ok"  # ok | transport | submit_failed | timeout_status | api_failed_status
    submit_lands: bool = True  # whether a lost submit reached the ledger anyway
    execute_mode: str = "ok"  # ok | transport | capacity
    open_dispute_mode: str = "ok"  # ok | transport | expired_once | duplicate
    uphold_mode: str = "ok"  # ok | transport | unconfirmed | refused
    refund_lags: int = 0  # dispute reads before the credit shows
    credit_stuck: bool = False  # a `crediting` dispute never resolves (an in-flight transfer)
    restarted: bool = False  # the backend forgot every task (AC4)
    charge_succeeds: bool = True  # v1 only
    plan_routes_agent: bool = True
    wrong_escrow_in_xdr: bool = False
    tamper_payout: bool = False  # v2 settle pays the operator one stroop short
    crash_on: str | None = None  # a route that raises inside the transport
    readiness_reachable: bool = True  # False: the frontend proxy, which forwards /api/* only
    events_forgotten: bool = False  # getEvents refuses: the settle is past the RPC's retention

    # ledger
    ledger: int = 1000
    balances: dict[str, int] = field(default_factory=dict)
    txs: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    authorizations: dict[str, dict[str, Any]] = field(default_factory=dict)
    attestations: dict[str, dict[str, Any]] = field(default_factory=dict)

    # api state
    plan: dict[str, Any] | None = None
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    traces: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    settlements: dict[str, dict[str, Any]] = field(default_factory=dict)
    disputes: dict[str, dict[str, Any]] = field(default_factory=dict)
    challenges: dict[tuple[str, int], str] = field(default_factory=dict)
    read_nonces: dict[str, str] = field(default_factory=dict)
    grants: set[str] = field(default_factory=set)
    rep: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    posted_bodies: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.balances.setdefault(self.buyer, 10_000 * 10_000_000)
        self.balances.setdefault(self.owner, 50 * 10_000_000)
        self.rep = {
            "agent_id": AGENT,
            "smoothed_bps": 7000,
            "lower_bound_bps": 5677,
            "avg_bps": 0,
            "count": 0,
            "weight": 0,
            "disputed": 0,
            "dispute_rate_bps": 0,
            "source": "prior",
            "degraded": False,
            "stale": False,
        }

    # ── helpers ─────────────────────────────────────────────────
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _tx(self, status: str = "SUCCESS", fee: int = FEE) -> str:
        self.ledger += 1
        tx_hash = secrets.token_hex(32)
        self.txs[tx_hash] = {"status": status, "ledger": self.ledger, "fee_charged": fee}
        return tx_hash

    def _event(self, contract: str, tx_hash: str, topics: list[stellar_xdr.SCVal], value: stellar_xdr.SCVal) -> None:
        self.events.append(
            {
                "type": "contract",
                "ledger": self.txs[tx_hash]["ledger"],
                "contractId": contract,
                "id": f"{self.txs[tx_hash]['ledger']:012d}-{len(self.events):010d}",
                "topic": [_b64(t) for t in topics],
                "value": _b64(value),
                "inSuccessfulContractCall": True,
                "txHash": tx_hash,
            }
        )

    def call_log(self, path: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"] == path]

    # ── dispatch ────────────────────────────────────────────────
    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content) if request.content else None
        if request.content:
            self.posted_bodies.append(request.content.decode("utf-8", "replace"))
        path = request.url.path
        self.calls.append({"method": request.method, "path": path, "headers": dict(request.headers), "json": body})
        if self.crash_on and path == self.crash_on:
            raise RuntimeError(f"fake crash on {path}")
        if url.startswith(RPC):
            return self.rpc(body or {})
        if url.startswith(HORIZON):
            return self.horizon(path)
        if url.startswith(API):
            return self.api(request.method, path, body, request.headers)
        return httpx.Response(599, text=f"no fake for {url}")

    # ── the API ─────────────────────────────────────────────────
    def api(self, method: str, path: str, body: Any, headers: httpx.Headers) -> httpx.Response:
        if path == "/api/health":
            if self.health_failures > 0:
                self.health_failures -= 1
                return httpx.Response(503, text="<html>waking</html>")
            return _json(200, {"status": "ok", "version": "t", "uptime_seconds": 1.0})
        if path == "/readiness":
            if not self.readiness_reachable:
                return httpx.Response(404, text="<html>not found</html>")
            return _json(
                200,
                {"status": "ready", "ratings": {"writer": "scorer", "signer": self.settler, "scorer": self.settler}},
            )
        if path == "/api/stellar/network":
            return _json(
                200,
                {
                    "network": "testnet" if self.passphrase == TESTNET_PASSPHRASE else "mainnet",
                    "rpc_url": RPC,
                    "network_passphrase": self.passphrase,
                    "admin": self.settler,
                    "dispatch_signer": None,
                    "asset": "native",
                    "asset_sac": SAC,
                    "contracts": {
                        "agent_registry": REGISTRY,
                        "reputation_ledger": LEDGER,
                        "payment_escrow": ESCROW,
                        "attestation_registry": ATTEST,
                    },
                },
            )
        if path == "/api/agents":
            return _json(
                200,
                [
                    {
                        "id": AGENT,
                        "name": AGENT_NAME,
                        "skills": ["code"],
                        "price": self.agent_price,
                        "rep": 4.0,
                        "status": "online",
                        "runs": 0,
                        "owner": self.owner,
                        "source": "onchain",
                        "bound": self.agent_bound,
                    },
                    {
                        "id": SEEDED,
                        "name": SEEDED_NAME,
                        "skills": ["copy"],
                        "price": self.seeded_price,
                        "rep": 4.0,
                        "status": "online",
                        "runs": 0,
                        "owner": None,
                        "source": "seeded",
                        "bound": None,
                    },
                ],
            )
        if path == f"/api/stellar/agent/{AGENT}":
            return _json(200, {"agent": {"owner": self.owner, "name": AGENT_NAME}})
        if path.startswith("/api/stellar/agent/"):
            return _err(404, "agent_read_failed")
        if path == "/api/stellar/reputation":
            return _json(200, {"reputations": {AGENT: dict(self.rep)}, "floor_bps": 5500, "prior_bps": 7000})
        if path == "/api/orchestrator/decompose" and method == "POST":
            return self.decompose(body)
        if path == "/api/stellar/build/authorize":
            return self.build_authorize(body)
        if path == "/api/stellar/submit":
            return self.submit(body)
        if path == "/api/orchestrator/execute":
            return self.execute(body)
        if path.startswith("/api/tasks/") and path.endswith("/disputes"):
            return self.task_disputes(path.split("/")[3], headers)
        if path.startswith("/api/tasks/"):
            return self.task(path.split("/")[3], headers)
        if path.startswith("/api/trace/"):
            return self.trace(path.split("/")[3], headers)
        if path == "/api/disputes/challenge":
            return self.dispute_challenge(body)
        if path == "/api/disputes/read-challenge":
            return self.read_challenge(body) if self.d067 else _err(404, "not_found")
        if path == "/api/disputes/read-grant":
            return self.read_grant(body) if self.d067 else _err(404, "not_found")
        if path == "/api/disputes" and method == "POST":
            return self.open_dispute(body)
        if path.startswith("/api/disputes/") and path.endswith("/uphold"):
            return self.uphold(path.split("/")[3], headers)
        if path.startswith("/api/disputes/"):
            return self.get_dispute(path.split("/")[3])
        return _err(404, "not_found")

    def decompose(self, body: Any) -> httpx.Response:
        steps: list[dict[str, Any]] = [
            {"agent_id": SEEDED, "agent_name": SEEDED_NAME, "rationale": "copy", "est_price_usdc": self.seeded_price},
        ]
        if self.plan_routes_agent:
            steps.insert(
                0,
                {"agent_id": AGENT, "agent_name": AGENT_NAME, "rationale": "code", "est_price_usdc": self.agent_price},
            )
        total = round(sum(s["est_price_usdc"] for s in steps), 7)
        self.plan = {"plan_id": "pln_0a1b2c3d", "intent": body["intent"], "steps": steps, "total_usdc": total}
        return _json(200, {**self.plan, "total_eta": 10.0, "notices": [], "floor_bps": 5500})

    def build_authorize(self, body: dict[str, Any]) -> httpx.Response:
        expires_at = 1_900_000_000 + int(body["ttl_seconds"])
        contract = REGISTRY if self.wrong_escrow_in_xdr else ESCROW
        tx = (
            TransactionBuilder(Account(body["payer"], 77), network_passphrase=self.passphrase, base_fee=FEE)
            .append_invoke_contract_function_op(
                contract,
                "authorize",
                [
                    scval.to_address(body["payer"]),
                    scval.to_symbol(body["agent_id"]),
                    scval.to_int128(usdc_to_stroops(body["max_amount_usdc"])),
                    scval.to_uint64(expires_at),
                ],
            )
            .set_timeout(30)
            .build()
        )
        return _json(200, {"xdr": tx.to_xdr(), "expires_at": expires_at})

    def submit(self, body: dict[str, Any]) -> httpx.Response:
        env = TransactionEnvelope.from_xdr(body["signed_xdr"], self.passphrase)
        source = env.transaction.source.account_id
        Keypair.from_public_key(source).verify(env.hash(), env.signatures[0].signature)  # raises if unsigned
        tx_hash = env.hash_hex()
        invoke = env.transaction.operations[0]
        assert isinstance(invoke, InvokeHostFunction) and invoke.host_function.invoke_contract is not None
        args: list[Any] = [scval.to_native(a) for a in invoke.host_function.invoke_contract.args]
        label, max_amount, expires_at = args[1], int(args[2]), int(args[3])
        auth_id = secrets.token_hex(16)
        lands = self.submit_mode == "ok" or self.submit_lands
        if lands:
            self.ledger += 1
            self.txs[tx_hash] = {"status": "SUCCESS", "ledger": self.ledger, "fee_charged": FEE}
            self.balances[source] -= FEE
            if self.escrow_version >= 2:
                self.balances[source] -= max_amount
            self.authorizations[auth_id] = {
                "payer": source,
                "agent_id": label,
                "max_amount": max_amount,
                "spent": 0,
                "expires_at": expires_at,
                "revoked": False,
                "settled": False,
            }
        if self.submit_mode == "transport":
            raise httpx.ReadTimeout("fake: the submit's answer was lost")
        if self.submit_mode == "submit_failed":
            return _err(400, "submit_failed")
        if self.submit_mode == "timeout_status":
            return _json(200, {"hash": tx_hash, "status": "timeout"})
        if self.submit_mode == "api_failed_status":
            self.txs[tx_hash]["status"] = "FAILED"
            return _json(200, {"hash": tx_hash, "status": "FAILED", "ledger": self.ledger, "return_value": None})
        return _json(
            200,
            {
                "hash": tx_hash,
                "status": "SUCCESS",
                "ledger": self.ledger,
                "return_value": auth_id,
                "diagnostic": "no diagnostic events",
                "explorer": f"https://stellar.expert/explorer/testnet/tx/{tx_hash}",
            },
        )

    def execute(self, body: dict[str, Any]) -> httpx.Response:
        if self.execute_mode == "transport":
            raise httpx.RemoteProtocolError("fake: connection dropped")
        if self.execute_mode == "capacity":
            return _err(503, "capacity_exhausted")
        auth = self.authorizations.get(body.get("auth_id_hex") or "")
        if auth is None or auth["payer"] != body.get("payer"):
            return _err(409, "authorization_mismatch")
        task_id = f"tsk_{secrets.token_hex(6)}"
        token = f"rt_{secrets.token_hex(16)}"
        self.tokens[task_id] = token
        self.tasks[task_id] = {
            "id": task_id,
            "intent": (self.plan or {}).get("intent", ""),
            "agents": len((self.plan or {}).get("steps", [])),
            "spent": 0.0,
            "status": "running",
            "started_at": 1.0,
            "started": "just now",
            "charge_tx": None,
            "proof_tx": None,
            "artifact": None,
            "_polls": 0,
            "_auth": body["auth_id_hex"],
            "_payer": body["payer"],
        }
        self.traces[task_id] = [{"t": "0.0", "level": "input", "msg": "workflow started"}]
        return _json(200, {"task_id": task_id, "read_token": token})

    def _authorized(self, task_id: str, headers: httpx.Headers) -> bool:
        if not self.task_auth_required:
            return True
        if headers.get("x-api-key") == self.adjudicator_key:
            return True
        return headers.get("x-task-token") == self.tokens.get(task_id)

    def task(self, task_id: str, headers: httpx.Headers) -> httpx.Response:
        task = self.tasks.get(task_id)
        if self.restarted or task is None or not self._authorized(task_id, headers):
            return _err(404, "unknown_task")
        task["_polls"] += 1
        if task["status"] == "running" and task["_polls"] >= self.polls_to_finish:
            self.finish(task_id)
        return _json(200, {k: v for k, v in task.items() if not k.startswith("_")})

    def trace(self, task_id: str, headers: httpx.Headers) -> httpx.Response:
        if self.restarted or task_id not in self.tasks or not self._authorized(task_id, headers):
            return _err(404, "unknown_task")
        return _json(200, self.traces[task_id])

    def finish(self, task_id: str) -> None:
        """Run the workflow: ratings, settle (v2) or charge (v1), seal."""
        task = self.tasks[task_id]
        steps = (self.plan or {})["steps"]
        auth_id, payer = task["_auth"], task["_payer"]
        auth = self.authorizations[auth_id]
        job = secrets.token_hex(16)
        trace = self.traces[task_id]
        delivered = [s for s in steps if s["agent_id"] not in self.undelivered]

        # one rating per dispatched step, as `_submit_ratings` writes them
        for s in steps:
            rating = 90 if s in delivered else 20
            tx = self._tx()
            self._event(
                LEDGER,
                tx,
                [scval.to_symbol("rated"), scval.to_symbol(s["agent_id"])],
                scval.to_vec(
                    [
                        scval.to_uint32(rating),
                        scval.to_int128(1000),
                        scval.to_bytes(bytes.fromhex(job)),
                        scval.to_symbol("run"),
                    ]
                ),
            )
            trace.append(
                {
                    "t": "1.0",
                    "level": "proof",
                    "msg": f"reputation → {s['agent_name']} rated {rating}/100 · tx {tx[:10]}…",
                }
            )
            if s["agent_id"] == AGENT:
                self.rep.update(
                    count=self.rep["count"] + 1,
                    source="onchain",
                    smoothed_bps=self.rep["smoothed_bps"] + (400 if rating > 50 else -900),
                    lower_bound_bps=self.rep["lower_bound_bps"] + (200 if rating > 50 else -700),
                )

        paid_steps: list[tuple[int, dict[str, Any], int, str]] = []
        if self.escrow_version >= 2:
            charge_tx = self._tx(fee=FEE)
            spent = 0
            for index, s in enumerate(steps):
                if s not in delivered or s["agent_id"] != AGENT:
                    continue  # a seeded agent has no on-chain owner, so no payout
                amount = usdc_to_stroops(s["est_price_usdc"])
                receipt = secrets.token_hex(16)
                self._event(
                    ESCROW,
                    charge_tx,
                    [scval.to_symbol("charged"), scval.to_symbol(s["agent_id"])],
                    scval.to_vec(
                        [
                            scval.to_bytes(bytes.fromhex(receipt)),
                            scval.to_bytes(bytes.fromhex(auth_id)),
                            scval.to_int128(amount),
                            scval.to_bytes(bytes.fromhex(job)),
                        ]
                    ),
                )
                self.balances[self.owner] += amount - (1 if self.tamper_payout else 0)
                spent += amount
                paid_steps.append((index, s, amount, receipt))
            returned = auth["max_amount"] - spent
            self.balances[payer] += returned
            auth.update(spent=spent, settled=True)
            self._event(
                ESCROW,
                charge_tx,
                [scval.to_symbol("settled")],
                scval.to_vec(
                    [
                        scval.to_bytes(bytes.fromhex(auth_id)),
                        scval.to_bytes(bytes.fromhex(job)),
                        scval.to_int128(spent),
                        scval.to_int128(returned),
                    ]
                ),
            )
            settled_usdc = spent / 10_000_000
            settled = True
        else:
            charge_tx = self._tx(status="SUCCESS" if self.charge_succeeds else "FAILED")
            settled_usdc = sum(s["est_price_usdc"] for s in delivered)
            settled = self.charge_succeeds
            if settled:
                self._event(
                    ESCROW,
                    charge_tx,
                    [scval.to_symbol("charged"), scval.to_symbol("orizon_batch")],
                    scval.to_vec(
                        [
                            scval.to_bytes(secrets.token_bytes(16)),
                            scval.to_bytes(bytes.fromhex(auth_id)),
                            scval.to_int128(usdc_to_stroops(settled_usdc)),
                            scval.to_bytes(bytes.fromhex(job)),
                        ]
                    ),
                )

        proof_tx = None
        if settled:
            proof_tx = self._tx()
            self.attestations[job] = {
                "orchestrator": payer,
                "intent_hash": hashlib.sha256(b"intent").hexdigest(),
                # every plan step's agent, as `_settle_onchain` seals them
                "agents": [s["agent_id"] for s in steps],
                "receipts": [r for _, _, _, r in paid_steps],
                "total_spent": usdc_to_stroops(settled_usdc),
                "sealed_at": 1_900_000_100,
            }
            view_steps = []
            for index, s in enumerate(steps):
                paid = next(((a, r) for i, _, a, r in paid_steps if i == index), None)
                step = {
                    "step_index": index,
                    "agent_id": s["agent_id"],
                    "agent_name": s["agent_name"],
                    "price_usdc": s["est_price_usdc"],
                    "delivered": s in delivered,
                    "creditable_usdc": s["est_price_usdc"] if s in delivered else 0.0,
                    "output_summary": "ok" if s in delivered else None,
                }
                if self.v2_receipt_fields and self.escrow_version >= 2:
                    step["paid_usdc"] = paid[0] / 10_000_000 if paid else None
                    step["receipt_id_hex"] = paid[1] if paid else None
                view_steps.append(step)
            self.settlements[task_id] = {
                "job_id_hex": job,
                "payer": payer,
                "settled_at": 1_900_000_000.0,
                "window_closes_at": 1_900_086_400.0,
                "settled_usdc": settled_usdc,
                "charge_tx": charge_tx,
                "proof_tx": proof_tx,
                "steps": view_steps,
                "policy": {"credited_fraction": 1.0, "funded_by": "platform", "adjudicated_by": "platform"},
            }
        task.update(status="complete", spent=settled_usdc, charge_tx=charge_tx, proof_tx=proof_tx)

    def task_disputes(self, task_id: str, headers: httpx.Headers) -> httpx.Response:
        grant_ok = headers.get("x-dispute-read-grant") in self.grants
        if self.task_auth_required and not (self._authorized(task_id, headers) and not self.restarted) and not grant_ok:
            return _err(404, "unknown_task")
        settlement = self.settlements.get(task_id)
        return _json(
            200,
            {
                "task_id": task_id,
                "window_closes_at": settlement["window_closes_at"] if settlement else None,
                "now": 1_900_000_500.0,
                "settlement": settlement,
                "disputes": [d for d in self.disputes.values() if d["task_id"] == task_id],
            },
        )

    def _settlement_by_job(self, job: str) -> tuple[str, dict[str, Any]] | None:
        for task_id, s in self.settlements.items():
            if s["job_id_hex"] == job:
                return task_id, s
        return None

    def dispute_challenge(self, body: dict[str, Any]) -> httpx.Response:
        if self._settlement_by_job(body["job_id_hex"]) is None:
            return _err(404, "unknown_job")
        key = (body["job_id_hex"], int(body["step_index"]))
        nonce = self.challenges.setdefault(key, secrets.token_hex(16))
        message = f"orizon-dispute:v1:{key[0]}:{key[1]}:{nonce}"
        return _json(200, {"message": message, "nonce": nonce, "expires_at": 1_900_000_800.0})

    def open_dispute(self, body: dict[str, Any]) -> httpx.Response:
        found = self._settlement_by_job(body["job_id_hex"])
        if found is None:
            return _err(404, "unknown_job")
        task_id, settlement = found
        key = (body["job_id_hex"], int(body["step_index"]))
        if self.open_dispute_mode == "expired_once":
            self.open_dispute_mode = "ok"
            self.challenges.pop(key, None)
            return _err(400, "challenge_expired")
        nonce = self.challenges.get(key)
        message = f"orizon-dispute:v1:{key[0]}:{key[1]}:{nonce}"
        if nonce != body["nonce"]:
            return _err(400, "challenge_expired")
        try:
            Keypair.from_public_key(settlement["payer"]).verify_message(
                message, base64.b64decode(body["signature_b64"])
            )
        except Exception:
            return _err(403, "not_the_payer")
        del self.challenges[key]
        for d in self.disputes.values():
            if (d["job_id_hex"], d["step_index"]) == key:
                return _json(
                    409,
                    {
                        "detail": "duplicate_dispute",
                        "error": {"code": "duplicate_dispute", "message": "dup"},
                        "dispute": d,
                    },
                )
        step = settlement["steps"][key[1]]
        dispute_id = f"dsp_{secrets.token_hex(8)}"
        record = {
            "id": dispute_id,
            "job_id_hex": key[0],
            "task_id": task_id,
            "step_index": key[1],
            "agent_id": step["agent_id"],
            "payer": settlement["payer"],
            "reason": body["reason"],
            "status": "open",
            "charged_usdc": step["price_usdc"],
            "creditable_usdc": step["creditable_usdc"],
            "opened_at": 1_900_000_600.0,
            "resolved_at": None,
            "refund_tx": None,
            "rating_tx": None,
            "credited_usdc": None,
            "rating_confirmed": None,
            "_reads": 0,
        }
        self.disputes[dispute_id] = record
        if self.open_dispute_mode == "transport":
            raise httpx.ReadTimeout("fake: the open's answer was lost")
        return _json(200, {k: v for k, v in record.items() if not k.startswith("_")})

    def read_challenge(self, body: dict[str, Any]) -> httpx.Response:
        nonce = self.read_nonces.setdefault(body["task_id"], secrets.token_hex(16))
        return _json(
            200,
            {"nonce": nonce, "message": f"orizon-dispute-read:v1:{body['task_id']}:{nonce}", "expires_at": 1.0},
        )

    def read_grant(self, body: dict[str, Any]) -> httpx.Response:
        settlement = self.settlements[body["task_id"]]
        nonce = self.read_nonces.pop(body["task_id"])
        message = f"orizon-dispute-read:v1:{body['task_id']}:{nonce}"
        Keypair.from_public_key(settlement["payer"]).verify_message(message, base64.b64decode(body["signature_b64"]))
        grant = f"g1.{secrets.token_hex(12)}.{secrets.token_hex(12)}"
        self.grants.add(grant)
        return _json(200, {"grant": grant, "expires_at": 2.0})

    def get_dispute(self, dispute_id: str) -> httpx.Response:
        d = self.disputes.get(dispute_id)
        if d is None:
            return _err(404, "unknown_dispute")
        d["_reads"] += 1
        if d["status"] == "crediting" and d["_reads"] > self.refund_lags and not self.credit_stuck:
            self._credit(d)
        return _json(200, {k: v for k, v in d.items() if not k.startswith("_")})

    def _credit(self, d: dict[str, Any]) -> None:
        amount = usdc_to_stroops(d["creditable_usdc"])
        refund = self._tx()
        self.balances[d["payer"]] += amount
        rating = self._tx()
        self._event(
            LEDGER,
            rating,
            [scval.to_symbol("rated"), scval.to_symbol(d["agent_id"])],
            scval.to_vec(
                [
                    scval.to_uint32(0),
                    scval.to_int128(1000),
                    scval.to_bytes(secrets.token_bytes(16)),
                    scval.to_symbol("dispute"),
                ]
            ),
        )
        d.update(
            status="credited",
            refund_tx=refund,
            rating_tx=rating,
            credited_usdc=d["creditable_usdc"],
            rating_confirmed=True,
        )
        self.rep.update(
            disputed=self.rep["disputed"] + 1,
            count=self.rep["count"] + 1,
            smoothed_bps=self.rep["smoothed_bps"] - 1500,
            lower_bound_bps=self.rep["lower_bound_bps"] - 1200,
        )

    def uphold(self, dispute_id: str, headers: httpx.Headers) -> httpx.Response:
        if headers.get("x-api-key") != self.adjudicator_key:
            return _err(401, "invalid_api_key")
        d = self.disputes.get(dispute_id)
        if d is None:
            return _err(404, "unknown_dispute")
        if self.uphold_mode == "refused":
            return _err(409, "refund_above_cap")
        d["status"] = "crediting"
        if self.uphold_mode == "transport":
            raise httpx.ReadTimeout("fake: the uphold's answer was lost")
        if self.uphold_mode == "unconfirmed":
            d["refund_tx"] = secrets.token_hex(32)
            return _err(504, "refund_unconfirmed")
        if self.refund_lags == 0:
            self._credit(d)
        return _json(200, {k: v for k, v in d.items() if not k.startswith("_")})

    # ── Soroban RPC ─────────────────────────────────────────────
    def rpc(self, payload: dict[str, Any]) -> httpx.Response:
        method, params = payload.get("method"), payload.get("params") or {}

        def ok(result: Any) -> httpx.Response:
            return _json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "result": result})

        if method == "getNetwork":
            return ok({"passphrase": self.rpc_passphrase or self.passphrase, "protocolVersion": 23})
        if method == "getLatestLedger":
            return ok({"sequence": self.ledger, "id": "x", "protocolVersion": 23})
        if method == "getTransaction":
            tx = self.txs.get(params["hash"])
            if tx is None:
                return ok({"status": "NOT_FOUND", "latestLedger": self.ledger})
            return ok({"status": tx["status"], "ledger": tx["ledger"], "latestLedger": self.ledger})
        if method == "getEvents":
            if self.events_forgotten:
                return _json(
                    200,
                    {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32600, "message": "startLedger"}},
                )
            ids = params["filters"][0]["contractIds"]
            start = params.get("startLedger", 0)
            events = [e for e in self.events if e["contractId"] in ids and e["ledger"] >= start]
            return ok({"events": events, "latestLedger": self.ledger, "cursor": ""})
        if method == "simulateTransaction":
            return ok(self.simulate(params["transaction"]))
        return _json(200, {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32601, "message": "no"}})

    def simulate(self, tx_xdr: str) -> dict[str, Any]:
        env = TransactionEnvelope.from_xdr(tx_xdr, self.passphrase)
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

        if contract == ESCROW and fn == "version":
            return (
                value(scval.to_uint32(2))
                if self.escrow_version >= 2
                else fail("HostError: Error(WasmVm, MissingValue)")
            )
        if contract == SAC and fn == "balance":
            if args[0].address not in self.balances:
                # testnet's native SAC traps on a missing account (seen live)
                return fail("HostError: Error(Contract, #6)\n\nEvent log (newest first): account entry is missing")
            return value(scval.to_int128(self.balances[args[0].address]))
        if contract == ATTEST and fn == "get":
            a = self.attestations.get(bytes(args[0]).hex())
            if a is None:
                return fail("HostError: Error(Contract, #2)")
            return value(
                scval.to_map(
                    {
                        scval.to_symbol("agents"): scval.to_vec([scval.to_symbol(x) for x in a["agents"]]),
                        scval.to_symbol("intent_hash"): scval.to_bytes(bytes.fromhex(a["intent_hash"])),
                        scval.to_symbol("orchestrator"): scval.to_address(a["orchestrator"]),
                        scval.to_symbol("receipts"): scval.to_vec(
                            [scval.to_bytes(bytes.fromhex(r)) for r in a["receipts"]]
                        ),
                        scval.to_symbol("sealed_at"): scval.to_uint64(a["sealed_at"]),
                        scval.to_symbol("total_spent"): scval.to_int128(a["total_spent"]),
                    }
                )
            )
        if contract == ESCROW and fn == "settler":
            if self.escrow_version < 2:
                return fail("HostError: Error(WasmVm, MissingValue)")
            return value(scval.to_address(self.settler))
        if contract == ESCROW and fn == "authorization":
            a = self.authorizations.get(bytes(args[0]).hex())
            if a is None:
                return fail("HostError: Error(Contract, #2)")
            return value(
                scval.to_map(
                    {
                        scval.to_symbol("agent_id"): scval.to_symbol(a["agent_id"]),
                        scval.to_symbol("expires_at"): scval.to_uint64(a["expires_at"]),
                        scval.to_symbol("max_amount"): scval.to_int128(a["max_amount"]),
                        scval.to_symbol("payer"): scval.to_address(a["payer"]),
                        scval.to_symbol("revoked"): scval.to_bool(a["revoked"]),
                        scval.to_symbol("settled"): scval.to_bool(a["settled"]),
                        scval.to_symbol("spent"): scval.to_int128(a["spent"]),
                    }
                )
            )
        return fail(f"no fake for {contract}.{fn}")

    # ── Horizon ─────────────────────────────────────────────────
    def horizon(self, path: str) -> httpx.Response:
        if path.startswith("/transactions/"):
            tx = self.txs.get(path.split("/")[2])
            if tx is None:
                return _json(404, {"status": 404})
            return _json(
                200,
                {
                    "successful": tx["status"] == "SUCCESS",
                    "ledger": tx["ledger"],
                    "fee_charged": str(tx["fee_charged"]),
                },
            )
        return _json(404, {"status": 404})

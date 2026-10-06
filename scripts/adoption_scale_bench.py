"""Adoption report at scale — an offline reproduction of the live build (D-091).

Builds the adoption report, and runs one registry-sync pass, against a fake
Soroban RPC that answers like testnet's public node does — the same latencies,
an occasional read that times out — for a registry of N agents. Nothing leaves
the machine: the fake stands in at `sc._server()`, so the real client code
(envelope building, XDR decoding, the bounded thread pool) runs as it does in
production.

Time is compressed: every simulated latency is multiplied by `--scale`, and so
is every `*_SECONDS` budget in the modules under test, so a build that would
take 15 minutes live runs in seconds here. The projection back to live time is
`(wall - cpu) / scale + cpu` — waits scale, the Python work does not.

    python -m scripts.adoption_scale_bench --agents 1000
    python -m scripts.adoption_scale_bench --agents 5000 --mirror half

Latencies are the ones measured against soroban-testnet.stellar.org on
2026-10-06: getEvents 0.24 s a page, simulateTransaction 0.27-0.30 s,
getLedgerEntries 0.57 s for 200 keys, getLatestLedger ~0.1 s, and the
`load_account` hop ~0.2 s.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stellar_sdk import Account, Address, Keypair, scval  # noqa: E402
from stellar_sdk import xdr as stellar_xdr  # noqa: E402
from stellar_sdk.soroban_rpc import (  # noqa: E402
    EventInfo,
    GetEventsResponse,
    GetLedgerEntriesResponse,
    LedgerEntryResult,
    SimulateHostFunctionResult,
    SimulateTransactionResponse,
)

from app.config import settings  # noqa: E402
from app.services import adoption_svc, registry_sync, snapshot_store  # noqa: E402
from app.services.binding_store import InMemoryBindingStore  # noqa: E402
from app.state import state  # noqa: E402
from app.stellar import cache as rcache  # noqa: E402
from app.stellar import client as sc  # noqa: E402

REGISTRY = "CAPHXWU53UZUZJGV7IAE57NNMH3YYB5MTWO6YA53KKMXSFVLOITBJ3GQ"
ESCROW = "CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4"
SAC = "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC"
ADMIN = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
SETTLER = "GDB4N25UYM3YNTTAWX7LSGI2P7OR62QZQXRNQWAGF5TFVENDKCTTCDHP"

LATEST = 5_047_925
OLDEST = LATEST - 120_958
CLOSE = 1_790_000_000

# Seconds, live. Multiplied by --scale.
LATENCY = {
    "load_account": 0.20,
    "simulate": 0.29,
    "get_events": 0.24,
    "get_latest_ledger": 0.10,
    "get_ledger_entries": 0.57,
    "binding_get": 0.02,
    "binding_list": 0.05,
}
# The read profile's timeout (`sc._server()`): a call that hangs costs this.
READ_TIMEOUT = 5.0

# What the binding store and the registry hold per agent, roughly as live.
BOUND_SHARE = 0.03
CHARGED_AGENTS = 5


def _gaddr(n: int) -> str:
    return Keypair.from_raw_ed25519_seed(n.to_bytes(32, "big")).public_key


class FakeRpc:
    """A SorobanServer that answers from an in-memory registry and escrow."""

    def __init__(self, n: int, *, scale: float, timeout_rate: float, seed: int) -> None:
        self.scale = scale
        self.timeout_rate = timeout_rate
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.calls: Counter[str] = Counter()
        self.timeouts = 0
        # ~99% distinct owners, as on the live registry (1,017 agents, ~990 owners).
        owners = [_gaddr(1000 + (i if i % 100 else i - 1)) for i in range(n)]
        self.records: dict[str, dict[str, Any]] = {}
        for i in range(n):
            agent_id = f"ext_{i:05d}"
            self.records[agent_id] = {"id": agent_id, "owner": owners[i], "name": f"agent {i}", "price": 100_000}
        for i in range(20):  # the team's own agents
            agent_id = f"team_{i:02d}"
            self.records[agent_id] = {"id": agent_id, "owner": ADMIN, "name": f"team {i}", "price": 100_000}
        self.ids = list(self.records)
        self.payers: dict[str, str] = {}
        self.events: list[EventInfo] = []
        paid = self.ids[:: max(1, n // CHARGED_AGENTS)][:CHARGED_AGENTS]
        for k, agent_id in enumerate(paid):
            auth = bytes([k + 1]) * 16
            self.payers[auth.hex()] = _gaddr(9_000 + k)
            self.events.append(self._charged(agent_id, LATEST - 1_000 * (k + 1), auth, bytes([0x80 | k]) * 16))

    # ── plumbing ──────────────────────────────────────────────────────────
    def _wait(self, kind: str) -> None:
        with self.lock:
            self.calls[kind] += 1
            hang = self.rng.random() < self.timeout_rate
            if hang:
                self.timeouts += 1
        if hang:
            time.sleep(READ_TIMEOUT * self.scale)
            raise TimeoutError(f"{kind}: read timed out")
        time.sleep(LATENCY[kind] * self.scale)

    @staticmethod
    def _charged(agent_id: str, ledger: int, auth: bytes, job: bytes) -> EventInfo:
        value = scval.to_vec(
            [scval.to_bytes(b"\xee" * 16), scval.to_bytes(auth), scval.to_int128(100_000), scval.to_bytes(job)]
        )
        return EventInfo(
            type="contract",
            ledger=ledger,
            ledgerClosedAt="2026-10-05T08:57:00Z",
            contractId=ESCROW,
            id=f"{ledger:019d}-0000000001",
            topic=[scval.to_symbol("charged").to_xdr(), scval.to_symbol(agent_id).to_xdr()],
            value=value.to_xdr(),
            inSuccessfulContractCall=True,
            operationIndex=0,
            transactionIndex=0,
            txHash=f"{ledger:064x}",
        )

    def _record(self, agent_id: str) -> stellar_xdr.SCVal:
        r = self.records[agent_id]
        return scval.to_map(
            {
                scval.to_symbol("active"): scval.to_bool(True),
                scval.to_symbol("id"): scval.to_symbol(agent_id),
                scval.to_symbol("name"): scval.to_string(r["name"]),
                scval.to_symbol("owner"): scval.to_address(r["owner"]),
                scval.to_symbol("price"): scval.to_int128(r["price"]),
                scval.to_symbol("registered_at"): scval.to_uint64(1_790_000_000),
                scval.to_symbol("skills"): scval.to_vec([scval.to_symbol("bench")]),
            }
        )

    # ── the SorobanServer surface the service uses ────────────────────────
    def load_account(self, address: str) -> Account:
        self._wait("load_account")
        return Account(address, 1)

    def simulate_transaction(self, tx: Any) -> SimulateTransactionResponse:
        self._wait("simulate")
        invoke = tx.transaction.operations[0].host_function.invoke_contract
        contract = Address.from_xdr_sc_address(invoke.contract_address).address
        fn = invoke.function_name.sc_symbol.decode()
        args = [scval.to_native(a) for a in invoke.args]
        try:
            value = self._view(contract, fn, args)
        except KeyError:
            return SimulateTransactionResponse(error="HostError: Error(Contract, #2)", latestLedger=LATEST)
        result = SimulateHostFunctionResult(xdr=value.to_xdr(), auth=[])
        return SimulateTransactionResponse(results=[result], latestLedger=LATEST)

    def _view(self, contract: str, fn: str, args: list[Any]) -> stellar_xdr.SCVal:
        if contract == REGISTRY and fn == "list_ids":
            return scval.to_vec([scval.to_symbol(i) for i in self.ids])
        if contract == REGISTRY and fn == "get":
            return self._record(args[0])
        if contract == REGISTRY and fn == "owner_of":
            return scval.to_address(self.records[args[0]]["owner"])
        if contract == REGISTRY and fn == "admin":
            return scval.to_address(ADMIN)
        if contract == ESCROW and fn == "settler":
            return scval.to_address(SETTLER)
        if contract == ESCROW and fn == "admin":
            return scval.to_address(ADMIN)
        if contract == ESCROW and fn == "authorization":
            return scval.to_map({scval.to_symbol("payer"): scval.to_address(self.payers[bytes(args[0]).hex()])})
        if contract == SAC and fn == "name":
            return scval.to_string("native")
        raise AssertionError(f"unexpected view {contract}.{fn}")

    def get_latest_ledger(self) -> Any:
        self._wait("get_latest_ledger")
        return SimpleNamespace(sequence=LATEST)

    def get_events(
        self,
        start_ledger: int | None = None,
        end_ledger: int | None = None,
        filters: Any = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> GetEventsResponse:
        self._wait("get_events")
        topic = filters[0].topics[0][1] if filters else "*"
        hits = [
            e
            for e in self.events
            if end_ledger is not None
            and start_ledger is not None
            and start_ledger <= e.ledger < end_ledger
            and (topic == "*" or e.topic[1] == topic)
        ]
        return GetEventsResponse(
            events=hits[:limit] if limit else hits,
            latestLedger=LATEST,
            oldestLedger=OLDEST,
            latestLedgerCloseTime=CLOSE + (LATEST - OLDEST) * 5,
            oldestLedgerCloseTime=CLOSE,
            cursor="",
        )

    def get_ledger_entries(self, keys: list[stellar_xdr.LedgerKey]) -> GetLedgerEntriesResponse:
        self._wait("get_ledger_entries")
        entries = []
        for key in keys:
            data = key.contract_data
            agent_id = scval.to_native(data.key)[1]
            if agent_id not in self.records:
                continue
            entry = stellar_xdr.LedgerEntryData(
                type=stellar_xdr.LedgerEntryType.CONTRACT_DATA,
                contract_data=stellar_xdr.ContractDataEntry(
                    ext=stellar_xdr.ExtensionPoint(0),
                    contract=data.contract,
                    key=data.key,
                    durability=data.durability,
                    val=self._record(agent_id),
                ),
            )
            entries.append(
                LedgerEntryResult(
                    key=key.to_xdr(),
                    xdr=entry.to_xdr(),
                    lastModifiedLedgerSeq=LATEST,
                    liveUntilLedgerSeq=LATEST + 10**6,
                )
            )
        return GetLedgerEntriesResponse(entries=entries, latestLedger=LATEST)


class SlowBindingStore(InMemoryBindingStore):
    """The binding store, at a Neon round trip per call."""

    def __init__(self, rpc: FakeRpc, scale: float) -> None:
        super().__init__()
        self.scale = scale
        self.rpc = rpc

    async def get(self, agent_id: str) -> Any:
        self.rpc.calls["binding_get"] += 1
        await asyncio.sleep(LATENCY["binding_get"] * self.scale)
        return await super().get(agent_id)

    async def list_agent_ids(self) -> frozenset[str]:
        self.rpc.calls["binding_list"] += 1
        await asyncio.sleep(LATENCY["binding_list"] * self.scale)
        return await super().list_agent_ids()


def _scale_budgets(scale: float) -> None:
    """Every `*_SECONDS` constant in the modules under test, compressed alike."""
    names = [
        "app.services.adoption_svc",
        "app.services.settlement_svc",
        "app.services.registry_sync",
        "app.services.charge_window",
    ]
    for name in names:
        module: ModuleType | None = sys.modules.get(name)
        if module is None:
            try:
                module = __import__(name, fromlist=["_"])
            except ImportError:
                continue
        for attr in dir(module):
            value = getattr(module, attr)
            if attr.endswith("_SECONDS") and isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(module, attr, value * scale)


def _project(wall: float, cpu: float, scale: float) -> float:
    waits = max(0.0, wall - cpu)
    return waits / scale + cpu


async def _main(args: argparse.Namespace) -> int:
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=8, thread_name_prefix="soroban"))
    scale = args.scale
    rpc = FakeRpc(args.agents, scale=scale, timeout_rate=args.timeout_rate, seed=args.seed)
    store = SlowBindingStore(rpc, scale)
    for agent_id in rpc.ids[:: max(1, int(1 / BOUND_SHARE))]:
        await InMemoryBindingStore.put(store, agent_id, "https://op.example/run", rpc.records[agent_id]["owner"])

    settings.stellar_agent_registry = REGISTRY
    settings.stellar_payment_escrow = ESCROW
    settings.stellar_asset_sac = SAC
    settings.stellar_admin_address = ADMIN
    settings.stellar_signing_key = ""
    settings.database_url = ""
    sc._server = lambda **_kw: rpc  # type: ignore[assignment]
    sc.escrow_version = lambda _id: 2  # type: ignore[assignment]
    adoption_svc.get_binding_store = lambda: store  # type: ignore[assignment]
    snapshot_store._store = None
    budget = adoption_svc.REPORT_BUILD_BUDGET_SECONDS
    _scale_budgets(scale)

    # ── one registry-sync pass, from an empty mirror ──────────────────────
    state.agents.clear()
    rpc.calls.clear()
    wall0, cpu0 = time.perf_counter(), time.process_time()
    try:
        await registry_sync.sync_once()
    except Exception as e:  # list_ids itself failed: the live loop retries in 15 s
        print(f"registry pass failed: {type(e).__name__}: {e}")
    wall, cpu = time.perf_counter() - wall0, time.process_time() - cpu0
    print(
        f"registry pass: {len(state.agents)} agents mirrored · projected {_project(wall, cpu, scale):.0f} s live "
        f"(wall {wall:.1f} s, cpu {cpu:.1f} s) · calls {dict(rpc.calls)}"
    )

    # ── the report build ──────────────────────────────────────────────────
    if args.mirror == "half":
        for agent_id in list(state.agents)[::2]:
            del state.agents[agent_id]
    rcache.clear()
    rpc.calls.clear()
    rpc.timeouts = 0
    wall0, cpu0 = time.perf_counter(), time.process_time()
    outcome = "built"
    report = None
    try:
        report = await asyncio.wait_for(adoption_svc.build_report(), timeout=budget * scale)
    except TimeoutError:
        outcome = f"FAILED: abandoned at the {budget:.0f} s build budget"
    wall, cpu = time.perf_counter() - wall0, time.process_time() - cpu0
    took = f"> {budget:.0f}" if report is None else f"{_project(wall, cpu, scale):.0f}"
    rpc_calls = sum(v for k, v in rpc.calls.items() if not k.startswith("binding"))
    print(
        f"report build ({args.mirror} mirror): {outcome} · projected {took} s live "
        f"(wall {wall:.1f} s, cpu {cpu:.1f} s) · rpc calls {rpc_calls} "
        f"{dict(rpc.calls)} · timeouts injected {rpc.timeouts}"
    )
    if report is not None:
        extra = report.model_dump(include={"complete", "coverage"}) if hasattr(report, "complete") else {}
        print(
            f"  totals {report.totals.model_dump()} degraded={report.degraded} "
            f"unreadable={len(report.unreadable_agents)} window_days={report.window_days} {extra}"
        )
    return 0 if report is not None else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agents", type=int, default=1000, help="external agents in the registry")
    parser.add_argument("--scale", type=float, default=0.02, help="time compression (live seconds x scale)")
    parser.add_argument("--timeout-rate", type=float, default=0.01, help="share of RPC reads that hang to timeout")
    parser.add_argument("--mirror", choices=["full", "half"], default="full", help="registry mirror at build time")
    parser.add_argument("--seed", type=int, default=7)
    # The per-read warnings are the point of a live log and noise here: the
    # summary lines carry the counts.
    logging.basicConfig(level=logging.CRITICAL)
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

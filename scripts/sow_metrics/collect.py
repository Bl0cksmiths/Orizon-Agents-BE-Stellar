"""Every read the metrics need, gathered into one `Snapshot` before anything is judged.

Reading and judging are kept apart so the rules (`metrics.py`) are a pure
function of what the chain and the deployment said, and so a read that failed
is recorded against the SOURCE it failed on. Each metric names the sources it
depends on; if any of them failed, that metric is "Not measured" — it is never
counted as 0 from a read that did not happen.

Sources, by name (the keys of `Snapshot.failures`):

    readiness       the backend's /readiness (the ratings signer and scorer, the refund switch)
    platform_keys   the contracts' role views and instance storage (admins, settler, scorer, sealer)
    registry        AgentRegistry.list_ids and get(id)
    escrow          every escrow's instance Nonce, and receipt(id) / authorization(id) for each id
    ledger          ReputationLedger.rep_state(id) for every agent, for the lifetime dispute count
    history         Horizon's operation history of every platform key (charges, ratings, refunds)
    params          /api/stellar/reputation/params
    openapi         the backend's /openapi.json
    page:<path>     a frontend page
    github          the GitHub repository reads

A failure to find a proof LINK (a registration transaction in an owner's
history) is only a warning: it changes a link, not a count. So is a failure to
read the adoption report (`/api/ecosystem/adoption`), which is read only for
each agent's `bound` flag: it changes a sentence, not a count.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from stellar_sdk import StrKey, scval

from .api import Answer, Reads, Unreachable
from .chain import READ_ERRORS, ChainReader, Operation, SimulationError, id_bytes, id_hex
from .config import DEMO_PAGE, GUIDE_PAGE, KNOWN_ESCROWS, REGISTER_PAGE, REPOSITORIES
from .register import TeamRegister

FAILURES: tuple[type[Exception], ...] = (*READ_ERRORS, Unreachable, SimulationError)


@dataclass(frozen=True)
class AgentRecord:
    id: str
    owner: str
    name: str
    active: bool
    registered_at: int | None


@dataclass(frozen=True)
class Registration:
    agent_id: str
    tx_hash: str
    date: str
    signer: str


@dataclass(frozen=True)
class AuthRecord:
    id_hex: str
    payer: str
    agent_id: str
    max_amount: int
    spent: int


@dataclass(frozen=True)
class ReceiptRecord:
    escrow: str
    version: int
    id_hex: str
    auth_id: str
    agent_id: str
    amount: int
    job_id: str
    settled_at: int


@dataclass
class EscrowScan:
    contract: str
    version: int
    live: bool  # the escrow the live API names
    settler: str | None
    admin: str | None
    nonce: int
    receipts: list[ReceiptRecord] = field(default_factory=list)
    auths: dict[str, AuthRecord] = field(default_factory=dict)
    # ids the nonce issued that read as neither a receipt nor an authorization
    unreadable_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TxRef:
    tx_hash: str
    date: str


@dataclass(frozen=True)
class DisputeRating:
    agent_id: str
    job_id: str
    payer: str | None
    rating: int | None
    tx_hash: str
    created_at: str


@dataclass(frozen=True)
class Transfer:
    """An asset-contract transfer out of a platform key: a refund candidate."""

    source: str
    destination: str
    amount_stroops: int
    tx_hash: str
    created_at: str
    muxed_id: str | None


@dataclass
class Snapshot:
    network: dict[str, Any]
    team: dict[str, str]  # the committed register: address -> role
    platform: dict[str, str] = field(default_factory=dict)  # runtime keys: address -> role
    registry_admin: str | None = None
    readiness: dict[str, Any] | None = None
    agents: list[AgentRecord] = field(default_factory=list)
    registrations: dict[str, Registration] = field(default_factory=dict)
    escrows: list[EscrowScan] = field(default_factory=list)
    charge_txs: dict[tuple[str, str], TxRef] = field(default_factory=dict)  # (escrow, receipt id) -> tx
    ledger_disputed: dict[str, int] = field(default_factory=dict)
    dispute_ratings: list[DisputeRating] = field(default_factory=list)
    rating_kinds: dict[str, int] = field(default_factory=dict)  # every ledger submit seen, by kind
    transfers: list[Transfer] = field(default_factory=list)
    params: dict[str, Any] | None = None
    # agent id -> the adoption report's own `bound` flag (None: the binding
    # store could not be read). None when the report itself could not be read.
    bound: dict[str, bool | None] | None = None
    openapi_routes: set[str] | None = None
    pages: dict[str, Answer] = field(default_factory=dict)
    page_urls: dict[str, str] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)  # source -> the URL it was read from
    repos: dict[str, Answer] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Each account's history as read this run: a platform key that also owns
    # an agent is read once, not twice.
    histories: dict[str, list[Operation]] = field(default_factory=dict)

    # ── derived views ───────────────────────────────────────────
    def contract(self, name: str) -> str | None:
        value = (self.network.get("contracts") or {}).get(name)
        return value if isinstance(value, str) and value else None

    @property
    def asset_sac(self) -> str | None:
        value = self.network.get("asset_sac")
        return value if isinstance(value, str) and value else None

    @property
    def asset_name(self) -> str:
        return "XLM" if self.network.get("asset") == "native" else "USDC"

    def fail(self, source: str, exc: BaseException) -> None:
        text = f"{type(exc).__name__}: {exc}".strip()
        previous = self.failures.get(source)
        self.failures[source] = text if previous is None else f"{previous}; {text}"


def _is_account(value: Any) -> bool:
    return isinstance(value, str) and StrKey.is_valid_ed25519_public_key(value)


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected an integer, got {value!r}")
    return value


# ── the platform's own keys ──────────────────────────────────────────────
def read_platform_keys(snap: Snapshot, chain: ChainReader) -> None:
    """The keys this deployment demonstrably holds or names, each with its role.

    Configuration first (the API's admin and dispatch signer, /readiness's
    ratings signer and scorer), then each contract's own answer: the
    registry's `admin()`, and the admin, settler, scorer and sealer each
    contract holds in its instance storage. A failed read leaves an owner we
    cannot rule out, so it fails `platform_keys` and every count that needs it.
    """

    def add(address: Any, role: str) -> None:
        if _is_account(address):
            snap.platform.setdefault(address, role)

    add(snap.network.get("admin"), "network admin key")
    add(snap.network.get("dispatch_signer"), "dispatch signer")
    ratings = (snap.readiness or {}).get("ratings") or {}
    add(ratings.get("signer"), "ratings signer")
    add(ratings.get("scorer"), "ratings scorer")

    registry = snap.contract("agent_registry")
    if registry:
        try:
            admin = chain.simulate(registry, "admin")
            add(admin, "registry admin")
            snap.registry_admin = admin if _is_account(admin) else None
        except FAILURES as exc:
            snap.fail("platform_keys", exc)
    for name, roles in (
        ("reputation_ledger", {"Admin": "reputation ledger admin", "Scorer": "reputation ledger scorer"}),
        ("attestation_registry", {"Admin": "attestation registry admin", "Sealer": "attestation sealer"}),
    ):
        contract = snap.contract(name)
        if not contract:
            continue
        try:
            storage = chain.instance_storage(contract)
        except FAILURES as exc:
            snap.fail("platform_keys", exc)
            continue
        for key, role in roles.items():
            add(storage.get(key), role)


# ── the registry ─────────────────────────────────────────────────────────
def read_registry(snap: Snapshot, chain: ChainReader) -> None:
    registry = snap.contract("agent_registry")
    if not registry:
        snap.failures["registry"] = "the live API names no agent_registry contract"
        return
    try:
        ids = chain.simulate(registry, "list_ids")
        if not isinstance(ids, list):
            raise TypeError(f"list_ids answered {ids!r}")
        for agent_id in ids:
            record = chain.simulate(registry, "get", [scval.to_symbol(str(agent_id))])
            if not isinstance(record, dict) or not _is_account(record.get("owner")):
                raise TypeError(f"get({agent_id}) answered {record!r}")
            registered = record.get("registered_at")
            snap.agents.append(
                AgentRecord(
                    id=str(agent_id),
                    owner=str(record["owner"]),
                    name=str(record.get("name") or agent_id),
                    active=bool(record.get("active")),
                    registered_at=registered if isinstance(registered, int) else None,
                )
            )
    except FAILURES as exc:
        snap.agents.clear()
        snap.fail("registry", exc)


# ── the escrows ──────────────────────────────────────────────────────────
def escrow_ids(snap: Snapshot, extra: tuple[str, ...]) -> list[str]:
    live = snap.contract("payment_escrow")
    ordered = ([live] if live else []) + list(KNOWN_ESCROWS) + list(extra)
    return list(dict.fromkeys(ordered))


def read_escrow(chain: ChainReader, contract: str, live: bool) -> EscrowScan:
    """Every id the escrow's `Nonce` has issued, each read as a receipt or else an authorization.

    Both versions number authorizations and receipts from one counter, so ids
    0..Nonce-1 are the escrow's complete history, readable however long ago
    it happened. A v2 `settle` writes one receipt per payout; a v1 `charge`
    writes one receipt per charge.
    """
    version = chain.escrow_version(contract)
    storage = chain.instance_storage(contract)
    nonce = _int(storage.get("Nonce", 0))
    settler, admin = storage.get("Settler"), storage.get("Admin")
    scan = EscrowScan(
        contract=contract,
        version=version,
        live=live,
        settler=settler if _is_account(settler) else None,
        admin=admin if _is_account(admin) else None,
        nonce=nonce,
    )
    raw_receipts: list[tuple[str, dict[str, Any]]] = []
    for n in range(nonce):
        hex_id = id_hex(n)
        try:
            value = chain.simulate(contract, "receipt", [id_bytes(n)])
            raw_receipts.append((hex_id, value))
            continue
        except SimulationError:
            pass
        try:
            auth = chain.simulate(contract, "authorization", [id_bytes(n)])
        except SimulationError:
            scan.unreadable_ids.append(hex_id)
            continue
        scan.auths[hex_id] = AuthRecord(
            id_hex=hex_id,
            payer=str(auth["payer"]),
            agent_id=str(auth["agent_id"]),
            max_amount=_int(auth["max_amount"]),
            spent=_int(auth["spent"]),
        )
    for hex_id, value in raw_receipts:
        scan.receipts.append(
            ReceiptRecord(
                escrow=contract,
                version=version,
                id_hex=hex_id,
                auth_id=str(value["auth_id"]),
                agent_id=str(value["agent_id"]),
                amount=_int(value["amount"]),
                job_id=str(value["job_id"]),
                settled_at=_int(value["settled_at"]),
            )
        )
    return scan


def read_escrows(snap: Snapshot, chain: ChainReader, extra: tuple[str, ...]) -> None:
    live = snap.contract("payment_escrow")
    for contract in escrow_ids(snap, extra):
        try:
            scan = read_escrow(chain, contract, contract == live)
        except FAILURES as exc:
            snap.fail("escrow", exc)
            continue
        snap.escrows.append(scan)
        if scan.settler:
            snap.platform.setdefault(scan.settler, f"v{scan.version} escrow settler")
        if scan.admin:
            snap.platform.setdefault(scan.admin, f"v{scan.version} escrow admin")
        if scan.unreadable_ids:
            snap.failures.setdefault(
                "escrow",
                f"{len(scan.unreadable_ids)} id(s) on {contract} read as neither a receipt nor an authorization",
            )


# ── histories: charges, dispute ratings, refunds ─────────────────────────
def _match_charges(snap: Snapshot, op: Operation) -> None:
    escrow = next((e for e in snap.escrows if e.contract == op.contract_id), None)
    if escrow is None or op.function not in ("charge", "settle") or len(op.args) < 3:
        return
    if op.function == "charge":  # v1: (caller, auth_id, amount, job_id)
        auth_id, amount, job_id = op.args[1], op.args[2], op.args[3] if len(op.args) > 3 else None
        matches = [
            r
            for r in escrow.receipts
            if r.auth_id == auth_id
            and r.job_id == job_id
            and r.amount == amount
            and (r.escrow, r.id_hex) not in snap.charge_txs
        ][:1]
    else:  # v2: (caller, auth_id, job_id, payouts)
        auth_id, job_id = op.args[1], op.args[2]
        matches = [r for r in escrow.receipts if r.auth_id == auth_id and r.job_id == job_id]
    for r in matches:
        snap.charge_txs.setdefault((r.escrow, r.id_hex), TxRef(op.tx_hash, op.date))


def history(snap: Snapshot, chain: ChainReader, account: str) -> list[Operation]:
    if account not in snap.histories:
        snap.histories[account] = chain.operations(account)
    return snap.histories[account]


def read_histories(snap: Snapshot, chain: ChainReader) -> None:
    """Horizon's history of every platform key: who charged, who rated a dispute, who paid a refund.

    Every settlement, rating and refund is signed by a platform key (the
    settler, the scorer, the signing key), so their histories hold every such
    transaction. One operation seen from two accounts is counted once.
    """
    ledger = snap.contract("reputation_ledger")
    sac = snap.asset_sac
    seen: set[str] = set()
    for account in sorted(snap.platform):
        try:
            operations = history(snap, chain, account)
        except FAILURES as exc:
            snap.fail("history", exc)
            continue
        for op in operations:
            if op.id in seen:
                continue
            seen.add(op.id)
            if op.contract_id is None:
                continue
            _match_charges(snap, op)
            if op.contract_id == ledger and op.function == "submit" and len(op.args) >= 7:
                kind = str(op.args[6])
                snap.rating_kinds[kind] = snap.rating_kinds.get(kind, 0) + 1
            if op.contract_id == ledger and op.function == "submit" and len(op.args) >= 7 and op.args[6] == "dispute":
                # submit(caller, agent_id, job_id, rating, weight, payer, kind)
                snap.dispute_ratings.append(
                    DisputeRating(
                        agent_id=str(op.args[1]),
                        job_id=str(op.args[2]),
                        payer=op.args[5] if _is_account(op.args[5]) else None,
                        rating=op.args[3] if isinstance(op.args[3], int) else None,
                        tx_hash=op.tx_hash,
                        created_at=op.created_at,
                    )
                )
            if op.contract_id == sac and op.function == "transfer":
                for change in op.balance_changes:
                    if change.kind == "transfer" and change.source in snap.platform and change.destination:
                        snap.transfers.append(
                            Transfer(
                                source=change.source,
                                destination=change.destination,
                                amount_stroops=change.amount_stroops,
                                tx_hash=op.tx_hash,
                                created_at=op.created_at,
                                muxed_id=change.destination_muxed_id,
                            )
                        )
    snap.dispute_ratings.sort(key=lambda d: (d.created_at, d.tx_hash))
    snap.transfers.sort(key=lambda t: (t.created_at, t.tx_hash))


def read_ledger(snap: Snapshot, chain: ChainReader) -> None:
    """The lifetime dispute count of every agent, from `rep_state` — state, not events."""
    ledger = snap.contract("reputation_ledger")
    if not ledger:
        snap.failures["ledger"] = "the live API names no reputation_ledger contract"
        return
    agent_ids = sorted({a.id for a in snap.agents} | {d.agent_id for d in snap.dispute_ratings})
    try:
        for agent_id in agent_ids:
            state = chain.simulate(ledger, "rep_state", [scval.to_symbol(agent_id)])
            snap.ledger_disputed[agent_id] = _int((state or {}).get("disputed", 0))
    except FAILURES as exc:
        snap.fail("ledger", exc)


def read_registrations(snap: Snapshot, chain: ChainReader) -> None:
    """Each agent's registration transaction, from its owner's history. Links only: a miss is a warning."""
    registry = snap.contract("agent_registry")
    wanted = {a.id for a in snap.agents}
    for owner in sorted({a.owner for a in snap.agents}):
        try:
            operations = history(snap, chain, owner)
        except FAILURES as exc:
            snap.warnings.append(f"registration history of an owner could not be read: {type(exc).__name__}: {exc}")
            continue
        for op in operations:
            # register(owner, id, name, skills, price)
            if op.contract_id == registry and op.function == "register" and len(op.args) >= 2:
                agent_id = op.args[1]
                if agent_id in wanted and agent_id not in snap.registrations:
                    snap.registrations[agent_id] = Registration(agent_id, op.tx_hash, op.date, op.source_account)
    missing = sorted(wanted - set(snap.registrations))
    if missing:
        snap.warnings.append(f"no registration transaction found for: {', '.join(missing)}")


# ── the deployment ───────────────────────────────────────────────────────
def _read(snap: Snapshot, source: str, call: Callable[[], Answer]) -> Answer | None:
    try:
        return call()
    except FAILURES as exc:
        snap.fail(source, exc)
        return None


def read_bound(snap: Snapshot, reads: Reads) -> None:
    """Each agent's `bound` flag, exactly as the live adoption report states it. A miss is a warning.

    Whether an agent is bound is the backend's word (an off-chain binding),
    not a chain fact, and it says nothing about whether the endpoint works.
    """
    snap.urls["adoption"] = reads.api_url("/ecosystem/adoption")
    try:
        answer = reads.adoption()
    except FAILURES as exc:
        snap.warnings.append(f"the adoption report could not be read, so no bound count is stated: {exc}")
        return
    operators = answer.obj().get("operators")
    if not answer.ok or not isinstance(operators, list):
        snap.warnings.append(
            f"the adoption report answered HTTP {answer.status} without its operators, so no bound count is stated"
        )
        return
    bound: dict[str, bool | None] = {}
    for operator in operators:
        agents = operator.get("agents") if isinstance(operator, dict) else None
        for agent in agents if isinstance(agents, list) else []:
            if isinstance(agent, dict) and isinstance(agent.get("agent_id"), str):
                flag = agent.get("bound")
                bound[agent["agent_id"]] = flag if isinstance(flag, bool) else None
    snap.bound = bound


def read_deployment(snap: Snapshot, reads: Reads) -> None:
    snap.urls.update(
        params=reads.api_url("/stellar/reputation/params"),
        openapi=f"{reads.backend}/openapi.json",
        readiness=f"{reads.backend}/readiness",
    )
    params = _read(snap, "params", reads.reputation_params)
    if params is not None:
        if params.ok and isinstance(params.body, dict):
            snap.params = params.body
        else:
            snap.failures["params"] = f"{params.url} answered HTTP {params.status}"
    spec = _read(snap, "openapi", reads.openapi)
    if spec is not None:
        paths = spec.obj().get("paths")
        if spec.ok and isinstance(paths, dict):
            snap.openapi_routes = {
                f"{method.upper()} {path}" for path, ops in paths.items() if isinstance(ops, dict) for method in ops
            }
        else:
            snap.failures["openapi"] = f"{spec.url} answered HTTP {spec.status}"
    for path in (REGISTER_PAGE, GUIDE_PAGE, DEMO_PAGE):
        snap.page_urls[path] = reads.page_url(path)
        page = _read(snap, f"page:{path}", partial(reads.page, path))
        if page is not None:
            snap.pages[path] = page
    for full_name, _role in REPOSITORIES:
        repo = _read(snap, "github", partial(reads.repository, full_name))
        if repo is None:
            continue
        if repo.status not in (200, 404):
            snap.fail("github", RuntimeError(f"{repo.url} answered HTTP {repo.status}"))
            continue
        snap.repos[full_name] = repo


def collect(
    network: dict[str, Any],
    team: TeamRegister,
    reads: Reads,
    chain: ChainReader,
    *,
    extra_escrows: tuple[str, ...] = (),
) -> Snapshot:
    """Every read, in dependency order: keys before histories, receipts before the charges that made them."""
    snap = Snapshot(network=network, team=dict(team.roles))
    readiness = _read(snap, "readiness", reads.readiness)
    if readiness is not None:
        # /readiness answers 503 WITH its report when a dependency is missing; a
        # 503 without one (a proxy's error page) names no ratings signer, and
        # any owner could be that signer, so it is a failed read.
        body = readiness.body if isinstance(readiness.body, dict) else {}
        if readiness.status in (200, 503) and isinstance(body.get("ratings"), dict):
            snap.readiness = body
        else:
            snap.failures["readiness"] = (
                f"{readiness.url} answered HTTP {readiness.status} without a readiness report naming the ratings keys"
            )
    read_platform_keys(snap, chain)
    read_registry(snap, chain)
    read_escrows(snap, chain, extra_escrows)
    read_histories(snap, chain)
    read_ledger(snap, chain)
    read_registrations(snap, chain)
    read_deployment(snap, reads)
    read_bound(snap, reads)
    return snap

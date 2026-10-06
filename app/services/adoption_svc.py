"""
Adoption evidence — SOW §6.3's three numbers, computed from on-chain facts.

§6.3 asks for at least two externally operated agents, at least two unique
operator wallets, and at least three workflows routed to external agents and
settled on testnet, each with a charge transaction a reviewer can open. The
story's rule is the whole design: "External means external. Wallets controlled
by the Blocksmiths do not count toward the metric under any framing." So this
module answers the question the way a sceptical reviewer would, and shows its
working so they never have to trust us:

  - the agent list is the on-chain AgentRegistry (the registry-synced mirror in
    `state.agents`, cross-checked against `list_ids`), never our seeded catalog;
  - an agent is external only when its on-chain owner is in NEITHER the
    committed team register (`app/data/team_wallets.json`) NOR the set of keys
    this deployment demonstrably holds at runtime, and every agent that fails
    that test is listed under `excluded` with the reason;
  - a settled workflow is a `charged` event `settlement_svc` already counts as
    verified revenue, carrying its transaction hash and a Stellar Expert link,
    found in a scan of the RPC's event retention (about seven days), whose
    measured span the report carries as `window_days`;
  - a read that could not be made is reported as unreadable and never as zero.

The team register
-----------------
A public, committed declaration of every account the team controls, with where
each one is documented. It is loaded and validated at import, so a malformed
register — an address that is not a G-strkey, a duplicate, a missing role or
evidence — stops the service booting with a message naming the entry, rather
than quietly letting one of our own wallets read as an outside operator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel
from stellar_sdk import StrKey

from ..config import settings
from ..schemas import Agent
from ..state import state
from ..stellar import cache as rcache
from ..stellar import client as sc
from . import charge_window, external_binding, registry_sync, settlement_svc, snapshot_store, snapshots
from .binding_store import get_binding_store
from .dispatch_signing import dispatch_signer_address
from .snapshots import KeepWarm, Snapshot, SnapshotCell

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_R = TypeVar("_R")

# Beside the code that reads it, so it deploys with the service (render.yaml
# runs from the source checkout) and a reviewer finds it next to the rule.
REGISTER_PATH = Path(__file__).resolve().parent.parent / "data" / "team_wallets.json"

_ENTRY_FIELDS = frozenset({"address", "role", "evidence"})


class TeamRegisterError(ValueError):
    """The team register is unusable; the message names the offending entry."""


@dataclass(frozen=True)
class TeamWallet:
    """One declared team account: what it is for, and where that is documented."""

    address: str
    role: str
    evidence: str


def load_team_register(path: Path = REGISTER_PATH) -> tuple[TeamWallet, ...]:
    """Read and validate the register. Raises TeamRegisterError naming the entry.

    Strict on purpose. Every mistake this refuses has the same consequence if
    it is let through: a team wallet silently stops being recognised, and our
    own agent is counted as an outside operator's. An empty register is refused
    for the same reason — it would make every owner "external".
    """
    name = path.name
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TeamRegisterError(f"{name}: unreadable: {e}") from e
    if not isinstance(raw, dict) or not isinstance(raw.get("wallets"), list):
        raise TeamRegisterError(f"{name}: expected an object with a `wallets` list")
    if not raw["wallets"]:
        raise TeamRegisterError(f"{name}: `wallets` is empty, which would make every owner external")

    first_seen: dict[str, int] = {}
    wallets: list[TeamWallet] = []
    for index, entry in enumerate(raw["wallets"]):
        label = f"{name} entry {index}"
        if not isinstance(entry, dict):
            raise TeamRegisterError(f"{label}: expected an object, got {type(entry).__name__}")
        if set(entry) != _ENTRY_FIELDS:
            raise TeamRegisterError(f"{label}: expected exactly address, role and evidence, got {sorted(entry)}")
        address = entry["address"]
        if not isinstance(address, str) or not StrKey.is_valid_ed25519_public_key(address):
            raise TeamRegisterError(f"{label}: {address!r} is not a valid Stellar account id (G-strkey)")
        label = f"{label} ({address})"
        for key in ("role", "evidence"):
            value = entry[key]
            if not isinstance(value, str) or not value.strip():
                raise TeamRegisterError(f"{label}: `{key}` must be a non-empty string")
        if address in first_seen:
            raise TeamRegisterError(f"{label}: duplicates entry {first_seen[address]}")
        first_seen[address] = index
        wallets.append(TeamWallet(address=address, role=entry["role"].strip(), evidence=entry["evidence"].strip()))
    return tuple(wallets)


# Loaded at import: the router imports this module and `app.main` imports the
# router, so a bad register refuses boot instead of serving a wrong count.
TEAM_REGISTER: tuple[TeamWallet, ...] = load_team_register()


# ── the targets ────────────────────────────────────────────────────────────
# SOW §6.3, verbatim: ≥ 2 externally operated agents, ≥ 2 unique operator
# wallets, ≥ 3 workflows routed to external agents and settled on testnet.
TARGET_EXTERNAL_AGENTS = 2
TARGET_UNIQUE_OPERATOR_WALLETS = 2
TARGET_SETTLED_EXTERNAL_WORKFLOWS = 3

# The report is a snapshot built in the background (D-091), never on the
# request: a reviewer's request is answered from memory with the last report
# and its age, and the first request after a boot with nothing to serve gets a
# 202 that says it is being computed. See `report_cell` below.
#
# A build's cost does not grow with the number of agents. Settlements come from
# ONE scan of the escrow's event window for every agent (`charge_window`), kept
# between builds so each build reads only the ledgers closed since; owners come
# from the registry mirror; binding status is one read of the bound set. It
# used to be a settlement scan per external agent — ~15 RPC calls each — and
# at 1,000 agents the build overran its budget and the endpoint went 503.
#
# Rebuilt this often while the process is up. Settlements only show up here
# through a scan, so this is how long a new one can take to appear.
REPORT_REFRESH_SECONDS = 300.0
# ...and when the on-chain mirror's agents or owners change, but not more often
# than this.
REPORT_REGISTRY_REBUILD_SECONDS = 120.0
# The time a build gives itself to read. Past it, every read still outstanding
# is left for the next build, and what was read is published as a PARTIAL
# report (`complete: false`): each number is then a floor, never inflated.
REPORT_WORK_BUDGET_SECONDS = 300.0
# The backstop: a build still running past this is abandoned, the previous
# report kept. Well above the work budget, which is what normally ends a build.
REPORT_BUILD_BUDGET_SECONDS = 900.0
# After a build that failed or overran, the next attempt waits this long.
REPORT_RETRY_AFTER_FAILURE_SECONDS = 120.0
# The first build waits for the registry mirror's first full pass — a partial
# mirror would send every unmirrored id to its own `owner_of` read — but not
# longer than this after boot.
REPORT_BOOT_GRACE_SECONDS = 600.0
# What a 202 asks the client to wait before asking again.
REPORT_PENDING_RETRY_AFTER_SECONDS = 30
# A report restored from the database after a restart is served whatever its
# age, marked `persisted` and dated by `generated_at` and `X-Snapshot-Age`,
# until this process's first build replaces it: once any report has been
# built, an old one with its age on it beats "computing" or a 503.
REPORT_RESTORE_MAX_AGE_SECONDS: float | None = None
# A PARTIAL build does not replace a COMPLETE report younger than this: the
# complete one is kept in service and the next build resumes where the
# partial stopped (`charge_window` keeps its progress). An older complete
# report is replaced, because by then the registry it counted has moved on.
REPORT_PARTIAL_KEEPS_COMPLETE_SECONDS = 3_600.0

# How many `owner_of` probes run at once. Each pins a thread of the bounded
# pool app/main.py hands to asyncio.to_thread, so this stays well under that
# pool rather than starving the money path that shares it.
READ_CONCURRENCY = 4

# Registry ids the mirror does not hold are read in one batch from registry
# storage; an id that batch cannot answer is probed with `owner_of`, at most
# this many per build. The rest wait for the next build, and are named in
# `unreadable_agents` until then.
MAX_OWNER_PROBES_PER_BUILD = 64

# Ceiling on each of the smaller probes: one `owner_of` read, one read of the
# binding store's bound set. Each has its own transport timeouts; this is the
# bound the report relies on, so one stalled probe costs its answer, not the
# build.
PROBE_TIMEOUT_SECONDS = 20.0

# Contract `admin()` views. v2's settler and admin can be rotated, so these
# are not cached for as long as settlement_svc caches values that never move.
PLATFORM_READ_TTL_SECONDS = 300.0

# The seeded catalog's namespace. Those agents run inside this process and are
# never an operator's, whatever the registry holds under the same prefix.
SEEDED_PREFIX = "agt_"

# Amounts on the escrow are 7-decimal integers (stroops).
_STROOPS_PER_UNIT = 10_000_000

ExclusionReason = Literal["team_wallet", "platform_key"]


# ── the response (frozen: the frontend codes against it) ──────────────────
class AdoptionCounts(BaseModel):
    external_agents: int
    unique_operator_wallets: int
    settled_external_workflows: int


class AdoptionMet(BaseModel):
    external_agents: bool
    unique_operator_wallets: bool
    settled_external_workflows: bool


class SettledWorkflow(BaseModel):
    """One charge an external agent was paid, as a reviewer verifies it."""

    job_id_hex: str
    tx_hash: str
    explorer: str
    amount_usdc: float
    payer: str
    # The payer's role when it is one of ours — a team buyer paying an outside
    # operator still counts under §6.3, but a reviewer has to be able to see
    # it. None when the payer is not a team wallet.
    payer_team_role: str | None
    settled_at: int | None  # ledger close time, unix seconds; None if the node omitted it


class AdoptionAgent(BaseModel):
    agent_id: str
    name: str  # the on-chain name, as the registry mirror sanitised it
    active: bool
    # Whether an endpoint is bound — never the endpoint itself. None when the
    # binding store could not be read.
    bound: bool | None
    settled_workflows: list[SettledWorkflow]


class AdoptionOperator(BaseModel):
    owner: str
    owner_explorer: str
    agents: list[AdoptionAgent]


class ExcludedOwner(BaseModel):
    owner: str
    owner_explorer: str
    reason: ExclusionReason
    role: str
    agent_ids: list[str]


class AdoptionCoverage(BaseModel):
    """How much of what the report rests on this build actually read."""

    # On-chain ids `list_ids` names outside the seeded namespace; None when
    # the listing could not be read.
    agents_listed: int | None
    # Agents with an owner verdict: counted as external, or excluded as ours.
    agents_accounted: int
    # The settlement scan's reach: ledgers read, of the ledgers the RPC node
    # holds (0 when the node could not be asked).
    settlement_ledgers_scanned: int
    settlement_ledgers_in_window: int
    # Charges to external agents found in the ledgers read, and how many of
    # them have no payer read yet — those are never counted.
    external_charges: int
    external_charges_unattributed: int


class AdoptionReport(BaseModel):
    """SOW §6.3's answer, with the limits of what was looked at.

    `window_days` is how far back `settled_external_workflows` can see. The
    settled workflows come from `settlement_svc`'s scan of Soroban RPC events,
    which the node keeps for about seven days, so an older settlement is not
    counted. It is the scan's MEASURED span (`SettlementEvidence.window_days`),
    not a constant: when the per-agent scans cover different spans, it is the
    smallest, so "settled in the last N days" holds for every agent. Only scans
    that ran count toward it; an agent whose scan did not run is already named
    in `unreadable_agents`. 0 when no scan ran, e.g. no external agents.
    """

    network: str
    generated_at: int
    window_days: float
    targets: AdoptionCounts
    totals: AdoptionCounts
    met: AdoptionMet
    operators: list[AdoptionOperator]
    excluded: list[ExcludedOwner]
    # True when any number above may be LOWER than the truth: a read failed, a
    # scan stopped early, or an on-chain agent could not be accounted for.
    degraded: bool
    unreadable_agents: list[str]
    # False when the build ran out of time and published what it had read: a
    # PARTIAL report, its numbers floors, `degraded` true, the rest resumed by
    # the next build. A report from before this field existed was complete.
    complete: bool = True
    # What was read; None on a report from before this field existed.
    coverage: AdoptionCoverage | None = None


TARGETS = AdoptionCounts(
    external_agents=TARGET_EXTERNAL_AGENTS,
    unique_operator_wallets=TARGET_UNIQUE_OPERATOR_WALLETS,
    settled_external_workflows=TARGET_SETTLED_EXTERNAL_WORKFLOWS,
)


def _describe(e: BaseException) -> str:
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _network() -> str:
    """Keyed on the passphrase, like every other network decision here (D-074)."""
    return "mainnet" if settings.is_mainnet() else "testnet"


def _account_link(address: str) -> str:
    return f"https://stellar.expert/explorer/{sc.explorer_network()}/account/{address}"


def _tx_link(tx_hash: str) -> str:
    return f"https://stellar.expert/explorer/{sc.explorer_network()}/tx/{tx_hash}"


async def _bounded(items: Iterable[_T], fn: Callable[[_T], Awaitable[_R]], limit: int = READ_CONCURRENCY) -> list[_R]:
    """`fn` over `items`, at most `limit` at a time, results in input order."""
    gate = asyncio.Semaphore(limit)

    async def _one(item: _T) -> _R:
        async with gate:
            return await fn(item)

    return list(await asyncio.gather(*(_one(item) for item in items)))


# ── the platform's own keys, read at runtime ──────────────────────────────
@dataclass
class _PlatformKeys:
    """Accounts this deployment demonstrably holds, besides the register."""

    roles: dict[str, str] = field(default_factory=dict)
    # Reads that failed. Any of them could have named an owner that is ours, so
    # a non-empty list makes the whole answer `degraded`.
    unreadable: list[str] = field(default_factory=list)

    def add(self, address: str | None, role: str) -> None:
        if address:
            self.roles.setdefault(address, role)


async def _read_view(prefix: str, contract_id: str, function_name: str) -> str | None:
    """A zero-argument address view (`admin`), or None when it cannot be read."""

    async def _fetch() -> str | None:
        value = await asyncio.to_thread(sc.simulate_read, contract_id, function_name, [])
        return value if isinstance(value, str) and value else None

    try:
        result = await rcache.get_or_set(f"{prefix}:{contract_id}", PLATFORM_READ_TTL_SECONDS, _fetch)
    except Exception as e:
        logger.info("[adoption] %s.%s() unreadable: %s", contract_id, function_name, _describe(e))
        return None
    return result if isinstance(result, str) else None


async def _platform_keys() -> _PlatformKeys:
    """The keys this deployment holds or names, each with the role it plays.

    Configuration first — the signing key's public half, the dispatch signer,
    the configured admin — then the chain's own answer for the roles that can
    move: the escrow's `settler()` and `admin()`, and the registry's `admin()`.

    The escrow's `admin()` is read only on v2, because v1 has no such view
    and asking would log an RPC error on every report. It is best effort even
    there: the admin it names is the deployment admin already added above. The
    settler and the registry admin are not best effort: a failed read of
    either leaves an owner we cannot rule out, and says so.
    """
    keys = _PlatformKeys()
    keys.add(settings.stellar_admin_address, "deployment admin")
    if settings.stellar_signing_key:
        try:
            keys.add(sc.signer_public_key(), "deployment signing key")
        except Exception as e:
            logger.warning("[adoption] signing key unreadable: %s", _describe(e))
            keys.unreadable.append("signing key")
    keys.add(dispatch_signer_address(), "dispatch signer")

    escrow_id = settings.stellar_payment_escrow
    registry_id = settings.stellar_agent_registry
    settler, escrow_admin, registry_admin = await asyncio.gather(
        settlement_svc._read_settler(escrow_id) if escrow_id else _none(),
        _escrow_admin(escrow_id) if escrow_id else _none(),
        _read_view("registryadmin", registry_id, "admin") if registry_id else _none(),
    )
    if escrow_id:
        keys.add(settler, "escrow settler")
        keys.add(escrow_admin, "escrow admin")
        if settler is None:
            keys.unreadable.append("escrow settler()")
    if registry_id:
        keys.add(registry_admin, "registry admin")
        if registry_admin is None:
            keys.unreadable.append("registry admin()")
    return keys


async def _none() -> None:
    return None


async def _escrow_admin(escrow_id: str) -> str | None:
    """v2's `admin()`, or None on v1. An unreadable version still tries the view."""
    version = sc.cached_escrow_version(escrow_id)
    if version is None:
        try:
            version = await asyncio.to_thread(sc.escrow_version, escrow_id)
        except Exception as e:
            logger.info("[adoption] escrow version unreadable: %s", _describe(e))
    if version == 1:
        return None
    return await _read_view("escrowadmin", escrow_id, "admin")


# ── the rule: who counts as an outside operator ───────────────────────────
@dataclass(frozen=True)
class OwnerRule:
    """The one definition of "external", shared by every number that uses it.

    An owner is external only when it is in NEITHER the committed team register
    NOR the keys this deployment holds at runtime. `GET /api/metrics/overview`
    counts external agents with this same object, so the dashboard and the
    adoption report cannot disagree about who is an outside operator.
    """

    register: Mapping[str, TeamWallet]
    platform: _PlatformKeys

    def classify(self, owner: str) -> tuple[ExclusionReason, str] | None:
        """Why `owner` is ours, or None when it is an outside operator."""
        if owner in self.register:
            return "team_wallet", self.register[owner].role
        if owner in self.platform.roles:
            return "platform_key", self.platform.roles[owner]
        return None

    @property
    def team_roles(self) -> dict[str, str]:
        """Every account that is ours, with its role; the register wins a tie."""
        return {**self.platform.roles, **{a: w.role for a, w in self.register.items()}}

    @property
    def unreadable(self) -> list[str]:
        """Platform-key reads that failed. Any of them could have named an owner
        that is ours, so a non-empty list means "external" may be too high."""
        return self.platform.unreadable


async def owner_rule() -> OwnerRule:
    """The register and this deployment's runtime keys, read now. Never raises
    for a failed read: those land in `OwnerRule.unreadable`."""
    return OwnerRule(register={w.address: w for w in TEAM_REGISTER}, platform=await _platform_keys())


def onchain_mirror() -> dict[str, Agent]:
    """The registry-synced on-chain agents, by id — never the seeded catalog,
    whatever the registry holds under the seeded prefix."""
    return {a.id: a for a in state.list_agents() if a.source == "onchain" and not a.id.startswith(SEEDED_PREFIX)}


# ── the agent list ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Unmirrored:
    """On-chain ids the registry lists and the mirror does not hold."""

    ids: list[str]
    listed: bool  # False when `list_ids` itself could not be read
    total: int = 0  # every id it listed outside the seeded namespace


async def _unmirrored(mirrored: set[str]) -> _Unmirrored:
    """Cross-check the mirror against the registry's own `list_ids`.

    `state.agents` is the agent list, but it has blind spots nothing else
    reports: a process whose first sync pass has not landed, a pass that is
    failing, a record the mirror refused (an unbelievable price) or could not
    read. An agent in any of them would otherwise just be missing, and a
    missing agent reads as zero.
    """
    registry_id = settings.stellar_agent_registry
    if not registry_id:
        return _Unmirrored(ids=[], listed=False)
    try:
        ids = await asyncio.to_thread(sc.simulate_read, registry_id, "list_ids", [])
    except Exception as e:
        logger.warning("[adoption] registry list_ids unreadable: %s", _describe(e))
        return _Unmirrored(ids=[], listed=False)
    if not isinstance(ids, list):
        logger.warning("[adoption] registry list_ids answered %s, not a list", type(ids).__name__)
        return _Unmirrored(ids=[], listed=False)
    listed = {i for i in ids if isinstance(i, str) and not i.startswith(SEEDED_PREFIX)}
    return _Unmirrored(ids=sorted(listed - mirrored), listed=True, total=len(listed))


# Owners read for ids the mirror lacked, by (registry, id). `Agent.owner` is
# set by `register` and no function ever writes it again, so an owner once
# read is the owner for good: it is never read twice.
_owners_read: dict[tuple[str, str], str] = {}


def forget_owners() -> None:
    """Drop the owners read so far (tests)."""
    _owners_read.clear()


async def _owner_or_none(agent_id: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> str | None:
    """The live `owner_of`, or None when it could not be established."""
    try:
        return await asyncio.wait_for(external_binding.resolve_owner(agent_id), timeout=timeout)
    except (external_binding.OwnerLookupError, TimeoutError) as e:
        logger.warning("[adoption] owner of %s unreadable: %s", agent_id, _describe(e))
        return None


async def _owners_of(agent_ids: list[str], deadline: float) -> tuple[dict[str, str], bool]:
    """The owners of `agent_ids` that could be read by `deadline`, and whether
    any id was left unread for the next build (out of time, or past
    MAX_OWNER_PROBES_PER_BUILD).

    Owners already read are reused; the rest are read in one batch from the
    registry's storage, and only what that batch could not answer is probed
    with `owner_of`, a bounded number per build. An id with no owner here is
    never counted — the caller names it unreadable.
    """
    registry_id = settings.stellar_agent_registry
    owners = {i: _owners_read[(registry_id, i)] for i in agent_ids if (registry_id, i) in _owners_read}
    rest = [i for i in agent_ids if i not in owners]
    if rest and time.monotonic() < deadline:
        try:
            records = await asyncio.wait_for(
                asyncio.to_thread(sc.read_agent_records, registry_id, rest), timeout=deadline - time.monotonic()
            )
        except Exception as e:
            logger.warning("[adoption] registry records unreadable: %s", _describe(e))
            records = {}
        for agent_id, record in records.items():
            owner = record.get("owner")
            if isinstance(owner, str) and owner:
                owners[agent_id] = owner
        rest = [i for i in rest if i not in owners]
    probe = rest[:MAX_OWNER_PROBES_PER_BUILD]
    left = len(rest) > len(probe)

    async def _probe(agent_id: str) -> str | None:
        nonlocal left
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            left = True
            return None
        return await _owner_or_none(agent_id, min(remaining, PROBE_TIMEOUT_SECONDS))

    for agent_id, owner in zip(probe, await _bounded(probe, _probe), strict=True):
        if owner is not None:
            owners[agent_id] = owner
    for agent_id, owner in owners.items():
        _owners_read[(registry_id, agent_id)] = owner
    return owners, left


# ── one external agent ────────────────────────────────────────────────────
async def _bound_ids() -> frozenset[str] | None:
    """Every agent with a live binding, the way GET /agents/{id}/binding
    decides it — one read of the store, never one per agent. None when the
    store could not be read.

    Only the ids are read: a binding record carries the endpoint URL, which
    this public route never exposes in any form.
    """
    try:
        return await asyncio.wait_for(get_binding_store().list_agent_ids(), timeout=PROBE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.warning("[adoption] binding store unreadable: %s", _describe(e))
        return None


async def _window(owners: dict[str, str], deadline: float) -> charge_window.WindowSettlements | None:
    """Every external agent's settlement evidence, from one window scan, or
    None when the read did not come back at all."""
    if not owners:
        return None
    try:
        return await charge_window.fetch_settlements(owners, deadline=deadline)
    except Exception as e:
        logger.warning("[adoption] settlement window unreadable: %s", _describe(e))
        return None


def _unix(at: str | None) -> int | None:
    if not at:
        return None
    try:
        return int(datetime.fromisoformat(at).timestamp())
    except ValueError:
        return None


# Exclusions that are not a finding about the charge but a check that could
# not run: the charge may be revenue, so the count may be low.
_UNCHECKED: frozenset[str] = frozenset({"payer_unreadable", "settler_unreadable"})


@dataclass(frozen=True)
class _AgentResult:
    agent: AdoptionAgent
    complete: bool  # False: the settled list may be missing charges
    window_days: float | None  # the scan's measured span; None when no scan ran


def _external_agent(
    agent: Agent,
    evidence: settlement_svc.SettlementEvidence | None,
    bound_ids: frozenset[str] | None,
    team_roles: dict[str, str],
) -> _AgentResult:
    """One external agent: its binding, and the charges that verifiably paid it.

    A charge counts only when `settlement_svc`'s rule counts it as verified
    revenue — its payer was read, and is neither this agent's owner nor the
    platform — AND it names a transaction, because §6.3 asks for a charge a
    reviewer can open. Anything short of that leaves the agent
    `complete=False`: a scan that did not run or did not reach the tip, a
    charge whose payer (or the settler it is compared with) is not read yet,
    or a verified charge with no usable hash. Its verified charges still
    count; the flag is what stops the total from being read as the whole truth.
    """
    workflows: list[SettledWorkflow] = []
    complete = evidence is not None and evidence.unavailable is None and not evidence.truncated
    if evidence is not None and evidence.unavailable is not None:
        logger.info("[adoption] settlement for %s unavailable: %s", agent.id, evidence.unavailable)
    for entry in evidence.entries if evidence is not None else []:
        if entry.exclusion in _UNCHECKED:
            complete = False
            continue
        if entry.self_payment:
            continue
        if entry.tx_hash is None:
            complete = False
            logger.warning("[adoption] verified charge on %s job %s has no usable tx hash", agent.id, entry.job_id)
            continue
        workflows.append(
            SettledWorkflow(
                job_id_hex=entry.job_id,
                tx_hash=entry.tx_hash,
                explorer=_tx_link(entry.tx_hash),
                amount_usdc=round(entry.amount_stroops / _STROOPS_PER_UNIT, 7),
                payer=entry.payer,
                payer_team_role=team_roles.get(entry.payer),
                settled_at=_unix(entry.at),
            )
        )
    workflows.sort(key=lambda w: (w.settled_at or 0, w.job_id_hex, w.tx_hash))
    return _AgentResult(
        agent=AdoptionAgent(
            agent_id=agent.id,
            name=agent.name,
            active=agent.status != "offline",
            bound=None if bound_ids is None else agent.id in bound_ids,
            settled_workflows=workflows,
        ),
        complete=complete,
        window_days=evidence.window_days if evidence is not None and evidence.unavailable is None else None,
    )


# ── the report ────────────────────────────────────────────────────────────
async def build_report() -> AdoptionReport:
    """Compute the report. Never raises for a failed read, and stops reading
    at REPORT_WORK_BUDGET_SECONDS.

    Every failure lands in `degraded` and, where it concerns one agent, in
    `unreadable_agents` — the numbers are then a floor, never a zero standing
    in for a lookup that did not happen. A read left for the next build
    because time ran out also makes the report `complete: false`.
    """
    started = time.monotonic()
    deadline = started + REPORT_WORK_BUDGET_SECONDS
    rule = await owner_rule()
    classify = rule.classify
    team_roles = rule.team_roles
    degraded = bool(rule.unreadable)
    unreadable: set[str] = set()

    mirrored = onchain_mirror()
    owners: dict[str, str] = {}
    for agent in mirrored.values():
        if agent.owner:
            owners[agent.id] = agent.owner
        else:
            unreadable.add(agent.id)

    gap = await _unmirrored(set(mirrored))
    if not gap.listed:
        degraded = True
    gap_owners, owners_left = await _owners_of(gap.ids, deadline)
    for agent_id in gap.ids:
        owner = gap_owners.get(agent_id)
        if owner is not None and classify(owner) is not None:
            owners[agent_id] = owner  # ours: listed under `excluded`, never counted
        else:
            # An on-chain agent the mirror does not hold and that is not ours:
            # we cannot say whether it is operated, so it is not counted — and
            # not silently dropped either.
            unreadable.add(agent_id)

    excluded: dict[str, ExcludedOwner] = {}
    external: dict[str, list[Agent]] = {}
    for agent_id, owner in sorted(owners.items()):
        verdict = classify(owner)
        if verdict is not None:
            reason, role = verdict
            row = excluded.setdefault(
                owner,
                ExcludedOwner(owner=owner, owner_explorer=_account_link(owner), reason=reason, role=role, agent_ids=[]),
            )
            row.agent_ids.append(agent_id)
        else:
            external.setdefault(owner, []).append(mirrored[agent_id])

    externals = [a for owner in sorted(external) for a in external[owner]]
    bound_ids, window = await asyncio.gather(_bound_ids(), _window({a.id: owners[a.id] for a in externals}, deadline))
    evidence = window.by_agent if window is not None else {}
    results = {a.id: _external_agent(a, evidence.get(a.id), bound_ids, team_roles) for a in externals}
    for result in results.values():
        if not result.complete:
            unreadable.add(result.agent.agent_id)
    # One scan answers every agent, so this is its span — or 0 when it never
    # ran; kept as the smallest so the claim holds for every agent regardless.
    windows = [r.window_days for r in results.values() if r.window_days is not None]
    window_days = min(windows) if windows else 0.0
    complete = not owners_left and not (window is not None and window.out_of_time)
    if unreadable or not complete:
        degraded = True

    operators = [
        AdoptionOperator(
            owner=owner,
            owner_explorer=_account_link(owner),
            agents=[results[a.id].agent for a in external[owner]],
        )
        for owner in sorted(external)
    ]
    # One workflow that paid two external agents is ONE workflow.
    jobs = {w.job_id_hex for op in operators for a in op.agents for w in a.settled_workflows}
    totals = AdoptionCounts(
        external_agents=len(externals),
        unique_operator_wallets=len(external),
        settled_external_workflows=len(jobs),
    )
    coverage = AdoptionCoverage(
        agents_listed=gap.total if gap.listed else None,
        agents_accounted=len(owners),
        settlement_ledgers_scanned=window.ledgers_scanned if window is not None else 0,
        settlement_ledgers_in_window=window.ledgers_in_window if window is not None else 0,
        external_charges=window.charges if window is not None else 0,
        external_charges_unattributed=window.unattributed if window is not None else 0,
    )
    report = AdoptionReport(
        network=_network(),
        generated_at=int(time.time()),
        window_days=window_days,
        targets=TARGETS,
        totals=totals,
        met=AdoptionMet(
            external_agents=totals.external_agents >= TARGETS.external_agents,
            unique_operator_wallets=totals.unique_operator_wallets >= TARGETS.unique_operator_wallets,
            settled_external_workflows=totals.settled_external_workflows >= TARGETS.settled_external_workflows,
        ),
        operators=operators,
        excluded=[excluded[o] for o in sorted(excluded)],
        degraded=degraded,
        unreadable_agents=sorted(unreadable),
        complete=complete,
        coverage=coverage,
    )
    logger.info(
        "[adoption] external_agents=%d unique_operator_wallets=%d settled_external_workflows=%d window_days=%s "
        "excluded_owners=%d unreadable_agents=%d degraded=%s complete=%s platform_unreadable=%s registry_listed=%s "
        "ledgers=%d/%d charges=%d unattributed=%d elapsed_ms=%d",
        totals.external_agents,
        totals.unique_operator_wallets,
        totals.settled_external_workflows,
        window_days,
        len(report.excluded),
        len(report.unreadable_agents),
        degraded,
        complete,
        ",".join(rule.unreadable) or "-",
        gap.listed,
        coverage.settlement_ledgers_scanned,
        coverage.settlement_ledgers_in_window,
        coverage.external_charges,
        coverage.external_charges_unattributed,
        int((time.monotonic() - started) * 1000),
    )
    return report


def _serialize(report: AdoptionReport) -> bytes:
    return report.model_dump_json().encode()


class PartialReportHeld(RuntimeError):
    """A partial build, not published over the complete report in service."""


async def _build_for_cell() -> AdoptionReport:
    """A build, unless it is partial and a complete report younger than
    REPORT_PARTIAL_KEEPS_COMPLETE_SECONDS is in service: that one is kept
    (the cell records this build as failed and keeps serving it), and the
    next build resumes the reads this one did not finish."""
    report = await build_report()
    held = report_cell.current()
    if (
        not report.complete
        and held is not None
        and held.value.complete
        and held.age_seconds() < REPORT_PARTIAL_KEEPS_COMPLETE_SECONDS
    ):
        raise PartialReportHeld(
            f"partial build kept back: the complete report from {held.age_seconds():.0f} s ago stays in service"
        )
    return report


def registry_fingerprint() -> int:
    """What the report's agent list depends on in the mirror: each on-chain
    agent's id, owner and status. A change means a rebuild is due."""
    return hash(tuple(sorted((a.id, a.owner or "", a.status) for a in onchain_mirror().values())))


def _may_build() -> bool:
    """Whether a build may start: the registry mirror is complete, or boot was
    long enough ago that waiting longer only means serving nothing. Gates the
    requests and the schedule alike."""
    return registry_sync.status().synced or time.time() - state.started_at >= REPORT_BOOT_GRACE_SECONDS


# The report, built behind the request. Never retired by age (a request never
# waits on a scan): the keep-warm schedule below rebuilds it, and a request
# that finds it older than REPORT_REFRESH_SECONDS starts a rebuild behind the
# one it serves — once the registry gate is open.
report_cell: SnapshotCell[AdoptionReport] = SnapshotCell(
    "adoption",
    lambda: _build_for_cell(),
    _serialize,
    lambda report: float(report.generated_at),
    fresh_seconds=REPORT_REFRESH_SECONDS,
    build_timeout_seconds=REPORT_BUILD_BUDGET_SECONDS,
    retry_after_failure_seconds=REPORT_RETRY_AFTER_FAILURE_SECONDS,
    may_build=_may_build,
)
snapshots.keep_warm(
    KeepWarm(
        cell=report_cell,
        every_seconds=REPORT_REFRESH_SECONDS,
        fingerprint=registry_fingerprint,
        min_change_rebuild_seconds=REPORT_REGISTRY_REBUILD_SECONDS,
    )
)
# Every build is saved, and the next process serves it until its own lands.
snapshot_store.persist(
    report_cell,
    AdoptionReport.model_validate_json,
    max_restore_age_seconds=REPORT_RESTORE_MAX_AGE_SECONDS,
)


async def report_snapshot() -> Snapshot[AdoptionReport] | None:
    """The report to serve now, or None while the first one is computed.
    Never waits on a build: one is started if none is running."""
    return await report_cell.get(wait_seconds=0)

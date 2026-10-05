"""Operator readiness — story 5.02's "what do I fix next?", in one read.

An outside operator goes from zero to earning through a chain of steps that
each fail somewhere different: the registration (an unfunded wallet), the
listing (a delist, a price the mirror refuses), the binding, the endpoint (a
quick tunnel that silently died), the routing floor, and then simply no run
yet. Each of those already has an answer somewhere in this backend; none of
them is in one place, so a stuck operator — or whoever is helping them in an
onboarding session — has to know which five routes to read and how to read
them. `check_readiness` reads them all and answers with seven steps, always
the same seven, in the order an operator fixes them.

Each step's source, and why it is the honest one:

  registered        `external_binding.resolve_owner` — the LIVE
                    `AgentRegistry.owner_of`, the same read the bind API
                    authorizes against.
  active            `AgentRegistry.get` (the `active` flag and the price),
                    judged by `registry_sync._to_agent` — the exact gate the
                    marketplace mirror applies — and then the mirror itself,
                    because an agent the sync has not indexed is not listed
                    whatever the chain says.
  bound             the binding store — what dispatch reads.
  reachable         one bounded GET of the STORED bound URL, through the
                    dispatch path's own SSRF guard (see `probe_bound_endpoint`).
  routable          `reputation_svc.fetch_rep` judged by `passes_floor`, the
                    planner's own check. A degraded or superseded read is
                    `unknown`, never `failed`: neither is a verdict on the agent.
  first_run         the ReputationLedger's `rep_state.count`. The ledger bumps
                    it in the same call that publishes a `rated` event, once
                    per rated step, and never decays it — so it is the lifetime
                    count of those events. Counting the events themselves would
                    be the same fact seen through Soroban RPC's ~7-day event
                    retention: an agent last run eight days ago would read as
                    never run. Ratings are written for wallet-authorized runs,
                    so a simulated run is correctly not a first run.
  first_settlement  `settlement_svc.fetch_settlement`'s verified revenue: the
                    oldest `charged` event whose payer is neither the owner nor
                    the platform, with its transaction as evidence.

`ready` is the first five: an agent that is registered, listed, bound,
answering and above the floor is one the planner can route to and dispatch
to. The last two are progress, not readiness.

Cost and safety. The route is public — every fact here is public — but the
probe is an outbound request made from inside our network, so:

  - the whole answer is cached per agent for CACHE_TTL_SECONDS, single-flight
    (`app.stellar.cache`), so a polling console or a burst of callers costs
    one probe per agent per window;
  - the only URL ever fetched is the one already in the binding store, which
    passed the bind API's policy and was proved by the owner's signature; no
    request parameter reaches the probe;
  - the chain reads that only matter for a registered agent wait for the owner
    read, so a stream of made-up ids costs one cached `owner_of` each and never
    a probe (nothing is bound to an id that is not registered);
  - no URL, host or response byte leaves this module or reaches a log: the
    probe reports a coarse outcome and a status code, nothing more.

Every sub-read is bounded and a failing one is `unknown` with a sentence, so a
slow chain can make this answer vaguer, never slower than its bounds, and
never a 500.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlsplit

import httpx

from ..agents.workers.external_http import CONNECT_TIMEOUT_SECONDS, _PinnedAddressTransport
from ..config import settings
from ..state import state
from ..stellar import cache as rcache
from ..stellar import client as sc
from . import external_binding, reachability, registry_sync, reputation_svc, settlement_svc
from .binding_store import BindingRecord, get_binding_store
from .endpoint_policy import EndpointPolicyError, validate_endpoint_url

logger = logging.getLogger(__name__)

StepKey = Literal["registered", "active", "bound", "reachable", "routable", "first_run", "first_settlement"]
StepStatus = Literal["done", "todo", "failed", "unknown"]

# The frozen contract's order, which is also the order an operator fixes them.
STEP_KEYS: tuple[StepKey, ...] = (
    "registered",
    "active",
    "bound",
    "reachable",
    "routable",
    "first_run",
    "first_settlement",
)
# Routable and dispatchable. The first run and first settlement are progress.
READY_KEYS: frozenset[StepKey] = frozenset({"registered", "active", "bound", "reachable", "routable"})

# The whole answer, per agent. Long enough that a console polling it and a
# helper refreshing it in a session share one probe; short enough that an
# operator who just fixed something sees it within half a minute.
CACHE_TTL_SECONDS = 30.0

# Bounds on each sub-read. The owner read gates the chain reads that follow it,
# and the endpoint check runs beside both, so the worst case is
# max(BINDING_READ + PROBE, OWNER_READ + SETTLEMENT) — 10 s — rather than a sum.
OWNER_READ_TIMEOUT_SECONDS = 4.0
CHAIN_READ_TIMEOUT_SECONDS = 4.0
# The settlement scan pages Soroban RPC's event window and can run ~20 s cold.
# Cut off here, its flight keeps running under the cache's shield and lands for
# the next check, so "still scanning" is an answer that fixes itself.
SETTLEMENT_TIMEOUT_SECONDS = 6.0
BINDING_READ_TIMEOUT_SECONDS = 2.0
# One GET, connect included. The connect bound is the dispatch path's own; the
# total is far below the dispatch deadline because a health answer is not work.
PROBE_TIMEOUT_SECONDS = 5.0
PROBE_CONNECT_TIMEOUT_SECONDS = CONNECT_TIMEOUT_SECONDS
# AgentRegistry.get shares its cache key with GET /api/stellar/agent/{id}
# (routers/stellar.py), so both routes collapse onto one read; same TTL.
REGISTRY_READ_TTL_SECONDS = 3.0

_PROBE_HEADERS = {
    "User-Agent": "orizon-readiness/1",
    # The body is never read, but nothing is asked to be compressed either.
    "Accept-Encoding": "identity",
}

_QUICK_TUNNEL_SUFFIX = ".trycloudflare.com"


@dataclass(frozen=True)
class Step:
    key: StepKey
    status: StepStatus
    detail: str
    action: str | None = None
    evidence: dict[str, str] | None = None


@dataclass(frozen=True)
class Readiness:
    agent_id: str
    checked_at: int
    ready: bool
    steps: tuple[Step, ...]


def is_ready(steps: tuple[Step, ...]) -> bool:
    """True only when every READY_KEYS step is `done`."""
    done = {step.key for step in steps if step.status == "done"}
    return READY_KEYS <= done


# ── the probe ───────────────────────────────────────────────────


ProbeOutcome = Literal[
    "ok",
    "http_status",
    "timeout",
    "connection_refused",
    "tls_error",
    "unresolvable",
    "endpoint_refused",
    "connection_failed",
    "transport_error",
]


@dataclass(frozen=True)
class ProbeResult:
    """A coarse outcome. Never the URL, never a header, never a body byte."""

    outcome: ProbeOutcome
    status_code: int | None = None
    # The endpoint-policy rule that refused the URL, for `endpoint_refused` and
    # `unresolvable`. A rule name from a closed vocabulary, never the message,
    # which quotes the URL.
    rule: str | None = None


def _inner_transport() -> httpx.AsyncBaseTransport:
    """The socket-level transport under the pin. A seam for the tests, which
    put an in-process responder here and keep the real pin above it."""
    return httpx.AsyncHTTPTransport()


def _probe_transport() -> httpx.AsyncBaseTransport:
    """The dispatch path's own SSRF guard over the socket transport: resolves
    the host, refuses it unless EVERY address is public, then dials the
    address it checked, with SNI and certificate verification still against
    the name."""
    return _PinnedAddressTransport(_inner_transport())


def _probe_request(endpoint_url: str) -> httpx.Request:
    """The one request the probe sends. The timeouts ride on the request, as
    httpx.AsyncClient would have put them there."""
    timeout = httpx.Timeout(PROBE_TIMEOUT_SECONDS, connect=PROBE_CONNECT_TIMEOUT_SECONDS)
    return httpx.Request("GET", endpoint_url, headers=_PROBE_HEADERS, extensions={"timeout": timeout.as_dict()})


def _caused_by(error: BaseException, kind: type[BaseException]) -> bool:
    """Whether `kind` appears anywhere in `error`'s cause/context chain.

    httpx wraps httpcore's ConnectError, which wraps the socket's own error —
    a refused connect is three levels down, as a ConnectionRefusedError.
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [error]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, kind):
            return True
        stack.extend((current.__cause__, current.__context__))
    return False


async def _get_status(transport: httpx.AsyncBaseTransport, endpoint_url: str) -> ProbeResult:
    # The transport, not an httpx.AsyncClient, and deliberately. The client
    # logs every request at INFO as "HTTP Request: GET <full URL>", which would
    # put the bound URL — query-string credentials and all — into our log on
    # every probe. A bare transport also cannot follow a redirect (a followed
    # 30x would fetch a URL nobody validated) or pick up an HTTPS_PROXY from
    # the environment (a proxy would resolve the name past the pin).
    response = await transport.handle_async_request(_probe_request(endpoint_url))
    try:
        # The body is never read: the status line is the whole answer.
        code = response.status_code
    finally:
        await response.aclose()
    return ProbeResult("ok" if 200 <= code < 300 else "http_status", status_code=code)


async def probe_bound_endpoint(endpoint_url: str) -> ProbeResult:
    """One bounded GET of an already-bound endpoint. Never raises.

    `endpoint_url` must come from the binding store and nowhere else; the
    caller (`_check_endpoint`) is the only one there is. It is judged again
    anyway, twice, by the checks dispatch applies: the pure URL policy (the
    rules may have tightened since the bind) and, at connect time, the pinned
    transport's resolve-and-check (the DNS may have been repointed since).
    """
    try:
        validate_endpoint_url(endpoint_url)
    except EndpointPolicyError as e:
        return ProbeResult("endpoint_refused", rule=e.rule)
    transport = _probe_transport()
    try:
        try:
            return await asyncio.wait_for(_get_status(transport, endpoint_url), timeout=PROBE_TIMEOUT_SECONDS)
        except EndpointPolicyError as e:
            outcome: ProbeOutcome = "unresolvable" if e.rule == "unresolvable_host" else "endpoint_refused"
            return ProbeResult(outcome, rule=e.rule)
        except (TimeoutError, httpx.TimeoutException):
            return ProbeResult("timeout")
        except httpx.ConnectError as e:
            if _caused_by(e, ssl.SSLError):
                return ProbeResult("tls_error")
            if _caused_by(e, ConnectionRefusedError):
                return ProbeResult("connection_refused")
            return ProbeResult("connection_failed")
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as e:
            return ProbeResult("tls_error" if _caused_by(e, ssl.SSLError) else "transport_error")
    finally:
        await transport.aclose()


def is_quick_tunnel(endpoint_url: str) -> bool:
    """A Cloudflare quick tunnel: `https://<random words>.trycloudflare.com`."""
    try:
        host = (urlsplit(endpoint_url).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    return host.endswith(_QUICK_TUNNEL_SUFFIX)


# ── bounded sub-reads ───────────────────────────────────────────

T = TypeVar("T")


@dataclass(frozen=True)
class _Read(Generic[T]):
    """A sub-read's outcome. `failure` is None on success — `value` alone
    cannot say, because None is a real answer for the owner and the binding."""

    value: T | None
    failure: Literal["timeout", "error"] | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


async def _bounded(name: str, agent_id: str, make: Callable[[], Awaitable[T]], seconds: float) -> _Read[T]:
    """Run one sub-read under its own bound. Never raises.

    Logs the read's name and the exception TYPE only: an exception's text is
    whatever its author put there, and this module promises its logs carry no
    URL.
    """
    try:
        return _Read(await asyncio.wait_for(make(), timeout=seconds))
    except TimeoutError:
        logger.warning("readiness: agent_id=%s read=%s outcome=timeout bound_s=%g", agent_id, name, seconds)
        return _Read(None, "timeout")
    except Exception as e:
        logger.warning("readiness: agent_id=%s read=%s outcome=error error=%s", agent_id, name, type(e).__name__)
        return _Read(None, "error")


async def _read_registry_record(agent_id: str) -> dict[str, Any]:
    """`AgentRegistry.get(agent_id)`, cached under routers/stellar.py's key."""
    contract_id = settings.stellar_agent_registry
    if not contract_id:
        raise RuntimeError("AgentRegistry is not configured")

    async def _fetch() -> Any:
        return await asyncio.to_thread(sc.simulate_read, contract_id, "get", [sc.sym(agent_id)])

    raw = await rcache.get_or_set(f"agent:{agent_id}", REGISTRY_READ_TTL_SECONDS, _fetch)
    if not isinstance(raw, dict):
        raise TypeError(f"AgentRegistry.get returned {type(raw).__name__}")
    return raw


@dataclass(frozen=True)
class _Endpoint:
    binding: _Read[BindingRecord | None]
    # None when nothing is bound, the binding was unreadable, or the probe
    # itself overran its outer bound.
    probe: ProbeResult | None


async def _check_endpoint(agent_id: str) -> _Endpoint:
    """The binding, and — only if there is one — a probe of the URL it holds."""
    binding = await _bounded(
        "binding", agent_id, lambda: get_binding_store().get(agent_id), BINDING_READ_TIMEOUT_SECONDS
    )
    record = binding.value
    if not binding.ok or record is None:
        return _Endpoint(binding, None)
    started = time.monotonic()
    # The probe bounds itself; this outer bound is belt and braces, with a
    # second's slack so the inner one is the one that normally fires.
    probe = await _bounded(
        "probe", agent_id, lambda: probe_bound_endpoint(record.endpoint_url), PROBE_TIMEOUT_SECONDS + 1.0
    )
    result = probe.value if probe.ok else None
    # Outcome, status and timing — never the URL or its host.
    logger.info(
        "readiness probe: agent_id=%s outcome=%s status=%s elapsed_ms=%d quick_tunnel=%s",
        agent_id,
        result.outcome if result is not None else probe.failure,
        result.status_code if result is not None and result.status_code is not None else "-",
        round((time.monotonic() - started) * 1000),
        is_quick_tunnel(record.endpoint_url),
    )
    return _Endpoint(binding, result)


# ── the steps ───────────────────────────────────────────────────


def _explorer(kind: Literal["account", "tx"], ident: str) -> str:
    return f"https://stellar.expert/explorer/{sc.explorer_network()}/{kind}/{ident}"


def _register_action() -> str:
    if settings.is_mainnet():
        return "Register the agent on the Register page, signing with the wallet that will own it."
    return (
        "Register the agent on the Register page. Your wallet needs testnet XLM to sign it: if the "
        "transaction fails because the account does not exist or is unfunded, fund it with friendbot "
        "(https://friendbot.stellar.org/?addr=YOUR_G_ADDRESS) and register again."
    )


_RETRY_ACTION = "Check again in a minute. If it stays unknown, the Stellar RPC is degraded — it is not your agent."
_NOT_REGISTERED = "This agent id is not registered yet."


def registered_step(owner: _Read[str | None]) -> Step:
    if not owner.ok:
        return Step("registered", "unknown", "The AgentRegistry could not be read just now.", _RETRY_ACTION)
    if owner.value is None:
        return Step(
            "registered",
            "todo",
            "No agent with this id is registered in the on-chain AgentRegistry.",
            _register_action(),
        )
    return Step(
        "registered",
        "done",
        f"Registered on-chain; owned by {owner.value}.",
        evidence={"explorer": _explorer("account", owner.value)},
    )


def active_step(agent_id: str, owner: _Read[str | None], record: _Read[dict[str, Any]] | None) -> Step:
    if not owner.ok:
        return Step("active", "unknown", "Can't tell until the registration can be read.", _RETRY_ACTION)
    if owner.value is None or record is None:
        return Step("active", "todo", _NOT_REGISTERED, "Register the agent first; it is listed as it registers.")
    if not record.ok or record.value is None:
        return Step("active", "unknown", "The agent's registry record could not be read just now.", _RETRY_ACTION)
    raw = record.value
    if not raw.get("active"):
        return Step(
            "active",
            "todo",
            "The agent is delisted on-chain, so the marketplace does not offer it.",
            "Relist it from the Manage panel on the Agents page (it signs set_active true).",
        )
    if agent_id.startswith("agt_"):
        return Step(
            "active",
            "failed",
            "Ids starting agt_ are reserved for the built-in catalog, so the marketplace never lists this one.",
            "Register your agent under an id that does not start with agt_, then bind and run that one.",
        )
    try:
        registry_sync._to_agent(raw)
    except registry_sync.UnbelievablePrice as e:
        return Step(
            "active",
            "failed",
            f"The marketplace refuses this agent's on-chain price: {e}.",
            "Update the price from the Manage panel on the Agents page to one inside that range.",
        )
    except Exception:
        return Step("active", "unknown", "The agent's registry record could not be interpreted.", _RETRY_ACTION)
    listed = state.agents.get(agent_id)
    if listed is None or listed.source != "onchain":
        return Step(
            "active",
            "todo",
            "Active on-chain, but the marketplace has not indexed it yet.",
            f"Wait for the next registry sync (every {settings.registry_sync_seconds} s) and check again.",
        )
    return Step("active", "done", "Active on-chain and listed in the marketplace.")


def bound_step(endpoint: _Endpoint) -> Step:
    if not endpoint.binding.ok:
        return Step("bound", "unknown", "The binding could not be read just now.", _RETRY_ACTION)
    record = endpoint.binding.value
    if record is None:
        return Step(
            "bound",
            "todo",
            "No endpoint is bound, so no work can be dispatched to this agent.",
            "Bind an HTTPS endpoint on the Bind page",
        )
    return Step("bound", "done", "An HTTPS endpoint is bound to this agent.")


_QUICK_TUNNEL_WARNING = (
    " Warning: this endpoint is a Cloudflare quick tunnel (trycloudflare.com). Quick tunnels are "
    "ephemeral — the URL dies whenever cloudflared stops or restarts, and every dispatch then fails. "
    "Use a named tunnel or a hosted service for an agent that should keep earning, and rebind to it."
)

_REBIND = "Rebind an https URL with a public host on the Bind page."


def _status_step(code: int) -> tuple[StepStatus, str, str | None]:
    """(status, detail, action) for an endpoint that answered with `code`."""
    if code == 405:
        return (
            "done",
            "Your endpoint is up: it answered 405 to a GET. Dispatches are POSTs, so that is fine — answering "
            "GET with 200 (as the reference agent does) makes this check clearer.",
            None,
        )
    if 300 <= code < 400:
        return (
            "failed",
            f"Your endpoint answered {code}, a redirect. Dispatches never follow redirects.",
            "Bind the URL your endpoint redirects to, exactly, on the Bind page.",
        )
    if code in (401, 403):
        return (
            "failed",
            f"Your endpoint answered {code} to an unauthenticated GET.",
            "Let GET through with a 200 when the agent is up, so its health can be checked; keep "
            "authenticating dispatches by their signature.",
        )
    if code == 404:
        return (
            "failed",
            "Your endpoint answered 404.",
            "Check the bound URL's path is one your agent serves, and rebind on the Bind page if it is not.",
        )
    if code in (502, 503, 504, 530):
        return (
            "failed",
            f"Your endpoint answered {code}: the host or tunnel in front of it is up, the agent behind it is not.",
            f"Your endpoint answered {code} — is your agent process running, on the port your host or tunnel "
            "forwards to?",
        )
    return (
        "failed",
        f"Your endpoint answered {code}.",
        f"Your endpoint answered {code} — make GET answer 200 when the agent is up, and check its logs.",
    )


_PROBE_FAILURES: dict[str, tuple[str, str]] = {
    "timeout": (
        f"Your endpoint did not answer within {PROBE_TIMEOUT_SECONDS:g} s.",
        "Check your agent process is running and not stuck. A free-tier host that sleeps can take longer "
        "to wake — check again in a minute.",
    ),
    "connection_refused": (
        "Your endpoint's host refused the connection: nothing is listening there.",
        "Start your agent, and check it listens on the port your host or tunnel forwards to.",
    ),
    "tls_error": (
        "The TLS handshake with your endpoint failed.",
        "Serve a valid certificate for the bound hostname — a tunnel or PaaS URL does this for you.",
    ),
    "unresolvable": (
        "Your endpoint's hostname no longer resolves in DNS.",
        "If it was a temporary tunnel, it is gone: start a stable host and rebind it on the Bind page.",
    ),
    "connection_failed": (
        "No connection could be made to your endpoint.",
        "Check your host is up and reachable from the public internet.",
    ),
    "transport_error": (
        "The connection to your endpoint broke before it answered.",
        "Check your agent's HTTP server logs; it hung up or spoke malformed HTTP.",
    ),
}


def reachable_step(endpoint: _Endpoint) -> Step:
    if not endpoint.binding.ok:
        return Step("reachable", "unknown", "Can't check until the binding can be read.", _RETRY_ACTION)
    record = endpoint.binding.value
    if record is None:
        return Step(
            "reachable",
            "todo",
            "Nothing is bound, so there is nothing to check yet.",
            "Bind an HTTPS endpoint on the Bind page, then check again.",
        )
    warning = _QUICK_TUNNEL_WARNING if is_quick_tunnel(record.endpoint_url) else ""
    probe = endpoint.probe
    if probe is None:
        return Step("reachable", "unknown", "The health check did not complete." + warning, _RETRY_ACTION)
    if probe.outcome == "ok":
        return Step("reachable", "done", f"Your endpoint answered {probe.status_code}." + warning)
    if probe.outcome == "http_status" and probe.status_code is not None:
        status, detail, action = _status_step(probe.status_code)
        return Step("reachable", status, detail + warning, action)
    if probe.outcome == "endpoint_refused":
        return Step(
            "reachable",
            "failed",
            f"The bound endpoint no longer passes the endpoint policy (rule: {probe.rule}), so nothing is sent "
            "to it." + warning,
            _REBIND,
        )
    detail, action = _PROBE_FAILURES.get(probe.outcome, _PROBE_FAILURES["transport_error"])
    if probe.outcome == "unresolvable" and warning:
        action = "Your quick tunnel has gone. Start a named tunnel or a hosted service, and rebind on the Bind page."
    return Step("reachable", "failed", detail + warning, action)


def _rep_unreadable(key: StepKey, rep: _Read[reputation_svc.RepInfo]) -> Step | None:
    """`unknown` for a read that timed out, or degraded to the prior."""
    if not rep.ok or rep.value is None:
        return Step(key, "unknown", "The reputation ledger could not be read just now.", _RETRY_ACTION)
    if rep.value.degraded:
        return Step(
            key,
            "unknown",
            "The reputation ledger could not be read, so only the default prior is known.",
            _RETRY_ACTION,
        )
    return None


def routable_step(owner: _Read[str | None], rep: _Read[reputation_svc.RepInfo] | None) -> Step:
    if not owner.ok:
        return Step("routable", "unknown", "Can't tell until the registration can be read.", _RETRY_ACTION)
    if owner.value is None or rep is None:
        return Step("routable", "todo", _NOT_REGISTERED, "Register the agent first.")
    unreadable = _rep_unreadable("routable", rep)
    if unreadable is not None:
        return unreadable
    info = rep.value
    assert info is not None  # _rep_unreadable answered for None
    if info.superseded:
        return Step(
            "routable",
            "unknown",
            "A rating has just landed on this agent; its new score is being read.",
            "Check again in a few seconds.",
        )
    floor = settings.reputation_floor_bps
    score = f"Scores {info.lower_bound_bps} bps against the {floor} bps routing floor"
    if reputation_svc.passes_floor(info):
        if info.source == "prior":
            return Step("routable", "done", f"{score}: a new agent's prior, which clears the floor by design.")
        return Step("routable", "done", f"{score}, so the planner routes to it.")
    return Step(
        "routable",
        "failed",
        f"{score}, so the planner will not route to it.",
        "Its recent ratings pulled it under the floor. See why on the Operator page's routing standing; old "
        "evidence decays about 7.5% a week, so the score recovers as it ages.",
    )


def first_run_step(owner: _Read[str | None], rep: _Read[reputation_svc.RepInfo] | None) -> Step:
    if not owner.ok:
        return Step("first_run", "unknown", "Can't tell until the registration can be read.", _RETRY_ACTION)
    if owner.value is None or rep is None:
        return Step("first_run", "todo", _NOT_REGISTERED, "Register the agent first.")
    if not settings.reputation_enabled or not settings.stellar_reputation_ledger:
        return Step(
            "first_run",
            "unknown",
            "This deployment does not record runs on a reputation ledger, so a first run can't be confirmed.",
            "Ask the Orizon team to confirm your first run from the task trace.",
        )
    unreadable = _rep_unreadable("first_run", rep)
    if unreadable is not None:
        return unreadable
    info = rep.value
    assert info is not None
    if info.count > 0:
        plural = "step" if info.count == 1 else "steps"
        return Step("first_run", "done", f"{info.count} rated {plural} on-chain (ReputationLedger).")
    if info.stale:
        return Step("first_run", "unknown", "Only an old reputation read is available.", _RETRY_ACTION)
    return Step(
        "first_run",
        "todo",
        "No step has been dispatched to this agent and rated on-chain yet.",
        "Once the steps above are done, run a wallet-authorized workflow on the Run page whose goal needs this "
        "agent's skill; each step is rated on-chain when the run settles.",
    )


def first_settlement_step(owner: _Read[str | None], evidence: _Read[settlement_svc.SettlementEvidence] | None) -> Step:
    if not owner.ok:
        return Step("first_settlement", "unknown", "Can't tell until the registration can be read.", _RETRY_ACTION)
    if owner.value is None or evidence is None:
        return Step("first_settlement", "todo", _NOT_REGISTERED, "Register the agent first.")
    if not evidence.ok or evidence.value is None:
        detail = (
            "The settlement scan is still running."
            if evidence.failure == "timeout"
            else "Settlements could not be read just now."
        )
        return Step("first_settlement", "unknown", detail, "Check again in 30 seconds.")
    ev = evidence.value
    if ev.unavailable is not None:
        return Step("first_settlement", "unknown", f"Settlements could not be read: {ev.unavailable}.", _RETRY_ACTION)
    revenue = [entry for entry in ev.entries if not entry.self_payment]
    if revenue:
        first = revenue[0]  # entries are oldest-first
        if first.tx_hash is None:
            return Step("first_settlement", "done", "A buyer has paid this agent on-chain.")
        return Step(
            "first_settlement",
            "done",
            "A buyer has paid this agent on-chain.",
            evidence={"tx_hash": first.tx_hash, "explorer": _explorer("tx", first.tx_hash)},
        )
    window = f"the last {ev.window_days:g} days" if ev.window_days > 0 else "the window the RPC still holds"
    if ev.entries:
        return Step(
            "first_settlement",
            "todo",
            f"{len(ev.entries)} payment(s) in {window}, but none from a third-party buyer — the agent's own owner "
            "or the platform paid them, so none counts as revenue.",
            "Run a paid workflow from a buyer wallet that is not the agent's owner.",
        )
    return Step(
        "first_settlement",
        "todo",
        f"No settled payment to this agent in {window} (Stellar RPC keeps about 7 days of events).",
        "Run a paid, wallet-authorized workflow on the Run page from a buyer wallet that is not the agent's "
        "owner; the payout settles when the run completes.",
    )


# ── the answer ──────────────────────────────────────────────────


async def _compute(agent_id: str) -> Readiness:
    """Every sub-read, bounded and as concurrent as their dependencies allow.

    The endpoint check (binding → probe) runs beside everything else. The three
    chain reads that only mean something for a registered agent wait for the
    owner read, then run together: an id nobody registered costs one cached
    `owner_of` and nothing more.
    """
    endpoint_task = asyncio.ensure_future(_check_endpoint(agent_id))
    try:
        owner = await _bounded(
            "owner", agent_id, lambda: external_binding.resolve_owner(agent_id), OWNER_READ_TIMEOUT_SECONDS
        )
        record: _Read[dict[str, Any]] | None = None
        rep: _Read[reputation_svc.RepInfo] | None = None
        settlement: _Read[settlement_svc.SettlementEvidence] | None = None
        if owner.ok and owner.value is not None:
            record, rep, settlement = await asyncio.gather(
                _bounded("registry", agent_id, lambda: _read_registry_record(agent_id), CHAIN_READ_TIMEOUT_SECONDS),
                _bounded(
                    "reputation", agent_id, lambda: reputation_svc.fetch_rep(agent_id), CHAIN_READ_TIMEOUT_SECONDS
                ),
                _bounded(
                    "settlement",
                    agent_id,
                    lambda: settlement_svc.fetch_settlement(agent_id),
                    SETTLEMENT_TIMEOUT_SECONDS,
                ),
            )
        endpoint = await endpoint_task
    finally:
        if not endpoint_task.done():
            endpoint_task.cancel()

    steps = (
        registered_step(owner),
        active_step(agent_id, owner, record),
        bound_step(endpoint),
        reachable_step(endpoint),
        routable_step(owner, rep),
        first_run_step(owner, rep),
        first_settlement_step(owner, settlement),
    )
    # The planner reads this verdict (D-084): an endpoint this check just found
    # dead is left out of plans until a fresh probe says otherwise.
    reachability.record(agent_id, steps[STEP_KEYS.index("reachable")].status)
    ready = is_ready(steps)
    logger.info(
        "readiness: agent_id=%s ready=%s %s",
        agent_id,
        ready,
        " ".join(f"{step.key}={step.status}" for step in steps),
    )
    return Readiness(agent_id=agent_id, checked_at=int(time.time()), ready=ready, steps=steps)


def _all_unknown(agent_id: str) -> Readiness:
    steps = tuple(
        Step(key, "unknown", "The readiness check could not run just now.", _RETRY_ACTION) for key in STEP_KEYS
    )
    return Readiness(agent_id=agent_id, checked_at=int(time.time()), ready=False, steps=steps)


async def check_readiness(agent_id: str) -> Readiness:
    """The cached, single-flight readiness answer for one agent. Never raises.

    Concurrent callers share one computation, and a caller that walks away
    does not cancel it (the cache awaits it through a shield), so the next
    caller gets the finished answer instead of a second probe.
    """

    async def _produce() -> Readiness:
        return await _compute(agent_id)

    try:
        result = await rcache.get_or_set(f"readiness:{agent_id}", CACHE_TTL_SECONDS, _produce)
    except Exception as e:
        logger.warning("readiness: agent_id=%s outcome=error error=%s", agent_id, type(e).__name__)
        return _all_unknown(agent_id)
    return result if isinstance(result, Readiness) else _all_unknown(agent_id)

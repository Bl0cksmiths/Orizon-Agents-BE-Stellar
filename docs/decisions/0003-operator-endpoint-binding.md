# ADR 0003 — Operator endpoint binding: storage, authority, and the trust asymmetry

- **Status:** Accepted (story 2.01 / BLO-18), 2026-09-15
- **Deciders:** Danielle (lead)
- **Gates:** Epic 2 — every later story assumes a binding exists and is trustworthy
- **Supersedes nothing.** Extends ADR 0001 (Candidate A, off-chain binding) by
  deciding the two things 0001 deferred: *where the binding lives* and *how we
  know the binder owns the agent.*

## Context

Story 1.06 shipped two prototypes — `app/services/external_binding.py`
(challenge/verify) and `app/agents/workers/external_http.py` (dispatch + SSRF
rules). Both are complete and tested **as prototypes**, and neither is imported
by anything under `app/`. 2.01 turns them into a real feature, which forces
three decisions the spike was allowed to skip.

### 1. This repo persists nothing

`AppState` is documented as "process-local … contents are lost on restart"
(`app/state.py:14-22`). `app/pdax/ramp_store.py:1-11` says the same and names
the fix it never made. `requirements.txt` has no database driver. `render.yaml`
declares no `disk:` and runs `plan: free` with `--workers 1`.

AC-5 ("the binding survives a backend restart") is therefore **not** a
refactor — it is the first durable write this service has ever made.

### 2. The owner check inverts the repo's own failure convention

Every registry read in this codebase fails **open**: `_agent_exists`
(`app/routers/stellar.py:387-405`) returns `False` on any exception,
`agent_id_available` says outright that "the chain's `AlreadyExists` is the real
guard", and `registry_sync` logs and continues. That is correct *there*, because
in each case **the chain still guards the operation downstream** — the worst a
wrong answer buys is a friendlier error message before a transaction fails.

A binding has **no downstream chain guard.** Nothing is submitted, nothing is
signed by us, no contract re-checks anything. The owner read **is** the
authorization. Fail-open here means: *RPC degrades → anyone binds any agent to
any endpoint they control.*

### 3. The obvious shortcut is an authorization bug

`state.agents[agent_id].owner` is already in memory and is exactly the "cached
owner list" AC-1 forbids. It is wrong three ways: up to 15 s stale by design,
**stale indefinitely during an RPC outage** (the sync loop fails open and never
dies), and `None` for seeded agents while skipping the `agt_` namespace.

## Decision

### D1 — Storage: Neon Postgres, behind a `BindingStore` interface

Selected on one criterion the others fail: **it must still exist on demo day.**

| Option | Verdict |
|---|---|
| **Neon free tier** | **Chosen.** No expiry, no forced pause, survives restarts and redeploys. |
| Render Postgres free | **Expires after 30 days** — i.e. exactly at demo/award time. |
| Render Key Value | No disk persistence on free. |
| Supabase free | Pauses after 1 week idle, needs a manual unpause. |
| SQLite on the instance filesystem | Free instances spin down after 15 min idle and restart **from the image** — the file is gone. |
| Render persistent disk | Paid, and breaks zero-downtime deploys. |
| On-chain | **Structurally impossible** — see D4. |

The dependency is **`asyncpg` only** (no ORM, no migration framework: this is
one table). `database_url` defaults to `""`, which selects
`InMemoryBindingStore`, so **the test suite stays hermetic and offline** and the
82 % coverage gate is unaffected. Production sets `DATABASE_URL` in the Render
dashboard and gets `PostgresBindingStore` with no code change.

Two operational notes that are easy to get wrong:

- **`min_size=0`** on the pool. A free instance idles; a pool that insists on a
  live connection will wake to a dead socket.
- **Do not `@lru_cache` the store resolver.** The cache would freeze the
  in-memory store chosen at first import and silently ignore a later
  `DATABASE_URL`, which is precisely the bug this abstraction exists to avoid.

The table is **append-only** — a rebind inserts a new row rather than updating
one — so AC-6's "records a timestamp" and the audit history come for free, and
`previous_endpoint_url` is derivable rather than maintained.

### D2 — Authority: read `owner` from the chain, per request, fail **closed**

`resolve_owner(agent_id)` calls `AgentRegistry.owner_of(Symbol) -> Address`,
which is confirmed present in the deployed wasm and proven live by
`PaymentEscrow.charge` cross-calling it (`contract/payment-escrow/src/lib.rs:141`).
One single-agent read — never a `list_ids` scan.

- **Its own cache namespace, `agentowner:{agent_id}`, TTL 3 s.** It must *never*
  share `agent:{agent_id}`: that key is fail-open **and publicly pollable** via
  `GET /api/stellar/agent/{id}`, so sharing it would let an attacker pre-warm
  the authorization cache.
- **RPC failure raises `OwnerLookupError` → 503 `registry_unavailable`.** Not
  404, and never "unknown owner, allow". "Chain unreachable" and "no such agent"
  are different facts and the API says so.
- **The nonce is checked before the chain read**, so the endpoint cannot be used
  as a free unauthenticated `owner_of` amplifier.
- **A failed lookup must not consume the nonce**, or an RPC outage becomes a
  griefing tool against every pending bind.

**Residual risk, accepted and bounded:** within the 3 s TTL a *former* owner
could re-bind to a host they control. The deployed AgentRegistry has **no
ownership-transfer entrypoint**, so this is currently unreachable — but the code
must not depend on that, which is why the TTL is 3 s and not 15 s.

### D3 — The signature covers the endpoint, not just the nonce

The 1.06 prototype signs `nonce` alone, so a signature captured inside its
5-minute window can be replayed to bind a **different** URL. The signed message
is therefore:

```
orizon-bind:v1:{agent_id}:{endpoint_url}:{nonce}
```

Domain-separated (`orizon-bind:v1`) so an Orizon signature can never be a valid
signature for another protocol, and versioned so the format can move. This is
cheap now and expensive once the frontend ships its signing call.

**Two signature encodings are accepted, deliberately.** Wallets do not agree on
what "sign this message" means. The frontend calls
`kit.signMessage(message)`, which delegates to the installed extension, and
Freighter implements **SEP-53**, which does *not* sign the raw bytes:

```
payload   = b"Stellar Signed Message:\n" + message.encode("utf-8")
digest    = sha256(payload).digest()          # single round
signature = ed25519_sign(private_key, digest)
```

Verified against the SEP-0053 spec, not assumed. Had we verified only raw bytes,
the flow would have failed end-to-end with the most common Stellar wallet — and
failed as `not_agent_owner`, i.e. looking like a rejected owner rather than a
format mismatch, which is about the most expensive possible way to learn this.
`verify_challenge` therefore accepts **either** encoding.

This is an interoperability widening, not a trust widening: both payloads are
derived deterministically from the *same* domain-separated message, which
already pins protocol, version, agent id, endpoint URL and nonce. There is no
message an attacker can get signed under one encoding that becomes a different
authorisation under the other. It is exactly two candidates — never a
"try some prefixes" loop, which would be a trust widening.

`stellar-sdk` 13.2.1 implements SEP-53 directly as `Keypair.sign_message` /
`verify_message`, so production calls the SDK rather than re-deriving the hash:
a second copy of a signing framing inside an authorization path is a copy that
can drift from the SDK's. The hand-built construction lives in the **tests**,
where it is worth having — it pins the wire format against the spec, so the
suite fails loudly if the SDK's framing ever moves, instead of merely proving
the SDK agrees with itself.

### D4 — Why the binding is not on-chain

Not a preference — the contracts cannot express it. `AgentRegistry` has no
endpoint field and no `set_endpoint`/`set_name`. `AttestationRegistry` cannot
hold a URL because a Soroban `Symbol` forbids `:` `/` `.` and `-`, and `seal` is
write-once. Storing it would require a contract change and a redeploy, which
would invalidate the four contract ids published as SOW §6.1 award evidence —
the same bar ADR 0002 refused to cross.

## Frozen interfaces

These are the contract between the parallel build lanes. **Signatures are
frozen; implementations are not.**

### `app/services/endpoint_policy.py` (new — moved out of the worker)

```python
ENDPOINT_RULES: frozenset[str]  # {"malformed_url", "scheme_not_https", "no_host",
                                #  "non_public_address", "loopback_host",
                                #  "metadata_host", "unresolvable_host"}

class EndpointPolicyError(ValueError):
    rule: str          # one of ENDPOINT_RULES — assertable, unlike prose
    def __init__(self, rule: str, message: str) -> None: ...

def validate_endpoint_url(url: str) -> None: ...
    # pure, no DNS, no I/O. Raises EndpointPolicyError.

async def resolve_and_check(url: str) -> tuple[str, ...]: ...
    # validate_endpoint_url, then resolve the host and apply the SAME address
    # predicate to EVERY returned address. Returns the resolved addresses.
    # Raises EndpointPolicyError("unresolvable_host"|"non_public_address", ...).
```

The rules move **wholesale**; they are not duplicated. Two copies of an SSRF
block-list drift, and the one that drifts is the one nobody dispatches through.
`app.agents.workers.external_http.validate_endpoint_url` keeps its exact current
signature and `ExternalDispatchError` type by catching `EndpointPolicyError` and
re-raising — so the worker's contract and its nine existing tests are untouched.

A bind-time DNS lookup is an outbound *query*, not an outbound *request to the
endpoint*; AC-4's "no outbound request" is still satisfied, and the route tests
assert it the way `test_external_endpoint_validation.py:102-120` does.

### `app/services/binding_store.py` (new)

```python
@dataclass(frozen=True)
class BindingRecord:
    agent_id: str
    endpoint_url: str
    owner: str                       # G-address that proved ownership at bind time
    bound_at: float                  # epoch seconds (house style — app/schemas.py:70)
    previous_endpoint_url: str | None

class BindingStore(Protocol):
    async def get(self, agent_id: str) -> BindingRecord | None: ...
    async def put(self, agent_id: str, endpoint_url: str, owner: str) -> BindingRecord: ...
    async def close(self) -> None: ...

class InMemoryBindingStore: ...     # bounded, mirrors ramp_store's eviction
class PostgresBindingStore: ...     # asyncpg, append-only, min_size=0

def get_binding_store() -> BindingStore: ...   # module singleton — NOT lru_cache
async def close_binding_store() -> None: ...   # called from the lifespan shutdown
```

`put` returns the **new** record with `previous_endpoint_url` already populated,
so the router never reads-then-writes and AC-6 cannot race.

### `app/services/external_binding.py` (reworked)

```python
CHALLENGE_TTL_SECONDS = 300
MAX_CHALLENGES = 500            # matches ramp_store._MAX_RAMPS
OWNER_CACHE_TTL_SECONDS = 3.0

class OwnerLookupError(RuntimeError): ...   # chain unreadable — NOT "not found"

def binding_message(agent_id: str, endpoint_url: str, nonce: str) -> str: ...
def issue_challenge(agent_id: str, endpoint_url: str) -> tuple[str, float]: ...
    # -> (nonce, expires_at). Bounded: evicts expired first, then oldest.
async def resolve_owner(agent_id: str) -> str | None: ...
    # None = agent not on-chain. Raises OwnerLookupError if the chain is unreadable.
def verify_challenge(agent_id: str, endpoint_url: str, owner: str,
                     signature_b64: str) -> bool: ...
    # Pure crypto + nonce lifecycle. Consumes the nonce ONLY on success.
```

Chain I/O (`resolve_owner`) is deliberately separate from crypto
(`verify_challenge`) so the hermetic suite — which forces
`settings.stellar_agent_registry = ""` (`tests/conftest.py:44-47`) — can test
each without the other. `resolve_owner` reads that setting **live** rather than
through the `lru_cache`d `sc.contract_ids()`, for the reason
`app/services/registry_sync.py:18-22` already records.

### `app/routers/binding.py` (new) — `APIRouter(prefix="/agents", tags=["binding"])`

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/agents/{agent_id}/bind/challenge` | public | mint a nonce + the exact message to sign |
| `POST /api/agents/{agent_id}/bind` | public | verify and store |
| `GET /api/agents/bind/endpoint-check?url=` | public | pure advisory preflight |
| `GET /api/agents/{agent_id}/binding` | public (host only) / `X-API-Key` (full URL) | read back — AC-6 is only observable through this |

**Both write routes are public, deliberately.** `require_api_key` guards exactly
the routes where *the backend spends its own key* (`/server/charge`,
`/server/seal`, the PDAX `secured` sub-router) — meanwhile `build/register-agent`
and `submit` are public *writes*, because there the wallet is the credential.
Binding is the same shape. Gating it would also be a no-op on the demo
(`require_api_key` does nothing while `API_KEY` is unset) while locking out every
external operator in production — and it contradicts the module's founding
premise: binding "must cost nothing but a signature from the wallet that owns
the agent". A shared secret cannot express "this caller owns *this* agent".

`GET /api/agents/bind/endpoint-check` is **not** `/api/agents/endpoint-check`,
because `GET /agents/{agent_id}` (`app/routers/agents.py:14`) has no pattern
constraint and would swallow it as an agent id. The three-segment form cannot
collide regardless of router registration order.

### Error codes

`detail` is always a bare snake_case token; `app/main.py:268-289` promotes it to
`error.code`. Anything else silently degrades to the HTTP phrase.

| Condition | Status | Code |
|---|---|---|
| agent id not on-chain | 404 | `agent_not_found` *(reused, not a synonym)* |
| signature fails against the on-chain owner | 401 | `not_agent_owner` |
| nonce missing / consumed / expired | 401 | `not_agent_owner` *(collapsed — see below)* |
| endpoint fails the SSRF policy | 422 | `endpoint_not_allowed` |
| signature not base64 / wrong length | 422 | `signature_malformed` |
| chain unreadable | 503 | `registry_unavailable` |
| no binding for this agent | 404 | `binding_not_found` |

One code for all three nonce failures is deliberate, following
`require_task_read` (`app/services/task_auth.py:47-50`): splitting
`nonce_expired` from `nonce_replayed` tells an attacker which half of a captured
signature is still live.

**Amended during implementation:** `challenge_invalid` was dropped entirely and
folded into `not_agent_owner`. The original table split "no live challenge"
from "signature does not verify", but `verify_challenge` returns a single
`bool` by design — the two are indistinguishable to the router, and making them
distinguishable would have meant *adding* an oracle. That is the same leak the
paragraph above refuses, one level up: a caller replaying a captured signature
learns nothing about whether the nonce or the key was the reason it failed.
`signature_malformed` (422) survives as a separate code because it is decided
by a pure base64 decode in the router, before any secret is consulted, so it
reveals nothing — it is a client bug, not a failed authentication.

### Handler order in `POST .../bind` — this ordering *is* AC-2 and AC-4

1. `validate_endpoint_url` → 422. **Before any chain read, nonce lookup, or
   write**, so a blocked URL provably never reaches the network and provably
   stores nothing.
2. `resolve_owner` → 404 `agent_not_found` / 503 `registry_unavailable`.
3. `verify_challenge` → 401. Nonce consumed only on success.
4. `resolve_and_check` (DNS) → 422. After authorization, so it is not a free
   unauthenticated resolver.
5. `store.put` → 200.

### Logging

Never log the nonce and never log the signature — the nonce is a live
single-use credential and the signature is exactly the "signature material"
`app/routers/stellar.py:529-531` refuses to record. Refusals log the **host and
rule**, not the attacker-controlled full URL; only the accept path logs the URL.

## D5 — Making the binding actually route (added after the first pass)

Originally deferred, then closed inside 2.01 because AC-5's wording is "my
binding should still be in effect **and my agent should still be dispatchable**"
— persistence alone does not meet it.

`app/services/binding_registry.py` answers the orchestrator's two different
questions with two different mechanisms, which is the whole design:

- **`resolve_worker` (async) — dispatch.** Reads the binding store, the source
  of truth, so a rebind takes effect on the next step rather than whenever a
  cache expires. Used by `execution_svc._run`.
- **`is_dispatchable` (sync) — planning.** `orchestrator_svc` filters candidate
  agents inside list comprehensions and cannot await. This is the part that
  nearly got missed: wiring only the dispatch path would have left a bound
  agent dispatchable and **never selected**, because three separate sites gated
  routability on `get_worker(...) is not None`.

Both fail **open**, unlike `resolve_owner`. Nothing here is an authorization
decision — ownership was proved at bind time — so an unreadable store means a
skipped step and a degraded workflow, never an escalation.

The synchronous set is seeded at startup and added to on each bind. That is
sufficient only because `render.yaml` pins `--workers 1`; a multi-worker
deployment needs a periodic refresh, or a bind served by one worker stays
unroutable on another. Stated in the module, and repeated here because it is
the kind of assumption that outlives the comment recording it.

**Not changed, deliberately:** a binding does not exempt an agent from the
reputation floor. `fetch_reps` returns a `RepInfo` for every agent in state —
on both its success and its timeout path — so a cold-start external agent is
judged by the same arithmetic as a cold-start local one, and `passes_floor`'s
`None` branch is unreachable from the real path. Two consequences were accepted
rather than decided silently: a bound agent counts toward
`_MIN_ROUTABLE_AGENTS` and can therefore suppress the floor-starvation
fallback, and that fallback's sort key still falls back to self-declared
`Agent.rep` for any agent missing from `reps` (unreachable in production, but
now guarding a wider set). Changing either is a reputation-policy decision, not
a routability one.

> **Amended 2026-09-18.** The second consequence no longer holds. The
> free-form starvation fallback now tops the shortlist up rather than replacing
> it — every agent that clears the floor stays offered, and only the shortfall
> below `_MIN_ROUTABLE_AGENTS` is filled from sub-floor agents — and both
> backstops rank that shortfall with one helper, `_backstop_rank`
> (`app/services/orchestrator_svc.py`), which orders by smoothed score and
> counts a missing reputation entry as 0. Neither backstop consults self-declared
> `Agent.rep` any more: an operator-written number was never evidence, and a key
> that rewards it is a key an operator can set. (It survives in two places that
> only matter for an agent with no reputation entry at all, which `fetch_reps`
> never produces: the kit substitute's sort key and the `rep=` display in
> AVAILABLE_AGENTS.) The first consequence
> stands — a bound agent that clears the floor still counts toward the minimum
> and can keep the backstop from firing.

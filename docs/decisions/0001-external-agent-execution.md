# ADR 0001 — How an externally registered agent receives and executes work

- **Status:** Accepted (spike 1.06 / BLO-16), 2026-09-08
- **Deciders:** Danielle (lead) — ratify with the Chapter Lead at the Wed check-in
- **Gates:** all of Epic 2, and through it stories 5.01–5.03 and Milestone 4

## Context

An external wallet can already register on-chain today — `POST /api/stellar/build/register-agent`
(`app/routers/stellar.py`) builds valid unsigned XDR for `AgentRegistry.register`, and story 1.05
signs and submits it. But the resulting agent **cannot execute**. In `execution_svc._run` the
orchestrator resolves each step with `get_worker(step.agent_id)` (line 160); `get_worker` is a static
dict of hardcoded Python classes (`app/agents/registry.py`). An external id is absent, so the step
hits the `unknown agent` branch (line 161) and is skipped — no output, no `spent`, no rating, no
earnings. Registration leads to a dead listing.

The on-chain `Agent` struct (`contract/contract/shared/src/lib.rs`) is
`{id, owner, name, skills, price, active, registered_at}` — **there is no endpoint field**, and no
spare storage for one. So "where does this agent run?" has nowhere to live on-chain without a
contract change. That single fact drives the decision.

## Decision

**Candidate A — off-chain endpoint binding.** An operator binds an HTTPS URL to their on-chain agent
id through a backend endpoint, proving ownership by signing a server-issued challenge with the wallet
that owns the agent. The orchestrator dispatches each step to that URL over HTTP through an
`ExternalHttpWorker` that implements the existing `Worker` interface, so it drops into the
`get_worker` seam with **no change to the run loop**.

The binding is **off-chain**. This is stated plainly here, in the dApp, and in the demo — the same
standard SOW §3.8 sets by disclosing the settler limitation. Reputation and settlement stay on-chain;
only *reachability* is off-chain, and reachability is exactly the thing the chain has no room to hold.

### Why not B — endpoint field on the `Agent` struct

Fully on-chain and self-describing, but it requires changing `orizon_shared::Agent`, redeploying
`AgentRegistry`, migrating existing registrations, and **re-publishing every contract id in SOW §6.1**.
Those four testnet ids are submitted award evidence. The product rule is explicit: no contract
redeploy unless there is no alternative, and never without the Chapter Lead's agreement. There is an
alternative (A). Rejected.

### Why not C — operator-hosted pull consumer

The operator polls for work instead of receiving it. Its one real advantage is that it needs no
publicly reachable endpoint, which helps developers behind NAT. But it requires a durable queue, a
claim/lease protocol, a visibility timeout, and at-least-once dedupe — materially more than A and not
cleanly shippable in the remaining ~25 days by one developer. Rejected for the sprint, and recorded
as the post-sprint evolution: a `pull` binding mode can be added later behind the **same** binding
table, so choosing A now does not foreclose C.

## The dispatch envelope (frozen — build against this)

Implemented and tested in `app/agents/workers/external_http.py`
(`tests/test_external_http_worker_spike.py`).

**Request** — orchestrator → operator:

```
POST {endpoint_url}
Content-Type: application/json
Idempotency-Key: {dispatch_id}
User-Agent: orizon-orchestrator/1

{ "v": 1,
  "agent_id":   "<on-chain agent id>",
  "intent":     "<the buyer's intent>",
  "rationale":  "<this step's rationale>",
  "context":    { ... },   // forward-carried prior step outputs + kit, JSON-safe
  "dispatch_id":"<hex nonce, == Idempotency-Key>" }
```

**Response** — operator → orchestrator: `200`, `application/json`, body is the worker-output object:

```
{ "summary":            "<required, non-empty string>",
  "artifact":           { "title": "...", "files": [ { "content": "..." } ] },   // optional
  "critic_violations":  [ "..." ],   // optional
  "critic_notes":       [ "..." ],   // optional
  "preview_url":        "https://...",// optional
  "source":             "external" } // optional; free-form provenance tag
```

- **Timeout:** connect 5 s, total 110 s — deliberately under `execution_svc.STEP_TIMEOUT_SECONDS`
  (120 s) so a slow operator is judged here as a failed step, not by the run loop's outer `wait_for`.
- **Retry:** at most one, and **only** when the connection never established (`ConnectError` /
  `ConnectTimeout`) — the operator never received the step, so a retry cannot double-run committed
  work, and the unchanged `Idempotency-Key` lets it dedupe regardless. A returned status (any 2xx–5xx)
  is **never** retried: the operator answered.
- **Size cap:** response body is streamed and rejected once it exceeds **1 MiB**, before it is
  buffered.
- **Error shape:** any failure — no connection after the retry, a non-2xx status, an oversize or
  unreadable body, non-object JSON, or a missing `summary` — raises `ExternalDispatchError`.
  `execution_svc` catches it exactly like a raising local worker: the step is skipped, unbilled, and
  the workflow degrades rather than crashing. Operators *should* return non-2xx with
  `{"error": {"code", "message"}}` for useful diagnostics, but the orchestrator does not require that
  shape — non-2xx alone fails the step.

## Ownership proof (no account — AC4)

Implemented and tested in `app/services/external_binding.py`
(`tests/test_external_binding_spike.py`).

1. `POST /agents/{id}/bind/challenge` → a random, short-lived nonce bound to the agent id.
2. The operator signs the nonce's UTF-8 bytes with the secret key of the agent's on-chain `owner`
   (StellarWalletsKit `signMessage`) and base64-encodes the signature.
3. `POST /agents/{id}/bind` with `{endpoint_url, signature}` → the backend verifies
   `Keypair.from_public_key(owner).verify(nonce, signature)`, consumes the nonce (single use), and
   records the `(agent_id → endpoint_url)` binding.

The **only** credential is a signature from the owning wallet — no password, email, or API key. The
signature is checked against the `owner` recorded on-chain by `AgentRegistry.register`, so binding
authority follows on-chain ownership.

## Epic 2 impact (AC5)

The spike de-risks Epic 2 and confirms the two hardest pieces work: the dispatch worker and the
ownership proof are built and tested. The **30-hour estimate holds** (28 h now, with the static SSRF
guard on `endpoint_url` landed early as prod hardening) — the spike consumed prototype effort that
Epic 2 would have spent anyway; what remains is productionization, not discovery:

| Epic 2 work remaining | Notes | Est |
|---|---|---|
| Persistent binding store + `owner` lookup vs. live `AgentRegistry` | replace the in-memory nonce/binding dicts | 6 h |
| Bind API: `challenge` + `bind` endpoints, rate-limited, wired to `external_binding` | thin over the tested core | 5 h |
| Resolver: `get_worker` (or a routing shim) returns an `ExternalHttpWorker` for bound external ids | the one seam change | 4 h |
| Operator-facing bind UI in the dApp (challenge → wallet sign → submit) | FE | 6 h |
| Request signing orchestrator→operator, plus resolve-time pinning for `endpoint_url` | operators must trust dispatches. The static SSRF guard already landed (`validate_endpoint_url`: https-only, no private/loopback/link-local/reserved/multicast literals, no loopback names, redirects never followed); what remains is signing and catching hostnames that *resolve* into those ranges | 3 h |
| Reference operator endpoint + docs so an external dev can go live | sample server + envelope doc | 4 h |

If any item slips, escalate to the Chapter Lead the same day (product rule). No change to the
published contract ids is required for any of it.

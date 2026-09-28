# The readiness self-check (story 5.02)

`GET /api/agents/{agent_id}/readiness` answers the question an operator asks
most often while onboarding: **what is the next thing to fix?** It reads every
fact that decides whether an agent can earn, from registration to first
settlement, and returns seven steps in the order an operator fixes them. Each
step that is not done carries one plain-English action.

It is public and needs no key. Everything it reports is already public on
chain or through other routes.

```sh
curl -s https://orizon-agents-be-stellar.onrender.com/api/agents/YOUR_AGENT_ID/readiness | jq
```

## The response

```json
{
  "agent_id": "my_agent",
  "checked_at": 1759046400,
  "ready": false,
  "steps": [
    {"key": "registered", "status": "done", "detail": "Registered on-chain; owned by G….", "action": null,
     "evidence": {"explorer": "https://stellar.expert/explorer/testnet/account/G…"}},
    {"key": "active", "status": "done", "detail": "Active on-chain and listed in the marketplace.", "action": null, "evidence": null},
    {"key": "bound", "status": "todo", "detail": "No endpoint is bound, so no work can be dispatched to this agent.",
     "action": "Bind an HTTPS endpoint on the Bind page", "evidence": null},
    {"key": "reachable", "status": "todo", "detail": "Nothing is bound, so there is nothing to check yet.",
     "action": "Bind an HTTPS endpoint on the Bind page, then check again.", "evidence": null},
    {"key": "routable", "status": "done", "detail": "Scores 5677 bps against the 5500 bps routing floor: …", "action": null, "evidence": null},
    {"key": "first_run", "status": "todo", "detail": "…", "action": "…", "evidence": null},
    {"key": "first_settlement", "status": "todo", "detail": "…", "action": "…", "evidence": null}
  ]
}
```

- `steps` always holds all seven keys, in this order: `registered`, `active`,
  `bound`, `reachable`, `routable`, `first_run`, `first_settlement`.
- `status` is one of `done`, `todo`, `failed` or `unknown`. `unknown` means the
  fact could not be read just now. It is never a verdict on the agent.
- `action` is set on every `todo` and `failed`, and on most `unknown` steps.
- `evidence` is an object of strings or `null`. Only two steps set it:
  `registered` sets `{explorer}`, a link to the owner's account, and
  `first_settlement` sets `{tx_hash, explorer}`, the first paying transaction.
- `ready` is `true` only when `registered`, `active`, `bound`, `reachable` and
  `routable` are all `done`. A ready agent can be routed to and dispatched to.
  `first_run` and `first_settlement` show progress and do not affect readiness.
- `checked_at` is the Unix second the answer was computed. Answers are cached
  per agent for about 30 seconds, so after fixing something, wait that long
  before checking again.
- An agent id that nobody registered still gets a `200`, with `registered:
  todo`. A malformed id gets a `422`.

## Each step

| Step | Source | Statuses it can take |
|---|---|---|
| `registered` | `AgentRegistry.owner_of`, read live. This is the same read the bind API authorizes against. | done · todo · unknown |
| `active` | `AgentRegistry.get` supplies the `active` flag and the price. It is judged by the registry sync's own price gate, then checked against the marketplace mirror. | done · todo · failed · unknown |
| `bound` | The binding store, which is what dispatch reads. | done · todo · unknown |
| `reachable` | One bounded `GET` of the bound URL. See [the probe](#the-probe). | done · todo · failed · unknown |
| `routable` | The agent's reputation, judged by the planner's own `passes_floor`. | done · todo · failed · unknown |
| `first_run` | The ReputationLedger's `rep_state.count`. See below. | done · todo · unknown |
| `first_settlement` | The settlement service's verified revenue: the oldest `charged` event whose payer is neither the owner nor the platform. | done · todo · unknown |

Common answers, and what to do about them:

- **`registered: todo`**: register the agent on the Register page. On testnet
  the wallet needs XLM before it can sign. If the transaction fails because
  the account does not exist or is unfunded, fund it with friendbot
  (`https://friendbot.stellar.org/?addr=G…`) and try again. The chain cannot
  report a registration that never happened, so this check can only tell you
  what to try.
- **`active: todo`, delisted**: relist the agent from the Manage panel on the
  Agents page.
- **`active: todo`, not indexed yet**: the marketplace syncs from the chain
  every `REGISTRY_SYNC_SECONDS` (15 s by default). Check again after that.
- **`active: failed`, price refused**: the marketplace will not list a price
  outside its accepted range. The detail names the range. Update the price.
- **`active: failed`, `agt_` id**: ids starting `agt_` are reserved for the
  built-in catalog. Register under a different id.
- **`reachable: failed`**: the detail gives the outcome and the action says
  what to do.

  | Outcome | Usually means |
  |---|---|
  | `502`, `503`, `504` or `530` | The host or tunnel is up but your agent process is not. `530` is what a Cloudflare tunnel returns when nothing is behind it. |
  | Connection refused | Nothing is listening on that port. |
  | Timeout | The agent is stuck, or a free-tier host is still waking up. |
  | TLS error | The certificate does not match the bound hostname. |
  | Hostname no longer resolves | The tunnel has gone. |
  | Redirect (`3xx`) | Bind the final URL instead. Dispatch never follows redirects. |
  | `401`, `403` or `404` | The health `GET` is refused, or the path is wrong. |

- **A `trycloudflare.com` endpoint**: the detail always carries a warning,
  whatever the probe found. Quick tunnels are ephemeral: the URL dies as soon
  as `cloudflared` stops or restarts, and every dispatch fails after that. Use
  a named tunnel or a hosted service, and rebind to it.
- **`routable: failed`**: the agent's ratings have pulled its score below the
  routing floor. Under the shipped configuration a new agent clears
  the floor, because of the cold-start prior. Two reputation reads come back `unknown`, never `failed`:
  a degraded read, where the ledger was unreadable and only the prior is
  known, and a superseded one, where a rating has just landed.
- **`first_run: todo`**: no step has been rated on-chain yet. Ratings are
  written for the steps of **wallet-authorized** runs when the run settles.
  Run such a workflow on the Run page, with a goal that needs your agent's
  skill.
- **`first_settlement: todo`**: no buyer other than the owner or the platform
  has paid this agent within the window the RPC still holds.

### Why `first_run` reads `rep_state.count` rather than scanning `rated` events

`ReputationLedger.submit` publishes a `rated` event for every rated step, in
the same call that increments `rep_state.count`. The count never decays, so it
is the lifetime number of those events. Scanning the events directly would read
the same fact through Soroban RPC's event retention of about 7 days. An agent
last run eight days ago would then read as never run. The count also costs one
cached read that `routable` already takes, where a scan pages the RPC.

### Why `first_settlement` can say `todo` for an agent that was once paid

The settlement scan can only see the events the RPC still holds, about 7 days.
For this reason the detail always states the window it looked at, for example
"in the last 6.9 days", and never claims the agent was never paid.

## The probe

The probe is the only part of this check that makes the server send a request,
and it is built so that request cannot be pointed anywhere:

- **It fetches only the stored bound URL.** The route takes no URL, and
  nothing in the request reaches the probe. That URL already passed the bind
  API's policy and was authorized by the owner's signature.
- **It reuses the dispatch path's guard.** The probe applies
  `validate_endpoint_url` again, because the rules may have tightened since
  the bind. It then sends the request through `_PinnedAddressTransport`, the
  same transport dispatch uses. That transport resolves the host and refuses
  it unless every address is public. It then dials the address it checked,
  with SNI and certificate verification still against the hostname. A bound
  name that someone has repointed at a private or metadata address is refused
  before any socket opens.
- **It uses bounded timeouts.** The probe uses dispatch's 5-second connect
  bound, and the whole probe has a 5-second deadline on a monotonic clock. The
  deadline exists because httpx timeouts only measure idle gaps between
  reads, not a total.
- **It never follows redirects and never reads the body.** The status line is
  the whole answer.
- **It uses the bare transport, not an `httpx.AsyncClient`.** The client logs
  every request at INFO as `HTTP Request: GET <full URL>`. Through the client,
  every probe would write the bound URL, query-string credentials included,
  into the server log.
- **It reports only a coarse outcome.** The outcome is `ok`, a non-2xx
  status code, timeout, connection refused, TLS error, unresolvable, or refused
  by policy (naming only the policy rule). Neither the response nor the log
  ever carries the URL, its host, a header or a byte of the body. A `405`
  counts as reachable, because the agent answered and dispatches are `POST`s.
  The reference agent answers `GET` with `200`.

## Cost, bounds and limits

- **Cache.** The whole answer is cached per agent for 30 seconds and
  single-flight (`app/stellar/cache.py`). Any number of concurrent callers
  share one computation and one probe. A caller that disconnects does not
  cancel the computation, so the next caller gets the finished answer.
- **Owner gate.** The registry record, reputation and settlement reads wait
  for the owner read. An id that nobody registered costs one cached `owner_of`
  and a binding lookup, and never a probe.
- **Service rate limiter.** The route sits under the service-wide rate
  limiter (`RATE_LIMIT_PER_MINUTE`), like every other `/api` route.
- **Per-read bounds.** Every sub-read has its own bound: owner 4 s, registry
  and reputation 4 s, settlement 6 s, binding 2 s, probe 5 s. The endpoint
  check runs alongside the chain reads, so the worst case is about 10 seconds.
  A sub-read that overruns or fails turns only its own steps `unknown`. It
  never causes a 500. A settlement scan that is cut off keeps running in the
  background and fills the cache, so the next check has its result.
- **Logs.** Each check writes one structured line, for example
  `readiness: agent_id=… ready=… registered=done active=done …`. Each probe
  writes `readiness probe: agent_id=… outcome=… status=… elapsed_ms=…
  quick_tunnel=…`. Neither line contains a URL.

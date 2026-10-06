import base64
import binascii
import logging
import math

from pydantic import ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .pdax_environments import BASE_URLS as PDAX_BASE_URLS
from .pdax_environments import moves_real_value as pdax_moves_real_value

logger = logging.getLogger(__name__)

MAINNET_PASSPHRASE = "Public Global Stellar Network ; September 2015"

# Every spelling of STELLAR_NETWORK that names the public network: ours, and
# `public`/`pubnet` as Horizon and Stellar Expert say it. Read through
# `label_names_mainnet`, which ignores case and padding, so no two readers of
# the label can disagree about what it says. The label never decides whether
# money is real — `Settings.is_mainnet` does, from the passphrase — it only
# lets the boot refuse a label that promises mainnet over a testnet signer.
MAINNET_LABELS = frozenset({"mainnet", "public", "pubnet"})


def label_names_mainnet(label: str) -> bool:
    """True when STELLAR_NETWORK spells the public network, in any case or padding."""
    return label.strip().lower() in MAINNET_LABELS


# The PDAX environments that resolve to a base URL, read from the shared table
# in app/pdax_environments.py. That module is dependency-free and lives outside
# the app.pdax package precisely so this one can import it while Settings() is
# still being constructed — so there is one source of truth and no copy to drift.
PDAX_ENVIRONMENTS = tuple(PDAX_BASE_URLS)

# Advertised service version — the FastAPI app's `version` and the liveness
# payload both read it here so the number they report can never disagree.
SERVICE_VERSION = "0.1.0"

# The largest share of the decompose planning budget that one batched
# reputation read is allowed to claim. See the validator that uses it,
# _reputation_read_fits_the_planning_budget, for why 10% and why a share
# rather than a fixed number of seconds.
REPUTATION_READ_BUDGET_SHARE = 0.10
# How far past that share a batch bound may sit and still count as AT it —
# relative, so it scales with the budget. The ceiling is a binary float product
# (decompose timeout × 0.10) and neither 0.1 nor most typed decimals are exact in
# binary, so a bound typed at exactly 10% — 0.07 against 0.7, 5.6 against 56 —
# can land a few ulps above the product and be refused for a value the rule
# permits. One part in a billion clears that noise by six orders of magnitude
# and is nanoseconds on any real budget, so it admits no bound anyone would type.
REPUTATION_READ_BUDGET_TOLERANCE = 1e-9

# Shortest API_KEY this service will boot with. Tied to
# `security._MIN_MASKED_SECRET_CHARS`, and it must never drop below it: under
# that length the redaction filter stops masking a configured value by exact
# match, so a shorter key is one that prints itself into the logs. Kept as a
# literal rather than imported, because app/security.py imports THIS module —
# `tests/test_config_validators.py` asserts the two agree, which is the check
# an import would have been for.
_MIN_API_KEY_CHARS = 8


def _seconds(value: float) -> str:
    """A duration as a boot error prints it: precise enough to copy back in.

    `:g` keeps six significant digits, so it prints a ceiling of 12.3456789 s
    as 12.3457 — above the ceiling it names — and a validator whose advice is
    formatted that way refuses its own suggested fix. Twelve digits bound the
    rounding at 5e-12 relative, far inside REPUTATION_READ_BUDGET_TOLERANCE,
    while still hiding binary float noise: 0.7 × 0.1 prints as 0.07, not as
    the 0.06999999999999999 it is stored as.
    """
    return f"{value:.12g}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── OpenAI / Agno ─────────────────────────────────────────
    openai_api_key: str = ""
    orchestrator_model: str = "gpt-4o-mini"
    worker_model: str = "gpt-4o-mini"
    # Per-request HTTP timeout handed to the OpenAI client (its own default
    # is 600 s per attempt — far too long for an interactive API).
    llm_timeout_seconds: float = 120.0
    # End-to-end budget for one decompose LLM call (asyncio.wait_for bound;
    # the router maps a breach to HTTP 504 "decompose_timeout").
    decompose_timeout_seconds: float = 90.0

    # ── Claude + jev (orchestrator v2, app/llm/) ──────────────
    # Which stack plans and runs the built-in workers: "anthropic", "openai",
    # or empty for automatic — Claude once ANTHROPIC_API_KEY is set, OpenAI
    # until then. The OpenAI path stays behind this switch until Claude is
    # proven live; app/llm/provider.py is the one reader.
    orchestrator_provider: str = ""
    # Set on Render at release, never in render.yaml (the dashboard overrides
    # it, and the file is in git). Empty keeps every Claude and jev call off:
    # the guard falls back as documented and planning reports unavailable.
    anthropic_api_key: str = ""
    typesafe_api_key: str = ""
    # The jev model the guard asks, pinned so a new release cannot move the
    # guard's thresholds under it (TYPESAFE_MODEL).
    typesafe_model: str = "jev-1.13.0"
    # One model per tier, exact Claude ids. The planner runs on the complex
    # tier's model, the prompt improver on the moderate one and the fallback
    # guard on the low one (app/llm/tiers.py).
    claude_model_low: str = "claude-haiku-4-5"
    claude_model_moderate: str = "claude-sonnet-5-5"
    claude_model_complex: str = "claude-opus-5-5"

    # ── Code-generation quality dials (code.gen + code.critic) ─
    # Higher reasoning = better artifacts, more latency + cost.
    # Valid: "low" | "medium" | "high" | "xhigh".
    code_reasoning_effort: str = "high"
    code_temperature: float = 0.3

    # ── HTTP / CORS ───────────────────────────────────────────
    cors_origins: str = "http://localhost:3000"
    port: int = 8000
    # Serve the interactive API docs (/docs, /redoc, /openapi.json). ON by
    # default — the public demo advertises them; flip off to run dark.
    docs_enabled: bool = True

    # ── Hardening ─────────────────────────────────────────────
    # Shared secret for the money-moving routes. Empty (the demo default)
    # disables the check; set it to require X-API-Key. It stops being
    # optional the moment the process holds credentials that can move real
    # value — see _money_capable_config_requires_api_key below.
    api_key: str = ""
    # Capability-token authorization for task-scoped reads. OFF by default —
    # the public demo stays fully open. When enabled, GET task/trace routes
    # require the per-task read token minted at execute (or a valid API key).
    task_auth_required: bool = False
    # Sliding-window request budget (in-process, per worker), spent per key
    # resolved by app/security.py client_identity(); requests it cannot
    # attribute share one bucket of the same size.
    #
    # Sized as a WHOLE-SERVICE budget, because that is what it was until the
    # identity existed: the limiter keyed on the last forwarded entry, which
    # on Render is an address from the platform's own pool, so every visitor
    # drew on a couple of buckets. It is now per visitor and could come down;
    # it stays a coarse flood cut until production traffic says where. The console's
    # real cost drives the number — an open dashboard tab polls two endpoints
    # every 5 s (24 req/min), /app/reputation adds a 4-request burst on mount
    # and 3 more on every window focus, and both liveness probes are exempt
    # (EXEMPT_PATHS), so a tab costs ~24/min steady with small bursts on
    # navigation. 1200/min therefore seats ~50 concurrent tabs before anyone
    # is throttled, against the 5 that 120/min allowed — a demo that gets
    # linked somewhere stays usable instead of 429ing its own visitors.
    #
    # It still bounds abuse: 20 req/s is a coarse flood cut, and it is not the
    # cost control for the expensive routes — orchestrator_max_concurrent caps
    # in-flight workflows (503 capacity_exhausted), every free-form /decompose
    # is one LLM call with its own per-client budget, concurrency gate and
    # bounded wait queue (the planner-spend block below), and contract reads are
    # TTL-cached per key. A cache only absorbs a flood of the SAME key, though:
    # a flood of distinct keys is a distinct upstream read each, so a read
    # route whose key a caller chooses must bound the key space itself —
    # /api/stellar/reputation/{agent_id} answers only registered ids, and 404s
    # the rest before any RPC. This limit alone does not make a flood cheap.
    # The frontend backs off on 429 and honours Retry-After, so a tightened
    # limit degrades cadence rather than breaking the console.
    rate_limit_per_minute: int = 1200
    # How many TRAILING X-Forwarded-For entries the ACCESS LOG's `client=`
    # drops (app/security.py client_key). The rate limiters no longer read it:
    # they key on client_identity(), which recognises Render's own hops by
    # address (non-public, then one Cloudflare edge) and so needs no count —
    # 0, the default, is correct for this deployment, and no stale dashboard
    # value can make a limiter read an entry the caller wrote. For the log,
    # too low names our own edge and too high names whatever the caller wrote;
    # the chain samples below print both views side by side.
    trusted_proxy_hops: int = 0
    # Diagnostic budget for that tuning: log the raw X-Forwarded-For chain and
    # the key it resolves to for the first N non-exempt requests after each
    # process start, then stay silent for the life of the process. 0 disables
    # it. Deliberately a bounded log sample rather than a diagnostic route —
    # the chain carries visitors' IP addresses, and API_KEY is empty on demo
    # deployments so a route could not be reliably gated. Changing any env var
    # on Render redeploys the service, which re-arms the sample.
    forwarded_chain_samples: int = 5
    # Shared secret our own frontend's server-side route handlers send as the
    # X-Frontend-Proxy-Token header. Empty — the default — trusts nobody. With
    # it set, a request carrying it (compared in constant time, never logged)
    # is our frontend: its X-Orizon-Client-Ip header, the visitor's address as
    # Vercel saw it, becomes the rate-limit client, so visitors behind
    # Vercel's shared egress addresses keep budgets of their own; without that
    # header (a cached, shared read) no per-client budget applies at all. See
    # `security.client_identity`. At least 32 characters, random.
    frontend_proxy_token: str = ""
    # /api/stellar/server/seal makes the platform's sealer sign an attestation
    # with whatever the caller sends, so it FAILS CLOSED while API_KEY is
    # empty. A local or CI testnet run that wants it open without a key sets
    # this to true, deliberately; never on a deployment.
    allow_keyless_server_seal: bool = False
    # Ceiling on concurrently running workflows (each fans out LLM calls).
    # execute returns 503 "capacity_exhausted" once this many are in flight.
    orchestrator_max_concurrent: int = 8
    # Ceiling on concurrent free-form decompose planning calls (each is a
    # real LLM call; the demo-kit path makes none and is never gated).
    # Up to decompose_max_queued more wait for a slot, inside
    # decompose_timeout_seconds (504 "decompose_timeout" if the wait runs
    # out); past that, a request is refused at once with 503 "planner_busy".
    decompose_max_concurrent: int = 8
    # Ceiling for a single PaymentEscrow.charge, in USDC.
    max_charge_usdc: float = 100.0

    # ── Persistence (story 2.01 — operator endpoint binding) ──
    # Postgres DSN for the operator endpoint binding table — the first durable
    # write this service has ever made. EMPTY (the default) selects
    # InMemoryBindingStore instead, which is what keeps the test suite hermetic
    # and offline and what lets local dev run with no database at all: the whole
    # choice is made from this one value, behind the BindingStore interface, with
    # no code change on either side.
    #
    # It is set in the RENDER DASHBOARD, not here and not in render.yaml. A DSN
    # embeds the database password, render.yaml is in git, and every secret it
    # names is `sync: false` for exactly that reason. The dashboard also
    # overrides render.yaml, so the dashboard is where a deployment's real value
    # has to live regardless of which file this default sits in.
    #
    # This one variable is what makes AC-5 ("the binding survives a backend
    # restart") true in production. The free instance spins down after ~15
    # minutes idle and comes back FROM THE IMAGE, so a binding held in this
    # process — or written to this process's filesystem — is gone before anyone
    # looks at it. Pointed at a Neon DSN the binding outlives the restart, the
    # redeploy and the demo; see docs/decisions/0003-operator-endpoint-binding.md
    # for why Neon rather than Render's own free Postgres, which expires after 30
    # days, i.e. exactly at award time.
    database_url: str = ""

    # ── External dispatch (story 2.02 — envelope signing) ────
    # S… secret (or a 12/24-word mnemonic) used to sign the SEP-53 envelope on
    # every outbound dispatch, so an operator receiving a POST from us can prove
    # it is ours. See docs/decisions/0004-external-dispatch-hardening.md D2.
    #
    # This is deliberately NOT stellar_signing_key. Reusing that key is
    # cryptographically safe — SEP-53's "Stellar Signed Message:\n" prefix cannot
    # collide with a transaction or Soroban-auth preimage, both domain-separated
    # by network id — but it is wrong operationally, for three reasons:
    #   - stellar_signing_key is the settler/sealer/scorer key: it is what signs
    #     PaymentEscrow.charge and AttestationRegistry.seal, i.e. it moves money.
    #     Rotating the dispatch key after an operator integration goes wrong
    #     would then mean re-granting those on-chain roles and redeploying
    #     PaymentEscrow — key rotation welded to a contract migration.
    #   - it would put the settler seed on the outbound HTTP hot path, in a
    #     module that talks to third-party URLs, for no benefit.
    #   - the deployments an operator integrates against first are the demo and
    #     read-only ones, which legitimately have no signing key at all, so
    #     dispatch would be unsigned in exactly the place it needs proving.
    #
    # EMPTY (the default) means UNSIGNED dispatch, never a failure: an unsigned
    # dispatch is not our vulnerability but a trust gap the operator is
    # positioned to close by rejecting it, and failing closed would convert
    # their policy into our outage. app/services/dispatch_signing.py degrades to
    # no signature headers and logs one coalesced warning; the hermetic suite
    # runs with no key at all. A malformed value degrades the same way.
    orizon_dispatch_signing_key: str = ""

    # ── Reputation (Bayesian smoothing + routing floor) ───────
    # The on-chain ReputationLedger stores decayed, value-weighted rating
    # evidence; the backend smooths it with a Bayesian prior so new agents
    # start at a meaningful score instead of zero (cold-start), and gates
    # routing on a conservative lower bound (see services/reputation_svc.py).
    reputation_enabled: bool = True
    # Prior mean, in bps of the 0–100 rating scale (7000 = a 3.5/5 score).
    reputation_prior_bps: int = 7000
    # Evidence mass of the prior, in USDC — how much settled work it takes
    # for on-chain evidence to dominate the prior. Sized so a prior-only
    # agent clears the default floor on the lower bound (see tests).
    reputation_prior_weight_usdc: float = 12.0
    # Routing floor applied to the smoothed lower bound at decompose time.
    reputation_floor_bps: int = 5500
    # TTL for cached on-chain rep_state reads (per agent).
    reputation_read_ttl_seconds: float = 15.0
    # Wall-clock bound on ONE batched reputation read — the deadline fetch_reps'
    # per-agent reads share under asyncio.wait (services/reputation_svc.py);
    # a read still pending at it is served stale or the prior, and keeps
    # running to fill the cache. Lifted out of that
    # function's default argument so a deployment can tune it without a code
    # change and, more to the point, so the validators below that bound it can
    # see the number they are validating: a bound that exists only as a literal
    # inside a signature is one no validator can check. fetch_reps' default is
    # now None, which it resolves to this setting on every call, so there is one
    # number and nothing to keep in step; tests/test_reputation_budget.py fails
    # if a literal default ever returns and disagrees with this one. An explicit
    # argument still wins — the degradation tests drive it to 0.02 s to force the
    # timeout path — and no production caller passes one, so the value validated
    # here is the bound every live read uses.
    reputation_batch_timeout_seconds: float = 2.5
    # How long past its TTL an agent's last on-chain read may still be served
    # when a fresh read does not answer in time (the batch deadline passed, or
    # the read failed). Served marked `stale` with its age, and judged by the
    # routing floor on that evidence — instead of the prior, which clears the
    # floor for everyone. Past this, the agent degrades to the prior as before.
    # 0 turns stale serving off.
    reputation_stale_grace_seconds: float = 300.0
    # What the batch deadline above has to cover, as three numbers the
    # validator `_reputation_deadline_covers_the_batch` multiplies out. A batch
    # reads every agent at once, but only `reputation_read_concurrency` reads
    # run at a time (the worker threads reserved for them), so it takes
    # ceil(agents / concurrency) waves of one read each. The live defect this
    # exists for: 23 agents on the shared 8-thread pool is 3 waves, at two RPC
    # hops a read, and 2.5 s broke at 0.83 s a read — 3 of 4 warm reads
    # degraded on a healthy chain.
    #   * concurrency — threads that ONLY reputation reads use, so a registry
    #     sync, the ratings writer or a flood of other reads cannot queue ahead
    #     of the batch a plan is waiting on.
    #   * latency — the time one read is sized at: a single simulate round
    #     trip (the load_account hop is gone), measured at 0.27–0.63 s against
    #     SDF testnet, with room over it.
    #   * agents — how many agents one batch is sized for. The live registry is
    #     23. A batch larger than this still runs, and logs once that the
    #     deadline was not sized for it.
    reputation_read_concurrency: int = 16
    reputation_read_latency_seconds: float = 0.75
    reputation_batch_agents: int = 32
    # Absolute per-rating weight cap in USDC. On its own it did NOT stop one
    # job owning the score: 100 USDC of weight against the prior's 12 meant a
    # single self-dealt run at the ceiling set an agent's reputation (one 95/100
    # took the bound from 5677 to 8980). The ratio below is the cap that
    # actually binds; this one remains as an outer bound.
    reputation_max_rating_weight_usdc: float = 100.0
    # The cap on ONE rating's weight, as a multiple of the prior's weight
    # (REPUTATION_PRIOR_WEIGHT_USDC). At 1.0 a single rating can at most EQUAL
    # the prior — it moves an agent's score at most halfway to itself and never
    # overrules the prior on its own — whatever the step was priced at. The
    # effective cap is min(this x prior weight, REPUTATION_MAX_RATING_WEIGHT_USDC),
    # 12 USDC with the shipped numbers. An OPEN PRODUCT DECISION: lower it to
    # make one job count for less (0.25 = a quarter of the prior); it is one
    # number so it can be changed without touching code. Finite and above zero.
    reputation_max_rating_to_prior_ratio: float = 1.0

    # ── Disputes (story 4.02 — ADR 0002) ──────────────────────
    # How long after a paid workflow settles its buyer may dispute a step.
    # The window a given workflow got is STAMPED ON ITS SETTLEMENT RECORD when
    # it settles, never recomputed from this value — a buyer was told a closing
    # time, and tuning this must not silently move it for work already done.
    # Changing it therefore only affects workflows that settle afterwards.
    dispute_window_seconds: float = 86_400.0  # 24 hours
    # Share of the disputed step's settled charge credited back when a dispute
    # is upheld (story 4.03 pays it). 1.0 = the whole step, which is what ADR
    # 0002 states as the policy buyer and operator are both told in advance.
    # Anything but a finite number in [0, 1] refuses to boot
    # (`_money_bounds_bound_something`); `refund_svc.credited_amount_usdc`
    # still clamps it, for a value set outside `Settings()`.
    dispute_credited_fraction: float = 1.0
    # Hard ceiling on a SINGLE partial-credit refund, checked before anything
    # is signed (story 4.03). Deliberately NOT `max_charge_usdc`: that one
    # bounds what a buyer authorised themselves to spend, while this bounds
    # what the PLATFORM pays out of its own wallet on an adjudicator's say-so,
    # so sharing a number between them would be a coincidence rather than a
    # control. A step settles for hundredths of a USDC on this deployment, so
    # 1.0 is far above anything legitimate and still keeps the blast radius of
    # a mistaken uphold small. It bounds what the refund path will sign, and
    # nothing else: a leaked settler key can transfer without asking it.
    # A ceiling only while it is a finite number above zero — NaN and inf
    # compare as "under the cap" for every amount — so anything else refuses to
    # boot (`_money_bounds_bound_something`, QA D-054).
    max_refund_usdc: float = 1.0
    # The master switch on the refund path (story 4.03). OFF by default, so a
    # deployment only pays out once an operator has deliberately turned it on
    # — and turning it on is what makes API_KEY mandatory below. A money path
    # that is enabled by the mere presence of a signing key would be enabled
    # in every test run and on every developer's laptop, which is how an
    # anonymous payout route reaches production without anyone choosing it.
    dispute_refunds_enabled: bool = False
    # The refund reconcile sweep (services/refund_reconcile.py): a background
    # pass that settles refund claims parked in `crediting` by asking the chain
    # what their in-flight transfer did — records `credited` when it landed,
    # releases the claim when it provably never can. OFF by default, like the
    # refund switch it depends on, and it only runs while that switch is ON
    # too: a released claim makes a dispute payable again, which is a decision
    # about the platform's wallet that belongs to a deployment paying credits.
    # It never signs or submits anything.
    refund_reconcile_enabled: bool = False
    # Seconds between two passes. Bounded both ways (`_refund_reconcile_interval_is_usable`):
    # at least 30, because a pass reads the chain once per held claim; at most
    # 3600, because the RPC keeps only a window of transaction history (about
    # seven days on SDF's testnet RPC, as little as a day on a default one) and
    # a claim must be looked at well inside it — past it, NOT_FOUND no longer
    # means anything and the claim falls back to a human.
    refund_reconcile_interval_seconds: float = 120.0

    # ── Stellar (testnet defaults) ────────────────────────────
    stellar_network: str = "testnet"
    stellar_rpc_url: str = "https://soroban-testnet.stellar.org"
    stellar_network_passphrase: str = "Test SDF Network ; September 2015"

    # Deployed contract IDs — empty until the backend is wired on-chain.
    stellar_agent_registry: str = ""
    stellar_reputation_ledger: str = ""
    stellar_payment_escrow: str = ""
    stellar_attestation_registry: str = ""
    stellar_asset_sac: str = ""

    # Signer
    stellar_admin_address: str = ""
    stellar_signing_key: str = ""  # S... secret — inject via host secrets in prod

    # Registry sync cadence (story 1.02). The background loop mirrors on-chain
    # registrations into the marketplace every N seconds; values under 5 are
    # clamped by the service, and a blank STELLAR_AGENT_REGISTRY disables it.
    registry_sync_seconds: int = 15
    # How long the reputation pre-warm waits for the registry sync's FIRST
    # pass before it reads the registry (app/main.py `_warm_reputation`), so
    # on-chain agents are pre-warmed too and not just the seeded catalog. The
    # wait runs in the BACKGROUND: boot does not wait for it, and requests —
    # /health included — are answered throughout. A pass slower than this is
    # not cancelled: it carries on in the sync loop, the pre-warm goes ahead
    # with a WARNING, and agents indexed later are read on first use. 0 skips
    # the wait.
    registry_boot_sync_timeout_seconds: float = 5.0

    # ── PDAX (PHP ↔ crypto on/off-ramp, institutions API) ─────
    # Env: "production" | "stage" | "uat". Base URL is resolved per
    # environment in app/pdax/config.py.
    pdax_environment: str = "uat"
    pdax_username: str = ""  # PDAX account email
    pdax_password: str = ""  # inject via host secrets in prod
    pdax_otp_secret: str = ""  # TOTP seed if MFA is enabled (optional)
    pdax_webhook_secret: str = ""  # shared secret for webhook validation
    # Escape hatch for local dev/smoke: accept inbound webhooks without a
    # signature when the secret is unset. Fails closed by default and is
    # rejected outright in production (see validator below).
    pdax_allow_unsigned_webhooks: bool = False
    # Resilience tunables (transport retry + client-side rate limiting).
    pdax_max_retries: int = 3
    pdax_rate_limit_per_sec: float = 8.0
    pdax_rate_limit_burst: int = 8
    # Safety buffer added to a fiat-funding quote (basis points) so the pesos
    # paid always cover the workflow after spread, fees, and step rounding.
    pdax_ramp_buffer_bps: int = 300  # 3%
    # PDAX fiat-deposit floor; tiny workflows are funded at this minimum (excess
    # stays as USDC). Reference PHP is what we price off, to clear trade minimums.
    pdax_ramp_min_php: float = 200
    pdax_ramp_quote_reference_php: str = "1000"

    # ── Planner spend (/decompose) ────────────────────────────
    # Every free-form decompose is one real LLM call; the demo-kit path makes
    # none and is never limited here. decompose_max_concurrent bounds how
    # many run at once, and this bounds how many may WAIT for a slot. Waiters
    # used to queue without limit, each holding a connection for up to
    # decompose_timeout_seconds; past this many, a request is refused at once
    # with 503 "planner_busy" instead of joining the queue.
    decompose_max_queued: int = 16
    # Free-form (LLM) decompose calls one client may make per minute, on top
    # of the global rate_limit_per_minute, keyed by security.client_identity()
    # (the visitor behind Render's proxies, or the one our frontend names with
    # FRONTEND_PROXY_TOKEN). A breach is 429 "decompose_rate_limited" with
    # Retry-After; kit intents make no LLM call and are not counted. 0 disables
    # it. A caller with no identity has no budget here; the planner's
    # concurrency gate and bounded queue hold those. Browser traffic through
    # the frontend's plain /api rewrite arrives from Vercel's shared egress, so
    # it shares one budget until the frontend sends the token and the visitor.
    decompose_rate_limit_per_minute: int = 30
    # Dispute challenges one client may mint per minute (POST
    # /api/disputes/challenge), keyed by the same client_identity(). Every mint that
    # takes a slot holds it for five minutes out of a 200-slot `dispute` budget,
    # so an unbounded client could fill that budget alone; at 20 a minute one
    # client holds at most 100. A buyer disputes one step at a time, and a
    # re-mint of a live challenge returns the same nonce, so no honest flow
    # comes near it. A breach is 429 "dispute_challenge_rate_limited" with
    # Retry-After; 0 disables it. No identity, no budget, as above.
    dispute_challenge_rate_limit_per_minute: int = 20
    # Most agents listed in the planning prompt. Prompt tokens per planner call
    # grew with every bound agent; past this many that cleared the floor, the
    # best-scored are listed. Never below the starvation backstop's minimum.
    decompose_prompt_max_agents: int = 24

    # ── Stored plans ──────────────────────────────────────────
    # How long a built plan stays executable. The card the buyer authorises
    # freezes prices, reputation stamps and floor notices at the moment the
    # plan was built, so it must not outlive the marketplace it describes;
    # the execute-time re-check covers listing and the floor, and this covers
    # everything else on the card. It must still outlive decompose, a careful
    # read, a wallet signature and /execute, with room for a buyer who tabs
    # away: 15 minutes covers that several times over. An expired plan is
    # refused with 410 "plan_expired" before any task is minted.
    plan_ttl_seconds: float = 900.0

    @model_validator(mode="after")
    def _plan_ttl_is_usable(self) -> "Settings":
        """Refuse a plan TTL that no buyer could execute inside, or never expires.

        A NaN compares false with every age, so it would never expire a plan;
        infinity does the same openly; anything under a minute expires the plan
        before a buyer can read the card and sign. Names the variable, never
        the value, per the boot-failure rule.
        """
        if not math.isfinite(self.plan_ttl_seconds) or self.plan_ttl_seconds < 60:
            raise ValueError(
                "PLAN_TTL_SECONDS must be finite and at least 60 — a plan has to outlive "
                "reading the card, signing the authorisation and executing it"
            )
        return self

    @model_validator(mode="after")
    def _refund_reconcile_interval_is_usable(self) -> "Settings":
        """Refuse a sweep interval that could not keep up with the RPC's history.

        A NaN or inf would make the loop's sleep raise or never end; under 30
        seconds a pass is a chain read per claim on repeat; over an hour starts
        eating into the RPC's history window, which is what makes NOT_FOUND
        answerable at all. Checked whether or not the sweep is on, so turning
        it on later is not the moment a typo is found. Names the variable,
        never the value.
        """
        interval = self.refund_reconcile_interval_seconds
        if not (math.isfinite(interval) and 30 <= interval <= 3600):
            raise ValueError(
                "REFUND_RECONCILE_INTERVAL_SECONDS must be a finite number of seconds from 30 to 3600 — the sweep "
                "reads the chain once per held refund claim each pass, and has to look at every claim well inside "
                "the RPC's transaction history window"
            )
        return self

    @model_validator(mode="after")
    def _registry_boot_sync_timeout_is_a_bound(self) -> "Settings":
        """Refuse a pre-warm wait that is not a bound.

        NaN would make the wait expire on arrival and inf would let a hung RPC
        hold the reputation pre-warm back forever, leaving the first plans
        after a restart routed on priors. Past 60 s the pre-warm starts so late
        that the plans it exists for have already been made. Names the
        variable, never the value.
        """
        bound = self.registry_boot_sync_timeout_seconds
        if not (math.isfinite(bound) and 0 <= bound <= 60):
            raise ValueError(
                "REGISTRY_BOOT_SYNC_TIMEOUT_SECONDS must be a finite number of seconds from 0 to 60 — the "
                "reputation pre-warm waits this long, in the background, for the first registry sync pass"
            )
        return self

    @model_validator(mode="after")
    def _mainnet_requires_mainnet_passphrase(self) -> "Settings":
        """Fail fast on a half-flipped mainnet config.

        stellar_network and stellar_network_passphrase default independently
        (testnet), so STELLAR_NETWORK=mainnet with a forgotten passphrase
        would silently sign transactions for the WRONG network. Signing key
        is deliberately not required — read-only deployments are legitimate.

        The label is read through `label_names_mainnet` — any case, any
        padding, `pubnet` included — so ` Mainnet` or `pubnet` over the
        testnet passphrase is refused here rather than booting as a testnet
        deployment that calls itself mainnet. The opposite mismatch, a testnet label over the mainnet passphrase, is
        not refused: `is_mainnet` reads the passphrase, so the key rule and the
        explorer links already treat that process as the mainnet it is.
        """
        if label_names_mainnet(self.stellar_network) and not self.is_mainnet():
            raise ValueError(
                "STELLAR_NETWORK is set to mainnet/public but "
                "STELLAR_NETWORK_PASSPHRASE is not the mainnet passphrase "
                f"({MAINNET_PASSPHRASE!r}). Set STELLAR_NETWORK_PASSPHRASE "
                "to match, or switch STELLAR_NETWORK back to testnet."
            )
        return self

    @model_validator(mode="after")
    def _production_webhooks_require_signature(self) -> "Settings":
        """Fail fast when production PDAX would accept unsigned webhooks.

        The webhook route verifies signatures with pdax_webhook_secret and
        only skips verification via the pdax_allow_unsigned_webhooks escape
        hatch. Both an enabled escape hatch and a missing secret would let
        anyone forge deposit/withdrawal callbacks in production, so refuse
        to start rather than run open.

        Scoped by whether the configured environment resolves to a real-fiat
        base URL, not by its name, so a future real-value environment is
        covered automatically.
        """
        if pdax_moves_real_value(self.pdax_environment):
            if self.pdax_allow_unsigned_webhooks:
                raise ValueError(
                    "PDAX_ALLOW_UNSIGNED_WEBHOOKS must not be enabled when "
                    "PDAX_ENVIRONMENT is production. Unset it and configure "
                    "PDAX_WEBHOOK_SECRET instead."
                )
            if not self.pdax_webhook_secret:
                raise ValueError(
                    "PDAX_WEBHOOK_SECRET is required when PDAX_ENVIRONMENT "
                    "is production — without it inbound webhooks cannot be "
                    "verified. Set the shared secret from the PDAX console."
                )
        return self

    @model_validator(mode="after")
    def _money_capable_config_requires_api_key(self) -> "Settings":
        """Fail fast when the money-moving routes would answer anonymously.

        `require_api_key` (app/security.py) is a deliberate no-op while
        api_key is empty — the public demo runs open — and that is only safe
        while nothing behind it can move real value. Once the process holds
        credentials that CAN, an empty API_KEY leaves the entire secured PDAX
        router (/api/pdax/ramp/{onramp,offramp}, /fiat/withdraw,
        /crypto/withdraw, /trade/order, /balances) plus
        /api/stellar/server/charge and /server/seal callable by anyone on the
        internet, with the backend signing on their behalf.

        "Can move real value" is scoped narrowly on purpose, so local dev and
        CI keep booting: a signing key on mainnet (the mainnet PASSPHRASE,
        whatever STELLAR_NETWORK says — see `is_mainnet`), or PDAX credentials in the
        production environment. Testnet signers and uat/stage PDAX move play
        money and stay open, as does a read-only mainnet deployment.

        The refund path (story 4.03) is the exception that applies on testnet
        as well, because an adjudicated payout spends the platform's OWN
        balance rather than an allowance a payer already authorised. It is
        scoped to `dispute_refunds_enabled` so that it names a deliberate
        operator choice rather than the presence of credentials every test run
        and laptop already has.

        Refusing to boot — rather than reporting not-ready — is both the
        safer option and the one consistent with the validators above.
        Failing /readiness would not actually close the hole: render.yaml
        points healthCheckPath at /health, so a not-ready answer never stops
        Render from routing traffic, and the process would keep serving the
        anonymous withdrawal routes it was supposed to be protecting. A
        refused boot fails the deploy loudly and cannot be missed.
        """
        if self.api_key:
            return self
        exposures: list[str] = []
        # The passphrase, not the label: see `is_mainnet`. A `testnet` label
        # beside the mainnet passphrase still signs real transactions.
        if self.is_mainnet() and self.stellar_signing_key:
            exposures.append(
                "STELLAR_SIGNING_KEY is set on mainnet, so /api/stellar/server/charge "
                "and /server/seal sign real transactions"
            )
        if pdax_moves_real_value(self.pdax_environment) and self.pdax_username and self.pdax_password:
            exposures.append("production PDAX credentials are set, so /api/pdax/* can move real fiat")
        # Story 4.03, and the one exposure here that bites on TESTNET too. The
        # branches above leave testnet open because a testnet signer moves play
        # money on behalf of a payer who already authorised the spend on-chain.
        # The refund path is different in kind: an adjudicator's say-so moves
        # the PLATFORM's own balance to an address in the request, with no
        # prior authorisation to bound it. Anonymous, that is a drain of the
        # settler wallet on any network, so the refund routes demand a key
        # wherever they can actually sign.
        # The switch ALONE, deliberately: conjoining it with the signer and the
        # SAC would let a deployment that flips refunds on before wiring either
        # of them boot with an empty key, leaving `require_adjudicator` as the
        # only thing between an anonymous caller and the payout route. It fails
        # closed, so the door is shut either way — but a validator that
        # promises "refunds on implies a key" must not have a hole in it, and
        # the operator who turns the switch on is the one who should be told.
        if self.dispute_refunds_enabled:
            # Says what the branch above ACTUALLY tests. It reads
            # `dispute_refunds_enabled` alone, deliberately (see the comment
            # above) — so naming the signer and the SAC as the trigger sent an
            # operator mid-deploy-failure hunting for credentials that may not
            # be set, and left the real cause, the switch, unnamed.
            exposures.append(
                "DISPUTE_REFUNDS_ENABLED is on, so /api/disputes/{id}/uphold is a route that "
                "transfers the platform's own funds — whether or not the signer is wired yet"
            )
        if exposures:
            raise ValueError(
                "API_KEY is required because " + "; and ".join(exposures) + ". Without it every "
                "money-moving route is anonymous. Set API_KEY (in the Render dashboard for the "
                "deployed service) and send it as the X-API-Key header, or remove the "
                "credentials above to run a read-only/demo deployment."
            )
        return self

    @model_validator(mode="after")
    def _keyless_seal_is_never_mainnet(self) -> "Settings":
        """The keyless-seal opt-out is for local and CI testnet runs, never for real money."""
        if self.allow_keyless_server_seal and self.is_mainnet():
            raise ValueError(
                "ALLOW_KEYLESS_SERVER_SEAL must not be set on mainnet: it lets anyone have the platform's "
                "sealer sign an attestation. Unset it and set API_KEY."
            )
        return self

    @model_validator(mode="after")
    def _frontend_proxy_token_is_strong(self) -> "Settings":
        """Refuse a frontend token a caller could guess, or one that would print itself.

        It lifts per-client rate limits for whoever sends it, so a short one is
        a bypass, and below `security._MIN_MASKED_SECRET_CHARS` the log
        redaction would not mask it by value either.
        """
        token = self.frontend_proxy_token
        if token and (len(token) < 32 or token != token.strip() or not token.isascii()):
            raise ValueError(
                "FRONTEND_PROXY_TOKEN must be at least 32 ascii characters with no surrounding whitespace "
                "(generate one with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`), or be unset."
            )
        return self

    @model_validator(mode="after")
    def _api_key_is_usable_on_the_wire(self) -> "Settings":
        """Refuse a key that would lock the operator out, or leak into the log.

        RAISED, not logged, and that is the whole point of it. Each of the
        three shapes below produces a service that *boots clean* and then
        answers 401 to its own operator on every uphold — a refusal
        indistinguishable, in the access log and in the body, from an
        attacker's. A named deploy failure someone has to read beats a silent
        permanent lockout nobody can diagnose.

          * NON-ASCII. Starlette decodes header bytes as latin-1, so what the
            operator's client puts on the wire and what an env var holds are
            the same string only while every byte is ascii.
            `security.header_secret_matches` round-trips those bytes so an
            accent no longer locks the door — but a key that means different
            things to a client, a proxy and this process is a key to replace,
            not one to make work.
          * PADDED. HTTP parsers do not agree about the optional whitespace
            around a header value (uvicorn may run h11 or httptools), so a key
            with padding is a key whose acceptance depends on which one the
            deploy happened to pick. The comparison strips both sides; this
            makes sure nobody relies on that.
          * SHORTER THAN 8 CHARACTERS. `security._MIN_MASKED_SECRET_CHARS` is
            8: below it the redaction filter does not mask a configured value
            by exact match, because masking every occurrence of a short string
            would shred unrelated log text. So a 6-character API_KEY is a
            credential that PRINTS ITSELF into any log line that quotes it —
            and this is the validator that filter's comment defers to.

        An empty API_KEY is not checked here: that is the public demo's
        supported configuration, and whether it is allowed at all is
        `_money_capable_config_requires_api_key`'s question, not this one.
        """
        key = self.api_key
        if not key:
            return self
        faults: list[str] = []
        if not key.isascii():
            faults.append("it holds a non-ascii character, which no HTTP client can send unambiguously")
        if key != key.strip():
            faults.append("it is padded with whitespace, which HTTP parsers disagree about")
        if len(key.strip()) < _MIN_API_KEY_CHARS:
            faults.append(
                f"it is shorter than {_MIN_API_KEY_CHARS} characters, "
                "below which the log redaction filter will not mask it"
            )
        if faults:
            # The key is a credential: name the variable and the faults, never
            # echo the value or its length beyond the bound it failed.
            raise ValueError(
                "API_KEY cannot be used as configured because " + "; and ".join(faults) + ". Set API_KEY "
                "(in the Render dashboard for the deployed service) to at least "
                f"{_MIN_API_KEY_CHARS} printable ascii characters with no surrounding whitespace — "
                "otherwise the adjudication routes answer 401 to the operator's own key, "
                "indistinguishably from an attacker, for as long as the deployment lives."
            )
        return self

    @model_validator(mode="after")
    def _money_bounds_bound_something(self) -> "Settings":
        """Refuse a money bound that cannot bound anything (QA D-054).

        Each setting checked here is read, at the point it guards, by a `<` or
        a `>` — and every comparison against NaN is false, while nothing is
        greater than inf. So a bound that is not a finite number does not fail
        loudly: it FAILS OPEN, silently, on exactly the guard whose job is to
        limit a loss. `MAX_REFUND_USDC=nan` was observed doing it: the 50 USDC
        credit it exists to refuse came back creditable at 50.0. pydantic
        accepts `nan`, `inf` and `-inf` for a float field by default, so
        nothing upstream of this validator stands in the way.

        The rules, one per bound:

          * `MAX_REFUND_USDC` — finite and strictly above zero. The ceiling on
            one credit the PLATFORM pays out of its own wallet on an
            adjudicator's say-so; not a number is no ceiling at all, and zero
            or below refuses every credit, which is a refund path switched off
            by a typo rather than by `DISPUTE_REFUNDS_ENABLED`.
          * `MAX_CHARGE_USDC` — finite and strictly above zero. The ceiling on
            one `PaymentEscrow.charge`, which `execution_svc` and
            `/api/stellar/server/charge` both compare against; not a number
            lets every plan total through uncapped, and zero or below skips
            the charge, the seal and the ratings of every paid run.
          * `DISPUTE_CREDITED_FRACTION` — finite, within [0, 1]. The policy
            share of a step that an upheld dispute credits.
            `refund_svc.credited_amount_usdc` clamps it with `min(max(…))`,
            which a NaN passes through untouched and freezes onto every
            dispute opened as its promise; outside [0, 1] the clamp quietly
            applies a policy nobody typed, so it is refused rather than bent.
          * `DISPUTE_WINDOW_SECONDS` — finite and strictly above zero. Not an
            amount, but the bound on how long a paid run stays refundable:
            stamped onto each settlement as `window_closes_at`, it is then
            read by `time.time() > window_closes_at`, which a NaN or inf
            stamp never satisfies — a window that never closes on work that
            is already paid for, and cannot be reopened once stamped.

        Raised rather than logged, for the reason the reputation bounds are:
        what it prevents is silent, and a refused deploy cannot be missed.
        The message names the variable and the rule and NEVER the value, the
        property `_boot_failure_message` depends on — `API_KEY`'s validator is
        the model. These values are not secrets, but a message that quotes its
        input is one edit away from a message that quotes a secret.
        """
        faults: list[str] = []
        refund_cap = self.max_refund_usdc
        if not (math.isfinite(refund_cap) and refund_cap > 0):
            faults.append(
                "MAX_REFUND_USDC is not a finite number of USDC above zero — it is the ceiling on ONE credit "
                "the platform pays from its own wallet, a ceiling that is not a finite number compares false "
                "against every amount and so bounds nothing, and one at or below zero refuses every credit"
            )
        charge_cap = self.max_charge_usdc
        if not (math.isfinite(charge_cap) and charge_cap > 0):
            faults.append(
                "MAX_CHARGE_USDC is not a finite number of USDC above zero — it is the ceiling on ONE "
                "PaymentEscrow.charge, a ceiling that is not a finite number lets every plan total through "
                "uncapped, and one at or below zero skips the charge and the seal of every paid run"
            )
        fraction = self.dispute_credited_fraction
        if not (math.isfinite(fraction) and 0 <= fraction <= 1):
            faults.append(
                "DISPUTE_CREDITED_FRACTION is not a finite fraction from 0 to 1 — it is the share of a disputed "
                "step an upheld dispute credits, one that is not a finite number is frozen onto every dispute "
                "opened as the credit it promises, and one outside 0 to 1 is not the policy the buyer was told"
            )
        window = self.dispute_window_seconds
        if not (math.isfinite(window) and window > 0):
            faults.append(
                "DISPUTE_WINDOW_SECONDS is not a finite number of seconds above zero — it is stamped onto every "
                "settlement as the time its disputes close, a window that is not a finite number never closes, "
                "and one at or below zero is closed before the buyer can open a dispute"
            )
        if faults:
            raise ValueError(
                "A money bound cannot be used as configured: " + "; and ".join(faults) + ". Set each named "
                "variable (in the Render dashboard for the deployed service) to a plain decimal number within "
                "the rule stated, or unset it to take the default."
            )
        return self

    @model_validator(mode="after")
    def _reputation_settings_are_in_range(self) -> "Settings":
        """Refuse a reputation setting the scoring cannot use (audit 3, finding 5).

        Only the batch timeout was ever checked. Every other reputation number
        was taken as typed, and pydantic parses `nan`, `inf` and any sign for a
        float field, so a typo did not fail — it changed the scoring, silently:

          * `REPUTATION_PRIOR_BPS` — within 0..10000, the rating scale. Outside
            it, a prior-only agent was served a smoothed score of 20000 or -100
            (`_prior_info` does not clamp).
          * `REPUTATION_FLOOR_BPS` — within 0..10000. A floor below 0 or above
            the scale admits or refuses every agent without looking.
          * `REPUTATION_PRIOR_WEIGHT_USDC` — finite and above zero. NaN made
            every read raise and took lifespan down with an opaque "cannot
            convert float NaN to integer"; zero or below leaves the prior no
            mass, scores every newcomer's bound at 0, and excludes them all.
          * `REPUTATION_MAX_RATING_WEIGHT_USDC` — finite and above zero. NaN
            compares false, so it DISABLED the cap: a 50,000 USDC step weighed
            50,000 USDC.
          * `REPUTATION_MAX_RATING_TO_PRIOR_RATIO` — finite and above zero,
            for the same reason: it is the cap that binds.
          * `REPUTATION_READ_TTL_SECONDS` — finite and above zero. inf meant a
            read was never repeated; zero or below meant nothing was cached.
          * `REPUTATION_STALE_GRACE_SECONDS` — finite and not below zero (0
            turns stale serving off). inf would serve a read of any age.

        Raised, like the money bounds, because what it prevents is silent; and
        the message names each variable and its rule and NEVER the value.
        """
        faults: list[str] = []
        for name, bps in (
            ("REPUTATION_PRIOR_BPS", self.reputation_prior_bps),
            ("REPUTATION_FLOOR_BPS", self.reputation_floor_bps),
        ):
            if not 0 <= bps <= 10_000:
                faults.append(f"{name} is not a whole number of basis points from 0 to 10000")
        for name, value, unit in (
            ("REPUTATION_PRIOR_WEIGHT_USDC", self.reputation_prior_weight_usdc, "USDC"),
            ("REPUTATION_MAX_RATING_WEIGHT_USDC", self.reputation_max_rating_weight_usdc, "USDC"),
            ("REPUTATION_MAX_RATING_TO_PRIOR_RATIO", self.reputation_max_rating_to_prior_ratio, "multiples"),
            ("REPUTATION_READ_TTL_SECONDS", self.reputation_read_ttl_seconds, "seconds"),
        ):
            if not (math.isfinite(value) and value > 0):
                faults.append(f"{name} is not a finite number of {unit} above zero")
        grace = self.reputation_stale_grace_seconds
        if not (math.isfinite(grace) and grace >= 0):
            faults.append("REPUTATION_STALE_GRACE_SECONDS is not a finite number of seconds, zero or above")
        if faults:
            raise ValueError(
                "A reputation setting cannot be used as configured: " + "; and ".join(faults) + ". Set each "
                "named variable (in the Render dashboard for the deployed service) to a plain number within the "
                "rule stated, or unset it to take the default."
            )
        return self

    @model_validator(mode="after")
    def _reputation_read_has_a_real_bound(self) -> "Settings":
        """Fail fast when the batched reputation read has no usable deadline.

        fetch_reps (services/reputation_svc.py) hands this number to
        asyncio.wait as the deadline its per-agent reads share. The budget rule
        below only ever looked UP — it caps the bound at a share of the
        planning budget — so nothing stopped it from going down to nothing. A
        deadline of 0, any negative value, or NaN is treated as already
        expired: no rep_state read that has to reach the chain can answer, so
        every agent without a recent read falls back to the prior marked
        degraded, and every plan goes out flagged reputation_degraded. Under
        the shipped config a prior-only agent clears the floor, so the floor
        then fails OPEN for the life of the process — an agent the ledger has
        already rated below it is routable again, with the chain perfectly
        healthy. The degradation policy accepts failing open while reads
        genuinely cannot be had; a timeout that expires on arrival makes that
        permanent, and no RPC recovery can end it.

        inf is the opposite failure: no bound at all, so a hung RPC holds every
        decompose for as long as the socket does — the exact incident this
        timeout exists to absorb. A finite planning budget happens to catch it
        in the share rule below, but only as a ratio; refusing it here names
        the actual problem. NaN is refused here for a sharper reason still: it
        compares false against everything, so the share rule cannot see it.

        Raised rather than logged, for the budget rule's own reason: it can
        only reject a number this file declares, which the deploy that typed it
        can untype, and what it prevents is silent — reads "succeed" at the
        prior, nothing errors, and the only symptom is a trust gate that has
        stopped gating.
        """
        bound = self.reputation_batch_timeout_seconds
        if not (math.isfinite(bound) and bound > 0):
            # Read from the field rather than restated, so the advice cannot
            # drift from the default it names.
            default = type(self).model_fields["reputation_batch_timeout_seconds"].default
            raise ValueError(
                f"REPUTATION_BATCH_TIMEOUT_SECONDS={bound:g} is not a positive, finite number of seconds. "
                "A deadline of zero, below zero or NaN expires before any reputation read can answer, so every "
                "agent is scored on the prior, every plan is flagged reputation_degraded and the routing floor "
                "stops filtering anyone; inf removes the bound, so a hung Soroban RPC stalls every /decompose. "
                "Set REPUTATION_BATCH_TIMEOUT_SECONDS to a positive number of seconds within the planning budget "
                f"(the default is {default:g})."
            )
        return self

    @model_validator(mode="after")
    def _reputation_read_fits_the_planning_budget(self) -> "Settings":
        """Fail fast when a reputation read could swallow the planning budget.

        decompose() (services/orchestrator_svc.py) reads reputation for every
        registered agent BEFORE it enters the asyncio.wait_for that bounds the
        planning call, so the two budgets are serial, not nested: a request's
        real planning latency is reputation_batch_timeout_seconds +
        decompose_timeout_seconds, and only the second half has an error code.
        A read that overruns does not produce 504 decompose_timeout — it
        produces a slow 200, or a client that gave up, with nothing in the
        failure taxonomy naming reputation at all. On the demo-kit path it is
        worse: that short circuit returns before the wait_for is ever reached,
        so the batch bound is the whole of the request's budget.

        The rule is a SHARE of the planning budget rather than a fixed slack,
        because both numbers are env-tunable and a fixed slack pins a shape
        that is wrong the moment either side moves. 10% is sized off how much
        of the decompose budget is already spoken for: the LLM leg it wraps is
        allowed llm_timeout_seconds = 120 s per attempt, which is MORE than the
        90 s decompose_timeout_seconds it is nested inside. That inversion is
        deliberate — the inner number is the OpenAI client's per-request HTTP
        bound, there to replace its 600 s default, while the outer one is the
        end-to-end bound including queue time at _decompose_gate() — but it
        means the planner is entitled to spend the entire budget and the plan
        assembly after it is free, so there is no idle slack for a reputation
        read to borrow. Today's shipped numbers sit at 2.5 / 90 = 2.8%, so the
        live config keeps 3.6x of headroom and ordinary tuning still boots: a
        batch bound doubled to 5 s, or a decompose timeout tightened to 30 s,
        both pass.

        Raised rather than logged — the opposite call from the report-only
        family below, and deliberately so. Those catch values this file cannot
        verify (a mistyped environment name, a malformed base32 seed, a key
        format that may drift), where being wrong about the world would take a
        live mainnet deploy down on merge. This one compares two numbers this
        file declares itself: it can only reject a ratio a human typed, and the
        same deploy that typed it can untype it. What it prevents is worse than
        a merely broken deployment. The batch timeout exists so a Soroban
        outage degrades reputation to the prior instead of taking planning
        down, and a bound sized near the planning budget silently deletes that
        guarantee — every decompose waits the full bound before falling back,
        so the mechanism written to make an outage invisible becomes the
        outage. It is latent, too: healthy RPC answers in milliseconds, so the
        bad ratio tests clean and only bites during the exact incident the
        timeout was written to survive.

        reputation_read_ttl_seconds is deliberately NOT part of this check. It
        is a cache TTL, not a request bound: it decides how OFTEN a decompose
        pays for a live read, never how long one is allowed to take, so
        measuring it against a timeout is a category error (a longer TTL makes
        reads rarer, not slower; a TTL of 0 makes every decompose pay the bound
        this rule already covers). The one relationship worth naming is a TTL
        below the batch bound, where an entry can expire before the read that
        wrote it returns — that wastes cache hits, breaches no budget, and does
        not earn the power to refuse a boot.
        """
        # A share of a budget that is not a real duration is not a rule. NaN
        # compares false against everything and a share of inf is inf, so
        # either one waved every batch bound through; zero or below was caught,
        # but by a message advising a batch bound of zero or below — advice the
        # validator above refuses. Each also breaks planning on its own:
        # wait_for expires a NaN, zero or negative deadline on arrival, so every
        # free-form plan is a 504, and inf leaves the call with no bound at all.
        planning = self.decompose_timeout_seconds
        if not (math.isfinite(planning) and planning > 0):
            raise ValueError(
                f"DECOMPOSE_TIMEOUT_SECONDS={planning:g} is not a positive, finite number of seconds. A deadline "
                "of zero, below zero or NaN expires every planning call the moment it starts and inf never "
                "expires one, and no share of any of them can bound the reputation read that runs before it. "
                "Set DECOMPOSE_TIMEOUT_SECONDS to a positive number of seconds."
            )
        allowance = self.decompose_timeout_seconds * REPUTATION_READ_BUDGET_SHARE
        if self.reputation_batch_timeout_seconds > allowance and not math.isclose(
            self.reputation_batch_timeout_seconds, allowance, rel_tol=REPUTATION_READ_BUDGET_TOLERANCE
        ):
            raise ValueError(
                f"REPUTATION_BATCH_TIMEOUT_SECONDS={_seconds(self.reputation_batch_timeout_seconds)} is more than "
                f"{REPUTATION_READ_BUDGET_SHARE:.0%} of DECOMPOSE_TIMEOUT_SECONDS="
                f"{_seconds(self.decompose_timeout_seconds)} (at most {_seconds(allowance)} s is allowed). "
                "Reputation is read before the planning call, not inside it, so a Soroban outage would add "
                "that long to every /decompose with no error code naming it. Lower "
                f"REPUTATION_BATCH_TIMEOUT_SECONDS to {_seconds(allowance)} or less, or raise "
                "DECOMPOSE_TIMEOUT_SECONDS to at least "
                f"{_seconds(self.reputation_batch_timeout_seconds / REPUTATION_READ_BUDGET_SHARE)}."
            )
        return self

    @model_validator(mode="after")
    def _reputation_deadline_covers_the_batch(self) -> "Settings":
        """Refuse a batch deadline too short for the reads it has to wait on.

        The budget rule above only ever asked whether the deadline fits the
        PLANNING budget. Nothing asked whether it fits the WORK: a batch of
        REPUTATION_BATCH_AGENTS reads, REPUTATION_READ_CONCURRENCY at a time,
        each sized at REPUTATION_READ_LATENCY_SECONDS, needs
        ceil(agents / concurrency) x latency to finish. A deadline under that
        is not a bound on an outage; it is an outage on a healthy chain — every
        batch cuts off its last wave, those agents fall back, and with no
        recent read to serve they fall back to the prior and the floor fails
        open for them. That is the configuration that shipped: 23 agents, 8
        shared threads, two hops a read, 2.5 s.

        Each number must also be a real one — a finite latency above zero, a
        concurrency from 1 to 64 (they are threads), at least one agent — or
        the product is meaningless. As with the money bounds, the message
        names the variables and the rule and never a value.
        """
        faults: list[str] = []
        latency = self.reputation_read_latency_seconds
        if not (math.isfinite(latency) and latency > 0):
            faults.append("REPUTATION_READ_LATENCY_SECONDS is not a finite number of seconds above zero")
        if not 1 <= self.reputation_read_concurrency <= 64:
            faults.append("REPUTATION_READ_CONCURRENCY is not a whole number of threads from 1 to 64")
        if self.reputation_batch_agents < 1:
            faults.append("REPUTATION_BATCH_AGENTS is not a whole number of agents of at least 1")
        if not faults:
            waves = math.ceil(self.reputation_batch_agents / self.reputation_read_concurrency)
            if waves * latency > self.reputation_batch_timeout_seconds:
                faults.append(
                    "REPUTATION_BATCH_TIMEOUT_SECONDS is shorter than the batch it bounds: "
                    "ceil(REPUTATION_BATCH_AGENTS / REPUTATION_READ_CONCURRENCY) x "
                    "REPUTATION_READ_LATENCY_SECONDS must fit inside it, or every batch cuts off its last wave "
                    "of reads on a healthy chain and those agents are routed on the prior"
                )
        if faults:
            raise ValueError(
                "The reputation batch cannot be sized as configured: " + "; and ".join(faults) + ". Raise "
                "REPUTATION_BATCH_TIMEOUT_SECONDS (within the planning budget) or REPUTATION_READ_CONCURRENCY, "
                "or unset the named variables to take the defaults."
            )
        return self

    # ── Startup reports (log, never raise) ────────────────────────
    # The validators above refuse to boot because the misconfiguration they
    # catch would EXPOSE money routes or sign on the wrong network. The ones
    # below catch a different class: values that are merely wrong, and whose
    # only victim is the deployment itself. This service is live on mainnet
    # with autoDeploy on, so a validator that wrongly rejects a real config
    # takes the product down on merge — a cost that is never worth paying to
    # improve an error message. They log loudly at startup instead, naming the
    # variable and what breaks, so the operator does not have to trace a 500
    # three layers down at request time.

    @model_validator(mode="after")
    def _report_unknown_pdax_environment(self) -> "Settings":
        """Name a mistyped PDAX_ENVIRONMENT at startup, not at first use.

        base_url() (app/pdax/config.py) raises RuntimeError lazily when the
        first PDAX client is built, so a typo boots clean and then 500s
        /api/pdax/environment and every other PDAX route.

        Logged rather than raised: an unknown environment resolves to no base
        URL at all, so the integration is inert — nothing authenticates, trades
        or moves fiat — which makes this a broken deployment, not an exposure.
        One sharp edge is called out in the message: an unknown value is also
        not "production", so _production_webhooks_require_signature above stays
        silent for it. An empty value is left alone; base_url() defaults it to
        DEFAULT_ENVIRONMENT ("uat") exactly as it always has.
        """
        environment = (self.pdax_environment or "").strip().lower()
        if environment and environment not in PDAX_ENVIRONMENTS:
            logger.error(
                "PDAX_ENVIRONMENT=%r is not a known PDAX environment (expected one of: %s). "
                "No base URL can be resolved, so every /api/pdax/* route will fail on its "
                "first call; an unrecognised value is also not treated as production, so the "
                "webhook-signature requirement will not be enforced. Fix PDAX_ENVIRONMENT.",
                self.pdax_environment,
                ", ".join(PDAX_ENVIRONMENTS),
            )
        return self

    @model_validator(mode="after")
    def _report_malformed_pdax_otp_secret(self) -> "Settings":
        """Name a mistyped PDAX_OTP_SECRET at startup, not at the MFA challenge.

        totp_now() (app/pdax/totp.py) base32-decodes the seed the first time
        PDAX answers a SOFTWARE_TOKEN_MFA challenge; a mistyped seed fails
        there, one login attempt deep into a code path that only runs when MFA
        is switched on upstream. The decode below mirrors that function's
        normalisation and padding exactly, so agreement is not a coincidence.

        Logged rather than raised: a bad seed only breaks this deployment's own
        PDAX login (it fails closed — no session, no orders), and the value is
        optional and unset in most deployments, so it can never justify
        refusing to boot the live service.
        """
        secret = self.pdax_otp_secret.strip().replace(" ", "").upper()
        if not secret:
            return self
        remainder = len(secret) % 8
        padded = secret + ("=" * (8 - remainder)) if remainder else secret
        try:
            base64.b32decode(padded)
        except (binascii.Error, ValueError):
            # The seed itself is a credential: name the variable, never echo
            # the value or the decoder's message (which quotes the input).
            logger.error(
                "PDAX_OTP_SECRET is not valid base32. Every PDAX login that hits a "
                "SOFTWARE_TOKEN_MFA challenge will fail to answer it, so no PDAX call can "
                "authenticate. Re-copy the TOTP seed from the PDAX console, or unset "
                "PDAX_OTP_SECRET if the account has no MFA."
            )
        return self

    @model_validator(mode="after")
    def _report_malformed_stellar_signing_key(self) -> "Settings":
        """Name a malformed STELLAR_SIGNING_KEY at startup, not on the money path.

        _signer_keypair() (app/stellar/client.py) parses the key lazily and
        memoizes it, so a bad key boots clean and first shows up as a
        400 charge_failed on a real payment — the worst possible place to
        learn about a typo. The two accepted forms below are that function's,
        so a key this reports as good is one it can build.

        Logged rather than raised: an unparseable key signs nothing, so it
        fails closed; the exposure the fail-fast validators guard against is
        the opposite case, a key that works. Raising here would also hand any
        future key-format drift the power to brick a live mainnet deploy.

        The key is a bearer credential for real funds: this reports the
        variable and never the value. stellar_sdk's own exception text quotes
        the rejected seed back, so the exception is deliberately swallowed
        (no message interpolation, no exc_info) rather than logged.
        """
        secret = self.stellar_signing_key.strip()
        if not secret:
            return self
        # Imported here, not at module scope: app.config is imported by
        # everything (including the smoke scripts) and stellar_sdk costs
        # ~250ms to import. This runs once, at construction.
        from stellar_sdk import Keypair

        words = secret.split()
        try:
            if len(words) >= 12:
                Keypair.from_mnemonic_phrase(" ".join(words))
            else:
                Keypair.from_secret(secret)
        except Exception:
            logger.error(
                "STELLAR_SIGNING_KEY is malformed — it is neither a valid S… secret key nor a "
                "valid 12/24-word mnemonic phrase (the value is withheld from this log). Every "
                "backend-signed transaction will fail, so /api/stellar/server/charge and "
                "/server/seal will answer 400 charge_failed. Re-inject the key from host secrets."
            )
        return self

    @model_validator(mode="after")
    def _report_contract_reads_without_a_source_address(self) -> "Settings":
        """Name a missing STELLAR_ADMIN_ADDRESS at startup, not per request.

        Every simulate_read needs a source account, and app/stellar/client.py
        raises RuntimeError("no source address; set STELLAR_ADMIN_ADDRESS")
        without one — so a deploy that has contract ids but no admin address
        has 100% of its contract reads failing, with the cause named only in a
        stack trace three layers down.

        Logged rather than raised: nothing is exposed by the omission (reads
        fail closed), and /readiness (app/main.py) already reports this exact
        condition as not_ready — this makes the startup story agree with that
        signal instead of duplicating or contradicting it. The report is
        conditioned on at least one contract id being configured, so it stays
        silent for the demo/local defaults, which have neither and which
        /readiness already calls incomplete on the contract ids alone.
        """
        if self.stellar_admin_address.strip():
            return self
        configured = [
            name
            for name, value in (
                ("STELLAR_AGENT_REGISTRY", self.stellar_agent_registry),
                ("STELLAR_REPUTATION_LEDGER", self.stellar_reputation_ledger),
                ("STELLAR_PAYMENT_ESCROW", self.stellar_payment_escrow),
                ("STELLAR_ATTESTATION_REGISTRY", self.stellar_attestation_registry),
                ("STELLAR_ASSET_SAC", self.stellar_asset_sac),
            )
            if value.strip()
        ]
        if configured:
            logger.error(
                "STELLAR_ADMIN_ADDRESS is not set, but contract ids are configured (%s). Contract "
                "reads have no source account, so every on-chain read will fail with "
                "'no source address; set STELLAR_ADMIN_ADDRESS' and /readiness will answer 503 "
                "not_ready. Set STELLAR_ADMIN_ADDRESS to the G… address that funds simulation.",
                ", ".join(configured),
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def is_mainnet(self) -> bool:
        """True when this process signs for the Stellar PUBLIC network (D-074).

        Keyed on the network PASSPHRASE, never on STELLAR_NETWORK. The
        passphrase is hashed into every transaction this process signs, so it
        is the fact that decides which chain a signature is valid on; the label
        is only a name somebody typed. Asking the label let `pubnet`, a padded
        or upper-cased `mainnet`, and even `testnet` beside the mainnet
        passphrase boot a real-money signer with no API_KEY.

        An exact comparison, deliberately: a passphrase that differs by one
        byte — padding included — hashes to a network id no chain answers to,
        so it signs for nothing and moves nothing.

        Every place that asks "is this real money?" asks here: the two boot
        validators below, `stellar.client.explorer_network`, and the operator
        script's explorer links through it.
        """
        return self.stellar_network_passphrase == MAINNET_PASSPHRASE


class ConfigurationError(RuntimeError):
    """This process refuses to boot, and why — WITHOUT quoting any value.

    Raised in place of pydantic's own `ValidationError`, which cannot be
    allowed to reach a deploy log. Its `__str__` appends
    `input_value={...}` — for a model validator that is the WHOLE settings
    dict, truncated to a fixed width, so whichever secrets happen to fall in
    the head or the tail are printed verbatim. Nothing chooses them: it is
    whatever the env supplied, in whatever order, so the leak is intermittent
    rather than absent. An observed failure printed
    `'pdax_password': '…'` in full.

    Where it lands is what makes that serious. `settings = _load_settings()`
    runs at import, so the ValidationError was an uncaught traceback on
    stderr — Render's deploy log, readable by anyone with dashboard access,
    retained, and read by several people at once precisely because the deploy
    just failed. A `STELLAR_SIGNING_KEY` or a `DATABASE_URL` password that
    reaches a log has to be rotated, so a validator that fires on a typo
    turned a five-minute fix into a credential rotation.

    `SecretStr` on the secret fields would also have worked and was not
    chosen: it changes the type of eight fields read across `app/pdax/*`,
    `app/stellar/client.py` and `app/services/*`, so the blast radius is the
    whole service rather than this module. The values are not the problem —
    printing them is.
    """


def _boot_failure_message(exc: ValidationError) -> str:
    """Every refusal pydantic collected, as text an operator can act on.

    `errors()[i]["msg"]` and the field's name, and nothing else: no `input`,
    no `ctx`, no URL. The `msg` of a `value_error` is the text this module's
    own validators wrote, every one of which names the variable at fault and
    none of which echoes its value — that is the property this function
    depends on, so it is the one to re-check before a validator's message
    grows an f-string.

    The field is reported as the ENV VAR an operator sets (`API_KEY`), not as
    the attribute name, because the fix happens in the Render dashboard.
    Model-level validators carry an empty `loc` — they are about a
    combination rather than one field — and their messages already name what
    they are about, so they are printed unprefixed.
    """
    lines = []
    for err in exc.errors():
        # "Value error, " is pydantic's own prefix on a ValueError raised by a
        # validator. Dropped: the sentence after it is a whole sentence.
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        loc = err.get("loc") or ()
        field = ".".join(str(part) for part in loc).upper()
        lines.append(f"  - {field}: {msg}" if field else f"  - {msg}")
    problems = "\n".join(lines)
    return (
        f"Refusing to boot: {len(lines)} configuration problem(s).\n"
        f"{problems}\n"
        "No configured values are shown above, deliberately — this text goes to the deploy log. "
        "Fix the named variables (in the Render dashboard for the deployed service) and redeploy."
    )


def _load_settings(**overrides: object) -> Settings:
    """Build the settings, turning a refusal into one that is safe to print.

    `overrides` exist for the tests that need to drive a real failure through
    this exact path; production calls it with none and the values come from
    the environment.
    """
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        message = _boot_failure_message(exc)
    # Raised OUTSIDE the `except` block, which is not a style choice. Raising
    # inside it — even `from None` — leaves the ValidationError hanging off
    # `__context__`, where the values still are, one attribute away from any
    # handler or reporting tool that walks the exception chain. `from None`
    # only stops the default traceback printing it. Out here the block has
    # ended, so there is no chain to walk.
    raise ConfigurationError(message)


settings = _load_settings()

import base64
import binascii
import logging
import math

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .pdax_environments import BASE_URLS as PDAX_BASE_URLS
from .pdax_environments import moves_real_value as pdax_moves_real_value

logger = logging.getLogger(__name__)

MAINNET_PASSPHRASE = "Public Global Stellar Network ; September 2015"

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
    # resolved by app/security.py client_key().
    #
    # Sized as a WHOLE-SERVICE budget, because that is what it currently is:
    # trusted_proxy_hops defaults to 0, so the key is the constant address our
    # own edge appends and every visitor draws on one bucket. The console's
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
    # in-flight workflows (503 capacity_exhausted) and contract reads are
    # TTL-cached, so a flood buys cheap cached JSON, not LLM calls or RPC.
    # Once trusted_proxy_hops is tuned this becomes per-visitor and can come
    # back down; the frontend backs off on 429 and honours Retry-After, so a
    # tightened limit degrades cadence rather than breaking the console.
    rate_limit_per_minute: int = 1200
    # How many TRAILING X-Forwarded-For entries belong to this deployment's own
    # infrastructure, and are therefore dropped when app/security.py resolves
    # the caller. 0 — the default — keys on the LAST entry, exactly as this
    # service always has, so nothing changes until the value is deliberately
    # tuned. Both directions of error are silent and opposite (too low: the key
    # is a constant our edge wrote, so every visitor shares one rate-limit
    # bucket and the access log's client= cannot attribute abuse; too high: the
    # key is one the CALLER wrote, so the limiter is bypassed by sending a
    # header). Only tune it against a chain actually observed from this edge —
    # see forwarded_chain_samples below and client_key()'s docstring.
    trusted_proxy_hops: int = 0
    # Diagnostic budget for that tuning: log the raw X-Forwarded-For chain and
    # the key it resolves to for the first N non-exempt requests after each
    # process start, then stay silent for the life of the process. 0 disables
    # it. Deliberately a bounded log sample rather than a diagnostic route —
    # the chain carries visitors' IP addresses, and API_KEY is empty on demo
    # deployments so a route could not be reliably gated. Changing any env var
    # on Render redeploys the service, which re-arms the sample.
    forwarded_chain_samples: int = 5
    # Ceiling on concurrently running workflows (each fans out LLM calls).
    # execute returns 503 "capacity_exhausted" once this many are in flight.
    orchestrator_max_concurrent: int = 8
    # Ceiling on concurrent free-form decompose planning calls (each is a
    # real LLM call; the demo-kit path makes none and is never gated).
    # Overflow queues inside decompose_timeout_seconds, so a saturated gate
    # degrades to 504 "decompose_timeout" rather than unbounded LLM spend.
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
    # Wall-clock bound on ONE batched reputation read — the asyncio.wait_for
    # around fetch_reps' gather (services/reputation_svc.py). Lifted out of that
    # function's default argument so a deployment can tune it without a code
    # change and, more to the point, so _reputation_read_fits_the_planning_budget
    # below can see the number it is validating: a bound that exists only as a
    # literal inside a signature is one no validator can check. fetch_reps keeps
    # its per-call override (the degradation tests drive it to 0.02 s to force the
    # timeout path); this value and that function's default are pinned equal by
    # tests/test_reputation_budget.py, so whichever of the two a live read
    # consults, the validator is checking the bound a read actually uses.
    reputation_batch_timeout_seconds: float = 2.5
    # Per-rating weight cap in USDC — one whale job can't own the score.
    reputation_max_rating_weight_usdc: float = 100.0

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

    @model_validator(mode="after")
    def _mainnet_requires_mainnet_passphrase(self) -> "Settings":
        """Fail fast on a half-flipped mainnet config.

        stellar_network and stellar_network_passphrase default independently
        (testnet), so STELLAR_NETWORK=mainnet with a forgotten passphrase
        would silently sign transactions for the WRONG network. Signing key
        is deliberately not required — read-only deployments are legitimate.
        """
        if (
            self.stellar_network.lower() in {"mainnet", "public"}
            and self.stellar_network_passphrase != MAINNET_PASSPHRASE
        ):
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
        CI keep booting: a signing key on mainnet, or PDAX credentials in the
        production environment. Testnet signers and uat/stage PDAX move play
        money and stay open, as does a read-only mainnet deployment.

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
        if self.stellar_network.strip().lower() in {"mainnet", "public"} and self.stellar_signing_key:
            exposures.append(
                "STELLAR_SIGNING_KEY is set on mainnet, so /api/stellar/server/charge "
                "and /server/seal sign real transactions"
            )
        if pdax_moves_real_value(self.pdax_environment) and self.pdax_username and self.pdax_password:
            exposures.append("production PDAX credentials are set, so /api/pdax/* can move real fiat")
        if exposures:
            raise ValueError(
                "API_KEY is required because " + "; and ".join(exposures) + ". Without it every "
                "money-moving route is anonymous. Set API_KEY (in the Render dashboard for the "
                "deployed service) and send it as the X-API-Key header, or remove the "
                "credentials above to run a read-only/demo deployment."
            )
        return self

    @model_validator(mode="after")
    def _reputation_read_has_a_real_bound(self) -> "Settings":
        """Fail fast when the batched reputation read has no usable deadline.

        fetch_reps (services/reputation_svc.py) hands this number to
        asyncio.wait_for as the deadline for the whole batch. The budget rule
        below only ever looked UP — it caps the bound at a share of the
        planning budget — so nothing stopped it from going down to nothing.
        wait_for treats a deadline of 0, any negative value, or NaN as already
        expired: the gather is cancelled before a single rep_state read can
        answer, every agent falls back to the prior marked degraded, and every
        plan goes out flagged reputation_degraded. Under the shipped config a
        prior-only agent clears the floor, so the floor then fails OPEN for
        the life of the process — an agent the ledger has already rated below
        it is routable again, with the chain perfectly healthy. The degradation
        policy accepts failing open because an outage is bounded by the read
        TTL and by this timeout; a timeout that expires on arrival turns a
        bounded outage into a permanent one that no RPC recovery can end.

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
                f"REPUTATION_BATCH_TIMEOUT_SECONDS={self.reputation_batch_timeout_seconds:g} is more than "
                f"{REPUTATION_READ_BUDGET_SHARE:.0%} of DECOMPOSE_TIMEOUT_SECONDS="
                f"{self.decompose_timeout_seconds:g} (at most {allowance:g} s is allowed). Reputation is "
                "read before the planning call, not inside it, so a Soroban outage would add that long to "
                "every /decompose with no error code naming it. Lower REPUTATION_BATCH_TIMEOUT_SECONDS to "
                f"{allowance:g} or less, or raise DECOMPOSE_TIMEOUT_SECONDS to at least "
                f"{self.reputation_batch_timeout_seconds / REPUTATION_READ_BUDGET_SHARE:g}."
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


settings = Settings()

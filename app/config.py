import base64
import binascii
import logging

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

MAINNET_PASSPHRASE = "Public Global Stellar Network ; September 2015"

# The PDAX environments app/pdax/config.py:BASE_URLS can resolve a base URL
# for. Written out here rather than imported because that module does
# `from ..config import settings`: importing it back would run it while this
# module is still executing (Settings() is constructed at the bottom), so the
# import would fail. BASE_URLS stays the source of truth —
# tests/test_config_validators.py asserts the two agree so they cannot drift.
PDAX_ENVIRONMENTS = ("production", "stage", "uat")

# Advertised service version — the FastAPI app's `version` and the liveness
# payload both read it here so the number they report can never disagree.
SERVICE_VERSION = "0.1.0"


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
        """
        if self.pdax_environment.strip().lower() == "production":
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
        if self.pdax_environment.strip().lower() == "production" and self.pdax_username and self.pdax_password:
            exposures.append("production PDAX credentials are set, so /api/pdax/* can move real fiat")
        if exposures:
            raise ValueError(
                "API_KEY is required because " + "; and ".join(exposures) + ". Without it every "
                "money-moving route is anonymous. Set API_KEY (in the Render dashboard for the "
                "deployed service) and send it as the X-API-Key header, or remove the "
                "credentials above to run a read-only/demo deployment."
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

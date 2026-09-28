"""The Orizon HTTP API, called exactly as the dApp calls it.

Every method names the backend route that answers it and the frontend call it
mirrors, and sends the same body with the same headers. The harness must
exercise the API the dApp does — a harness with its own private route to the
money would prove nothing about the product a reviewer opens.

Paths are `/api/...` because that is what the browser sends: the frontend
proxies `/api/*` to the backend verbatim (`next.config.mjs`), and the backend
mounts every router under `/api` (`app/main.py`). `/readiness` is the one
root-level route, read best-effort because the proxy does not forward it.

Three calls are NEVER retried — `submit`, `execute`, `uphold`. Each may take
effect before its answer is lost, so a transport error or a gateway status on
one of them is an `UnknownOutcome`: the runner reads state back and stops.
`open_dispute` is not retried either, but its unknown outcome is recoverable
by a read, because the server keeps one dispute per step (see `stages`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .retry import RETRYABLE_STATUS, RetryableStatus, RetryPolicy, retry_after_seconds

# lib/api.ts GET_TIMEOUT_MS / POST_TIMEOUT_MS: 60 s and 105 s. An uphold signs a
# SAC transfer and then a rating, each polled for up to ~30 s by the backend,
# so it gets the POST budget and then some.
GET_TIMEOUT = 60.0
POST_TIMEOUT = 105.0
UPHOLD_TIMEOUT = 180.0

DISPUTE_READ_GRANT_HEADER = "X-Dispute-Read-Grant"  # app/task_auth.py:235
TASK_TOKEN_HEADER = "X-Task-Token"  # app/task_auth.py:232
API_KEY_HEADER = "X-API-Key"  # app/security.py require_adjudicator

# App error codes that mean "sent, and nobody knows whether it landed".
# app/routers/disputes.py _UPHOLD_RESPONSES: 504 `refund_unconfirmed` — "It may
# still land, so it must be reconciled by hand and NEVER retried."
# `internal_error` is the unhandled-exception 500 (app/main.py:485): whatever
# raised may have done so after the write it was making.
UNKNOWN_OUTCOME_CODES = frozenset({"refund_unconfirmed", "internal_error"})


class ApiError(Exception):
    """A definitive answer from the API: an HTTP status with the envelope's code.

    `app/main.py` answers every error as `{"detail", "error": {"code",
    "message", "request_id"}}`; the code is what the harness branches on.
    """

    def __init__(self, method: str, path: str, status: int, code: str, message: str, body: Any = None) -> None:
        super().__init__(f"{method} {path} -> {status} {code}: {message}")
        self.method = method
        self.path = path
        self.status = status
        self.code = code
        self.message = message
        self.body = body


class UnknownOutcome(Exception):
    """A write that may or may not have taken effect. Never retried."""

    def __init__(self, method: str, path: str, reason: str) -> None:
        super().__init__(f"{method} {path}: outcome unknown ({reason})")
        self.method = method
        self.path = path
        self.reason = reason


def _envelope(response: httpx.Response) -> tuple[str, str, Any]:
    try:
        body = response.json()
    except ValueError:
        return f"http_{response.status_code}", response.text[:200], None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("code"):
            return str(err["code"]), str(err.get("message") or ""), body
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail, detail, body
    return f"http_{response.status_code}", "", body


@dataclass
class OrizonApi:
    """The API, bound to one base URL and one httpx client."""

    client: httpx.Client
    base: str
    retry: RetryPolicy

    # ── plumbing ────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _read(self, method: str, path: str, *, headers: dict[str, str] | None = None, json: Any = None) -> Any:
        """A read or an idempotent call: retried on transport errors and 429/5xx."""

        def once() -> Any:
            response = self.client.request(
                method,
                self._url(path),
                headers=headers,
                json=json,
                timeout=GET_TIMEOUT if json is None else POST_TIMEOUT,
            )
            if response.status_code in RETRYABLE_STATUS:
                raise RetryableStatus(response.status_code, retry_after_seconds(response))
            return self._answer(method, path, response)

        try:
            return self.retry.run(once)
        except RetryableStatus as exc:
            raise ApiError(method, path, exc.status, f"http_{exc.status}", "still failing after retries") from exc

    def _write_once(
        self, method: str, path: str, *, json: Any = None, headers: dict[str, str] | None = None, timeout: float
    ) -> Any:
        """A write that may take effect before its answer arrives: sent ONCE.

        A transport error is an `UnknownOutcome`, and so is a 5xx that is not
        the app's own answer — Render's or Vercel's gateway giving up, which
        carries no Orizon error envelope — and the one app code that SAYS the
        outcome is unknown (`refund_unconfirmed`). Every other status carrying
        an envelope code is the server's definitive answer, raised as an
        `ApiError`: a 503 `capacity_exhausted` minted no task, a 502
        `refund_failed` moved nothing.
        """
        try:
            response = self.client.request(method, self._url(path), json=json, headers=headers, timeout=timeout)
        except httpx.TransportError as exc:
            raise UnknownOutcome(method, path, type(exc).__name__) from exc
        if response.status_code >= 500:
            code, _message, body = _envelope(response)
            app_answered = isinstance(body, dict) and isinstance(body.get("error"), dict)
            if not app_answered or code in UNKNOWN_OUTCOME_CODES:
                raise UnknownOutcome(method, path, f"HTTP {response.status_code} {code}")
        return self._answer(method, path, response)

    def _answer(self, method: str, path: str, response: httpx.Response) -> Any:
        if response.is_success:
            return response.json()
        code, message, body = _envelope(response)
        raise ApiError(method, path, response.status_code, code, message, body)

    # ── meta ────────────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        """GET /api/health — app/routers/health.py:48; components/backend-warmup.tsx."""
        response = self.client.get(self._url("/api/health"), timeout=GET_TIMEOUT)
        return self._answer("GET", "/api/health", response)

    def readiness(self) -> dict[str, Any] | None:
        """GET /readiness — root-level, so best-effort: None when unreachable."""
        try:
            response = self.client.get(self._url("/readiness"), timeout=GET_TIMEOUT)
        except httpx.TransportError:
            return None
        if response.status_code not in (200, 503):
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def network(self) -> dict[str, Any]:
        """GET /api/stellar/network — app/routers/stellar.py:190 (NetworkInfo);
        lib/api.ts getStellarNetwork. network_passphrase, rpc_url, asset,
        asset_sac, contracts{agent_registry, reputation_ledger, payment_escrow,
        attestation_registry}."""
        return self._read("GET", "/api/stellar/network")

    def agents(self) -> list[dict[str, Any]]:
        """GET /api/agents — app/routers/agents.py:45 (list[Agent]); lib/api.ts listAgents."""
        return self._read("GET", "/api/agents")

    def registry_agent(self, agent_id: str) -> dict[str, Any]:
        """GET /api/stellar/agent/{id} — app/routers/stellar.py:220. A LIVE
        AgentRegistry.get, whose `owner` is the account a payout lands in."""
        return self._read("GET", f"/api/stellar/agent/{agent_id}")

    def reputation_batch(self) -> dict[str, Any]:
        """GET /api/stellar/reputation — app/routers/stellar.py:335 (ReputationBatch);
        lib/api.ts listReputation, which is what app/app/agents/page.tsx:54
        reads. So a snapshot taken here is the score the agents page shows."""
        return self._read("GET", "/api/stellar/reputation")

    # ── the buyer's flow ────────────────────────────────────────
    def decompose(self, intent: str) -> dict[str, Any]:
        """POST /api/orchestrator/decompose {intent} — app/routers/orchestrator.py:36,
        DecomposeRequest/DecomposeResponse at app/schemas.py:280-322; lib/api.ts
        decompose. Retried: a lost answer costs one unused plan, never money."""
        return self._read("POST", "/api/orchestrator/decompose", json={"intent": intent})

    def build_authorize(self, payer: str, max_amount_usdc: float, ttl_seconds: int, agent_id: str) -> dict[str, Any]:
        """POST /api/stellar/build/authorize {payer, agent_id, max_amount_usdc,
        ttl_seconds} -> {xdr, expires_at} — app/routers/stellar.py:707-743;
        lib/api.ts buildAuthorize as execution-plan.tsx:124-129 calls it.
        Retried: it builds an unsigned envelope and nothing else."""
        body = {"payer": payer, "agent_id": agent_id, "max_amount_usdc": max_amount_usdc, "ttl_seconds": ttl_seconds}
        return self._read("POST", "/api/stellar/build/authorize", json=body)

    def submit(self, signed_xdr: str) -> dict[str, Any]:
        """POST /api/stellar/submit {signed_xdr} -> {hash, status, ledger,
        return_value, diagnostic, explorer} or {hash, status: "timeout"} —
        app/routers/stellar.py:740-771 and app/stellar/client.py:829-843.
        NEVER retried. A 400 `submit_failed` is also unknown: the server raises
        it both for an envelope refused before the send and for a poll that
        failed after it (client.py:803-826), and the two cannot be told apart."""
        try:
            return self._write_once(
                "POST", "/api/stellar/submit", json={"signed_xdr": signed_xdr}, timeout=POST_TIMEOUT
            )
        except ApiError as exc:
            if exc.code == "submit_failed":
                raise UnknownOutcome("POST", "/api/stellar/submit", "submit_failed") from exc
            raise

    def execute(self, plan_id: str, auth_id_hex: str, payer: str) -> dict[str, Any]:
        """POST /api/orchestrator/execute {plan_id, auth_id_hex, payer} ->
        {task_id, read_token} — app/routers/orchestrator.py:72-83, ExecuteRequest
        at app/schemas.py:325-330. The auth id and payer come from the
        AUTHORIZE result, exactly as execution-plan.tsx:148-158 passes them;
        execute reads nothing from the chain itself. NEVER retried: a second
        call starts a second workflow against the same authorization."""
        body = {"plan_id": plan_id, "auth_id_hex": auth_id_hex, "payer": payer}
        return self._write_once("POST", "/api/orchestrator/execute", json=body, timeout=POST_TIMEOUT)

    def task(self, task_id: str, token: str | None) -> dict[str, Any]:
        """GET /api/tasks/{id} with X-Task-Token — app/routers/tasks.py:39,
        guarded by app/task_auth.py:250 require_task_read; lib/api.ts
        taskAuthHeaders sends the token the same way."""
        return self._read("GET", f"/api/tasks/{task_id}", headers=_token(token))

    def trace(self, task_id: str, token: str | None) -> list[dict[str, Any]]:
        """GET /api/trace/{id} with X-Task-Token -> [{t, level, msg}] —
        app/routers/trace.py:17; lib/api.ts getTrace (the polling fallback the
        console uses when the SSE stream is down)."""
        return self._read("GET", f"/api/trace/{task_id}", headers=_token(token))

    def task_disputes(self, task_id: str, token: str | None, grant: str | None = None) -> dict[str, Any]:
        """GET /api/tasks/{id}/disputes -> {task_id, window_closes_at, now,
        settlement, disputes} — app/routers/disputes.py:899-1030; lib/disputes.ts
        getTaskDisputes. The settlement carries job_id_hex, payer, charge_tx,
        proof_tx and each step's agent, price and delivered flag."""
        headers = dict(_token(token) or {})
        if grant:
            headers[DISPUTE_READ_GRANT_HEADER] = grant
        return self._read("GET", f"/api/tasks/{task_id}/disputes", headers=headers or None)

    # ── the dispute ─────────────────────────────────────────────
    def dispute_challenge(self, job_id_hex: str, step_index: int) -> dict[str, Any]:
        """POST /api/disputes/challenge {job_id_hex, step_index} -> {message,
        nonce, expires_at} — app/routers/disputes.py:618-659; lib/disputes.ts
        createDisputeChallenge. Retried: the mint is idempotent inside its
        window (a live challenge comes back as is)."""
        body = {"job_id_hex": job_id_hex, "step_index": step_index}
        return self._read("POST", "/api/disputes/challenge", json=body)

    def open_dispute(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/disputes {job_id_hex, step_index, reason, payer, nonce,
        signature_b64} -> DisputeResponse — app/routers/disputes.py:775-831;
        lib/disputes.ts openDispute. A 409 `duplicate_dispute` carries the
        original dispute in its body (`_duplicate_envelope`), which is returned
        as the answer: the step already has its dispute."""
        try:
            return self._write_once("POST", "/api/disputes", json=body, timeout=POST_TIMEOUT)
        except ApiError as exc:
            if exc.status == 409 and exc.code == "duplicate_dispute" and isinstance(exc.body, dict):
                existing = exc.body.get("dispute")
                if isinstance(existing, dict):
                    return existing
            raise

    def read_challenge(self, task_id: str) -> dict[str, Any]:
        """POST /api/disputes/read-challenge {task_id} -> {nonce, message,
        expires_at} — app/routers/disputes.py:695 (D-067). Absent on a backend
        older than D-067, which answers 404/405."""
        return self._read("POST", "/api/disputes/read-challenge", json={"task_id": task_id})

    def read_grant(self, task_id: str, nonce: str, signature_b64: str) -> dict[str, Any]:
        """POST /api/disputes/read-grant {task_id, nonce, signature_b64} ->
        {grant, expires_at} — app/routers/disputes.py:722. The grant goes back
        as X-Dispute-Read-Grant and buys the dispute's free text, nothing else."""
        body = {"task_id": task_id, "nonce": nonce, "signature_b64": signature_b64}
        return self._write_once("POST", "/api/disputes/read-grant", json=body, timeout=POST_TIMEOUT)

    def dispute(self, dispute_id: str, *, grant: str | None = None) -> dict[str, Any]:
        """GET /api/disputes/{id} -> DisputeResponse (status, refund_tx,
        rating_tx, credited_usdc, rating_confirmed) — app/routers/disputes.py:834."""
        headers = {DISPUTE_READ_GRANT_HEADER: grant} if grant else None
        return self._read("GET", f"/api/disputes/{dispute_id}", headers=headers)

    def uphold(self, dispute_id: str, api_key: str) -> dict[str, Any]:
        """POST /api/disputes/{id}/uphold with X-API-Key — app/routers/disputes.py:1033-1092,
        guarded by require_adjudicator. NEVER retried: 504 `refund_unconfirmed`
        means a transfer is on the network, and so may any dropped answer."""
        return self._write_once(
            "POST",
            f"/api/disputes/{dispute_id}/uphold",
            headers={API_KEY_HEADER: api_key},
            timeout=UPHOLD_TIMEOUT,
        )


def _token(token: str | None) -> dict[str, str] | None:
    return {TASK_TOKEN_HEADER: token} if token else None

"""The buyer's key, used the two ways the wallet uses it.

1. **The authorize envelope** — `lib/wallet.tsx` `signXdr` hands the prepared
   XDR from `/stellar/build/authorize` to the wallet kit's `signTransaction`
   with the network passphrase, and submits what comes back. Here the buyer
   keypair signs the same envelope under the same passphrase. BEFORE it signs,
   the envelope is decoded and checked to be exactly the call that was asked
   for — one `PaymentEscrow.authorize(payer, "orizon_batch", max, expiry)`
   from the buyer's own account on the network's escrow — because a key that
   signs whatever the server hands it is a key the server controls.

2. **The dispute and read challenges** — `lib/wallet.tsx` `signMessage` calls
   the kit's `signMessage`, which Freighter implements as SEP-53:
   ed25519 over `sha256("Stellar Signed Message:\\n" + message)`, base64.
   `Keypair.sign_message` is the SDK's own SEP-53 implementation, the twin of
   the `Keypair.verify_message` the backend checks it with
   (`app/services/external_binding.py` `_signature_matches`). The message is
   signed VERBATIM as the server returned it, after the same shape check the
   frontend makes (`lib/disputes.ts` `createDisputeChallenge`,
   `lib/dispute-read-grant.ts` `createReadChallenge`), never rebuilt.

The secret is read from an environment variable the operator names, and it is
registered with the redactor the moment it is read.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from stellar_sdk import Address, Keypair, TransactionEnvelope, scval
from stellar_sdk.operation import InvokeHostFunction

from .redact import Redactor

_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


class SigningRefused(Exception):
    """The harness declined to sign: a missing key, or an envelope that is not
    the call it asked for. Nothing was signed."""


def usdc_to_stroops(amount: float) -> int:
    """`app/stellar/client.py` usdc_to_i128, i.e. `app/money.to_stroops`: the
    float's shortest decimal, half-to-even to the stroop (ADR 0015).
    Re-stated rather than imported; the suite pins the two together."""
    return int((Decimal(repr(float(amount))) * 10_000_000).to_integral_value(rounding=ROUND_HALF_EVEN))


def load_keypair(env_name: str, redactor: Redactor, environ: Mapping[str, str] | None = None) -> Keypair:
    """The keypair whose seed is in `$env_name`. The seed never appears in an
    error: a message names the variable, not its value."""
    env = os.environ if environ is None else environ
    secret = (env.get(env_name) or "").strip()
    if not secret:
        raise SigningRefused(f"${env_name} is not set; export the buyer's S... seed there")
    redactor.register(secret)
    try:
        return Keypair.from_secret(secret)
    except Exception as exc:  # the SDK raises several types; none may echo the value
        raise SigningRefused(f"${env_name} does not hold a valid Stellar secret seed") from exc


def load_api_key(env_name: str, redactor: Redactor, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    key = (env.get(env_name) or "").strip()
    if not key:
        raise SigningRefused(f"${env_name} is not set; export the operator API key there")
    redactor.register(key)
    return key


@dataclass(frozen=True)
class AuthorizeCall:
    """The one call an authorize envelope may carry."""

    escrow: str
    payer: str
    agent_id: str
    max_stroops: int


@dataclass(frozen=True)
class SignedEnvelope:
    signed_xdr: str
    tx_hash: str
    expires_at: int


def inspect_authorize(xdr: str, passphrase: str) -> dict[str, Any]:
    """Decode an authorize envelope into the facts `sign_authorize` checks."""
    env = TransactionEnvelope.from_xdr(xdr, passphrase)
    ops = env.transaction.operations
    if len(ops) != 1 or not isinstance(ops[0], InvokeHostFunction):
        raise SigningRefused(f"authorize envelope carries {len(ops)} operation(s), not one contract call")
    invoke = ops[0].host_function.invoke_contract
    if invoke is None:
        raise SigningRefused("authorize envelope's operation is not a contract invocation")
    args = [scval.to_native(a) for a in invoke.args]
    return {
        "source": env.transaction.source.account_id,
        "contract": Address.from_xdr_sc_address(invoke.contract_address).address,
        "function": invoke.function_name.sc_symbol.decode("utf-8"),
        "args": [a.address if isinstance(a, Address) else a for a in args],
    }


def sign_authorize(xdr: str, keypair: Keypair, passphrase: str, expect: AuthorizeCall) -> SignedEnvelope:
    """Sign the prepared authorize envelope, having checked it is `expect`."""
    facts = inspect_authorize(xdr, passphrase)
    args = facts["args"]
    problems = []
    if facts["source"] != keypair.public_key:
        problems.append(f"source {facts['source']} is not the buyer {keypair.public_key}")
    if facts["contract"] != expect.escrow:
        problems.append(f"contract {facts['contract']} is not the escrow {expect.escrow}")
    if facts["function"] != "authorize":
        problems.append(f"function {facts['function']!r} is not 'authorize'")
    if len(args) != 4:
        problems.append(f"{len(args)} arguments, expected 4")
    else:
        if args[0] != expect.payer:
            problems.append(f"payer {args[0]} is not {expect.payer}")
        if args[1] != expect.agent_id:
            problems.append(f"agent label {args[1]!r} is not {expect.agent_id!r}")
        if args[2] != expect.max_stroops:
            problems.append(f"max_amount {args[2]} stroops is not {expect.max_stroops}")
    if problems:
        raise SigningRefused("refusing to sign the authorize envelope: " + "; ".join(problems))
    env = TransactionEnvelope.from_xdr(xdr, passphrase)
    env.sign(keypair)
    return SignedEnvelope(signed_xdr=env.to_xdr(), tx_hash=env.hash_hex(), expires_at=int(args[3]))


def sign_message_b64(keypair: Keypair, message: str) -> str:
    """SEP-53 signature over `message`, base64 — what the wallet kit returns."""
    return base64.b64encode(keypair.sign_message(message)).decode("ascii")


def check_dispute_message(message: str, job_id_hex: str, step_index: int, nonce: str) -> None:
    """`lib/disputes.ts` createDisputeChallenge's check, verbatim: the domain,
    the `:{job}:{step}:` it names, the nonce it ends with. The version segment
    is left free, as the frontend leaves it."""
    if not (
        message.startswith("orizon-dispute:")
        and f":{job_id_hex}:{step_index}:" in message
        and message.endswith(f":{nonce}")
    ):
        raise SigningRefused(f"the dispute challenge does not address step {step_index} of job {job_id_hex}")


def check_read_message(message: str, task_id: str, nonce: str) -> None:
    """`lib/dispute-read-grant.ts` createReadChallenge's check: parsed segment
    by segment, `orizon-dispute-read:<version>:<task>:<nonce>`."""
    domain, _, rest = message.partition(":")
    _version, _, rest = rest.partition(":")
    task, _, tail = rest.partition(":")
    if domain != "orizon-dispute-read" or task != task_id or tail != nonce:
        raise SigningRefused(f"the read challenge does not address task {task_id}")


def auth_id_from_return_value(value: Any) -> str | None:
    """execution-plan.tsx `bytesToHex`: the 16-byte auth id a submit returns, as
    32 lowercase hex characters — from hex, base64 or a 16-element byte list."""
    if isinstance(value, str):
        if _HEX32.match(value):
            return value.lower()
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError:
            raw = b""
        if len(raw) == 16:
            return raw.hex()
    if isinstance(value, list) and len(value) == 16 and all(isinstance(b, int) and 0 <= b < 256 for b in value):
        return bytes(value).hex()
    return None

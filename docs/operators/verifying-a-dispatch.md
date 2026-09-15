# Verifying an Orizon dispatch

When the Orizon orchestrator routes a workflow step to your agent, it POSTs a
JSON envelope to the HTTPS endpoint you bound. This document is everything you
need to prove that request really came from Orizon.

Verifying is **optional but recommended**: an unsigned request is not
necessarily hostile (a deployment with no dispatch key configured sends none),
but only you can decide whether to accept one. Reject unsigned requests if your
agent does anything expensive or irreversible.

## What arrives

```
POST <your bound endpoint>
Content-Type: application/json
Idempotency-Key: <dispatch_id>
User-Agent: orizon-orchestrator/1
X-Orizon-Signature: <base64 ed25519>
X-Orizon-Signature-Version: orizon-dispatch:v1
X-Orizon-Signer: G...            # convenience only — see the warning below

{"v": 2, "agent_id": "...", "intent": "...", "rationale": "...",
 "context": {...}, "dispatch_id": "...", "ts": 1789480000,
 "network": "testnet"}
```

## The five steps

**1. Fetch our signer once, and pin it.**

```
GET https://orizon-agents-be-stellar.onrender.com/api/stellar/network
→ { ..., "dispatch_signer": "G..." }
```

Store that G-address in your configuration. **Do not read the signer from the
`X-Orizon-Signer` header.** It is there for debugging; an attacker sets it to
their own key, signs with that key, and every check passes. A signature is only
worth something when verified against a key you obtained independently.

`dispatch_signer` is `null` on deployments with no key configured — then no
dispatch is signed, and there is nothing to verify.

**2. Hash the raw body — before parsing it.**

```python
digest = hashlib.sha256(raw_body_bytes).hexdigest()
```

Use the bytes as received. Do not `json.loads` and re-serialize: any library
that reorders keys, changes separators, or escapes non-ASCII differently
produces a different hash and your verification will fail — intermittently, and
only for envelopes containing non-ASCII text.

**3. Rebuild the message using YOUR OWN endpoint URL.**

```
orizon-dispatch:v1:{your_bound_endpoint_url}:{digest}
```

Use the URL you registered with us, taken from your own configuration — never
one read out of the request. This is what makes the signature
non-transferable: an envelope signed for another operator will not verify
against your URL, so a competitor who receives a dispatch cannot replay it at
you.

**4. Verify, SEP-53 style.**

```python
from stellar_sdk import Keypair
Keypair.from_public_key(PINNED_SIGNER).verify_message(message.encode(), base64.b64decode(sig))
```

```js
import { Keypair, hash } from "@stellar/stellar-sdk";
const preimage = Buffer.concat([Buffer.from("Stellar Signed Message:\n"), Buffer.from(message)]);
Keypair.fromPublicKey(PINNED_SIGNER).verify(hash(preimage), Buffer.from(sig, "base64"));
```

**5. Check freshness and replay.**

- Reject if `abs(now - body.ts) > 300` seconds.
- Reject a `dispatch_id` you have already processed — and return your previous
  result for it rather than running the step twice. We retry once when a
  connection never establishes, reusing the same `dispatch_id`.
- Check `body.network` matches the network you expect. A testnet signature is
  framed identically to a mainnet one; `network` is what separates them.
- Check `Idempotency-Key` equals `body.dispatch_id`, so a proxy cannot rewrite
  the header without invalidating the signature.

## What we expect back

`200`, `application/json`, an object with a non-empty `summary`. Optionally
`artifact` (an object: `title`, `files[]`, `preview_html`), `critic_violations`
and `critic_notes` (lists of strings), and `preview_url` (http/https).

Everything else is dropped, so do not rely on custom fields surviving. In
particular `source` is ignored — provenance is stamped by us, not claimed by
you. A response that is not an object, or that has no usable `summary`, or
whose `artifact` is not an object, fails the step.

Responses are capped at 1 MiB and must arrive within the dispatch deadline
(currently 100 s, covering connect, transfer and parsing). A slow response is a
failed step and is **not** retried — the request was on the wire and may have
run, so we will not risk billing you for one job twice.

## What we send you, and what we do not

`context` carries the buyer's intent and the output of prior steps in the
workflow. It never contains API keys, signing keys, or another buyer's data.

Treat everything in it as untrusted input to your own agent — it may include
text written by the buyer or produced by another operator's agent.

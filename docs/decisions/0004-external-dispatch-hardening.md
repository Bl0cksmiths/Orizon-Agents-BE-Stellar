# ADR 0004 — External dispatch: envelope signing and the operator response contract

- **Status:** Accepted (story 2.02 / BLO-19), 2026-09-16
- **Deciders:** Danielle (lead)
- **Extends** ADR 0001 (Candidate A, external execution) by deciding the two
  things the 1.06 spike left as prose: *how an operator knows a dispatch is
  really ours*, and *what we are willing to accept back*.

## Context

Story 2.01 wired bound external agents into `execution_svc._run`. That means an
untrusted third party now chooses the bytes of `output`, and those bytes reach
billing, on-chain reputation, the SSE trace, and the buyer's artifact viewer.

Three premises in the 2.02 card did not survive contact with the code, and are
corrected here so nobody re-derives them:

1. **"The dispatch timeout sits inside the step budget."** It does not. `httpx`
   has no total-request timeout: `Timeout(110.0, connect=5.0)` resolves to
   `read=110, write=110, pool=110` — an *idle gap between reads*, not a
   duration. A operator trickling one byte every 109 s runs until
   `execution_svc`'s 120 s `asyncio.wait_for` ceiling, which is precisely the
   "ambiguous outer timeout" the worker docstring and ADR 0001 claim cannot
   happen. `TOTAL_TIMEOUT_SECONDS` is a misnomer. Both documents are corrected
   in this change.
2. **"Reuse `fence_user_input`."** Wrong primitive: it labels its block
   `USER_INPUT` and clamps at `MAX_INTENT_CHARS = 500`, which silently
   truncates a legitimate artifact. The general primitive is
   `fence_untrusted(text, *, label, max_chars)`. The card's real intent — do
   not invent a second mechanism — is satisfied by using it.
3. **The fence has no live consumer.** Nothing iterates `context` generically;
   every reader takes hard-coded keys (`kit`, `seo.brief`, `research.pro`,
   `design.figma`), and an external worker is keyed `external.{agent_id}`,
   which the agent-id charset makes uncollidable. AC-5 is therefore
   *forward-looking* defence, and is only load-bearing once something reads an
   external key. Stated plainly so a reviewer who checks does not conclude the
   control is pointless.

## D1 — The operator response contract

**Everything an operator returns is parsed into a new dict; nothing is passed
through.** `_parse` today returns the operator's whole object, so any key they
invent lands in `context` and is forwarded to every later operator. The
replacement allowlists, type-checks and clamps.

Bounds mirror the local path (`code_gen.CodeArtifact`) rather than inventing a
second scale — external output must not be allowed more room than our own
workers get.

Rejection is a **step** failure, never a run failure. That is the existing
`continue` discipline, and it is what stops one hostile response from denying
settlement to every honest agent in the plan.

`source` is **not** accepted from an operator. `synthetic_rating` awards 95/100
for `source == "baked"`; honouring an operator-supplied value is self-dealing
with up to 100 USDC of rating weight behind it.

External HTML artifacts go through `harden_artifact` like local ones. Today
only `code_gen` and `code_critic` call it, so an operator's `preview_html`
reaches the viewer's `srcDoc` with no injected CSP — the sandbox blocks parent
access but not outbound beaconing from inside the frame.

## D2 — Envelope signing

**A dedicated key.** `ORIZON_DISPATCH_SIGNING_KEY`, separate from
`STELLAR_SIGNING_KEY`. Reusing the money key is *cryptographically* safe —
SEP-53's `"Stellar Signed Message:\n"` prefix cannot collide with a transaction
or Soroban-auth preimage, both of which are domain-separated by network id —
but it is wrong operationally: it welds dispatch-key rotation to redeploying
PaymentEscrow, puts the settler seed on the outbound HTTP hot path, and leaves
dispatch unsigned on exactly the demo and read-only deployments an operator
integrates against first.

**Unset key ⇒ unsigned dispatch, one coalesced warning, never an exception.**
An unsigned dispatch is not our vulnerability; it is a trust gap the *operator*
is positioned to enforce by rejecting it. Failing closed would convert their
policy into our outage, and would contradict the dispatch path's fail-open
convention (ADR 0003 D5). The hermetic suite runs with no key at all.

**Sign SEP-53, never raw bytes.** `Keypair.sign()` has no domain separation;
`sign_message()` is the framing already verified inbound in 2.01.

**The signed message binds the destination:**

```
orizon-dispatch:v1:{endpoint_url}:{sha256_hex(body_bytes)}
```

The URL lives in the *message*, not the body, deliberately: the verifier must
supply their own endpoint URL, which makes cross-operator replay structurally
impossible rather than an optional field-comparison an operator can forget.
Without it, operator A can replay a fully-signed envelope at operator B, who
would verify it against our published key and conclude we dispatched to them.
This is ADR 0003 D3's lesson pointed outward.

**The body gains `ts` and `network`** (envelope `v: 2`). SEP-53's preimage
carries no network id — unlike transaction signing — so a testnet dispatch is
otherwise byte-identical in framing to a mainnet one.

**Serialize once, sign those exact bytes, send them.** `httpx`'s `json=` encodes
with `separators=(",", ":")` and `ensure_ascii=False`, which differs from
`json.dumps()` defaults. Signing `json.dumps(payload)` while sending
`json=payload` yields a signature over bytes the operator never receives — and
it breaks *only* when the payload contains non-ASCII, so every ASCII test
passes and production fails on the first accented character. Verified.

## Frozen interfaces

Signatures are frozen; implementations are not.

### `app/agents/workers/external_contract.py` (new)

```python
MAX_SUMMARY_CHARS   = 2_000
MAX_TITLE_CHARS     = 120
MAX_ARTIFACT_CHARS  = 120_000     # mirrors code_gen.MAX_ARTIFACT_CHARS
MAX_FILES           = 24
MAX_NOTES           = 16
MAX_NOTE_CHARS      = 500

OUTPUT_RULES: frozenset[str]      # machine-readable refusal reasons

class ExternalOutputError(ValueError):
    rule: str                     # one of OUTPUT_RULES

def parse_operator_output(raw: object) -> dict[str, Any]: ...
    # Allowlist + type-check + clamp. Returns a NEW dict; never passes an
    # operator key through. Raises ExternalOutputError. Pure, no I/O.
```

### `app/services/dispatch_signing.py` (new)

```python
DISPATCH_SIG_VERSION = "orizon-dispatch:v1"
SIGNATURE_HEADER     = "X-Orizon-Signature"
VERSION_HEADER       = "X-Orizon-Signature-Version"
SIGNER_HEADER        = "X-Orizon-Signer"   # convenience only — operators MUST pin

def dispatch_signer_address() -> str | None: ...
    # G-address, or None when unconfigured. Never raises.

def dispatch_message(endpoint_url: str, body: bytes) -> str: ...
    # orizon-dispatch:v1:{endpoint_url}:{sha256_hex(body)}

def sign_dispatch(endpoint_url: str, body: bytes) -> dict[str, str]: ...
    # Signature headers, or {} when unconfigured. Never raises.
```

### `app/config.py`

```python
orizon_dispatch_signing_key: str = ""
```

### Integration (owned by the integrator, not a lane)

`app/agents/workers/external_http.py` — serialize the body once, sign it, send
`content=body` with an explicit `Content-Type`, enforce a real monotonic
deadline across connect + stream + parse, and route the response through
`parse_operator_output`. Plus the `dispatch_signer` field on
`GET /api/stellar/network`, guarded so an unset key yields `None` rather than
raising.

## Out of scope, stated so it is not assumed

The whole accumulated `context` is still POSTed to every bound operator — the
buyer's intent and every prior agent's full output, with no redaction and no
outbound size cap. That is a real egress question and it is not response-side
hardening, so it is not fixed here. Tracked separately.

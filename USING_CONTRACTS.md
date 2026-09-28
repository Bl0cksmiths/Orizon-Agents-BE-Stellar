# Using the Orizon Smart Contracts

The Orizon backend talks to four Soroban contracts on Stellar testnet. This
doc walks you through each one, when it's called, who signs it, and the
matching HTTP route on this backend.

## The cast

| contract | role | signer | backend route |
| --- | --- | --- | --- |
| **AgentRegistry** | identity + price catalog | agent owner (user) | `POST /api/stellar/build/register-agent` (user signs) |
| **PaymentEscrow** | x402-style authorize → settle (v2) / charge (v1) | payer (user) + settler (backend) | `POST /api/stellar/build/authorize`; the run settles itself (v2), `POST /api/stellar/server/charge` is v1-only |
| **AttestationRegistry** | write-once job proof | sealer (backend) | `POST /api/stellar/server/seal` |
| **ReputationLedger** | rating aggregates | scorer (backend) | (backend-only; rating submission route TBD) |

Current deploy (testnet): see `/api/stellar/network` or `addresses.json`.

> **Escrow v2 (ADR 0010).** v1's `charge` can never settle (D-039). v2 takes
> the payer's `max_amount` into custody at `authorize` (same signature — the
> label is the plan id) and, at the end of a paid run, the backend submits ONE
> `settle(settler, auth_id, job_id, payouts)` paying each **delivered** step's
> operator its own price, returning the rest; nothing delivered is an empty
> `settle`, a full release. Only agents with an on-chain owner are paid — the
> seeded `agt_*` catalogue is not registered, so its steps go unpaid and the
> buyer keeps that share. The backend reads `version()` (missing = v1) and
> picks the path itself; `/readiness` shows which. The flow diagram and
> sections D and "charge wiring" below describe v1.

## The full job lifecycle

```
┌────────────────────────┐
│ 1. user visits /app    │
│    /orchestrator       │
└───────────┬────────────┘
            │ connect Freighter (already wired via ConnectWallet button)
            ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. user types intent → POST /api/orchestrator/decompose                │
│    (GPT-4o-mini returns plan with per-step USDC price)                 │
└───────────┬────────────────────────────────────────────────────────────┘
            │ frontend sums prices → max_amount
            ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. frontend → POST /api/stellar/build/authorize                        │
│    { payer: walletAddress, agent_id: "batch", max_amount_usdc, ttl }   │
│    backend returns unsigned XDR                                        │
│    frontend → Freighter.signTransaction(xdr)                           │
│    frontend → POST /api/stellar/submit { signed_xdr }                  │
│    → returns { hash, return_value: auth_id_bytes16 }                   │
└───────────┬────────────────────────────────────────────────────────────┘
            │ frontend now has auth_id
            ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 4. frontend → POST /api/orchestrator/execute { plan_id, auth_id_hex }  │
│    (execute_plan still runs agents, but for each step it now calls)    │
│       POST /api/stellar/server/charge { auth_id_hex, amount, job_id }  │
│    → each charge signed with STELLAR_SIGNING_KEY                       │
│    → each charge triggers USDC transfer from payer → agent owner       │
│    trace stream emits a `cost` line with the returned tx hash          │
└───────────┬────────────────────────────────────────────────────────────┘
            │
            ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 5. when all steps complete → backend → POST /api/stellar/server/seal   │
│    writes an AttestationRegistry entry with (agents, receipts, total)  │
│    trace stream emits the final `proof` line with the tx hash          │
└────────────────────────────────────────────────────────────────────────┘
```

## Try each contract manually (curl)

### A. AgentRegistry — view

```bash
curl -s http://localhost:8000/api/stellar/agent/copy_v3 | jq
```

Reads via Soroban RPC simulation — no signing, no fees. If the id was
never registered on-chain yet, you'll get a 404 with `NotFound`.

### B. AgentRegistry — register (user signs)

1. Build unsigned XDR:
   ```bash
   curl -s -X POST http://localhost:8000/api/stellar/build/register-agent \
     -H 'content-type: application/json' \
     -d '{
       "owner": "GCO4EYZFPFKAXDFQFNG3MVCCK6VEJ2XYODYZCNKSJOWHG6PEDWLAKVGY",
       "agent_id": "copy_v3",
       "name": "copywrite.v3",
       "skills": ["copy","seo","en"],
       "price_usdc": 0.012
     }'
   # → { "xdr": "AAAAAg..." }
   ```
2. Sign with Freighter (in browser):
   ```ts
   import { signTransaction } from "@stellar/freighter-api";
   const { signedTxXdr } = await signTransaction(xdr, {
     networkPassphrase: "Test SDF Network ; September 2015",
     address: myAddress,
   });
   ```
3. Submit:
   ```bash
   curl -s -X POST http://localhost:8000/api/stellar/submit \
     -H 'content-type: application/json' \
     -d "{\"signed_xdr\":\"$SIGNED_XDR\"}"
   # → { "hash": "...", "status": "SUCCESS", "return_value": null }
   ```

### C. PaymentEscrow — authorize (user signs)

```bash
curl -s -X POST http://localhost:8000/api/stellar/build/authorize \
  -H 'content-type: application/json' \
  -d '{
    "payer": "GCO4EYZFPFKAXDFQFNG3MVCCK6VEJ2XYODYZCNKSJOWHG6PEDWLAKVGY",
    "agent_id": "copy_v3",
    "max_amount_usdc": 0.5,
    "ttl_seconds": 300
  }'
# → { "xdr": "...", "expires_at": 1761234567 }
```

Sign via Freighter, submit via `/api/stellar/submit`. The `return_value`
is the 16-byte `auth_id` (base64/hex depending on encoder) — store it.

### D. PaymentEscrow — charge (backend signs)

Only possible once `STELLAR_SIGNING_KEY` is set in `.env` (never commit!).

```bash
curl -s -X POST http://localhost:8000/api/stellar/server/charge \
  -H 'content-type: application/json' \
  -d '{
    "auth_id_hex": "92441bc1553690f4425463460cc537f2",
    "amount_usdc": 0.012,
    "job_id_hex": "07070707070707070707070707070707"
  }'
# → { "hash": "...", "status": "SUCCESS", "result": <receipt_id bytes> }
```

### E. AttestationRegistry — seal (backend signs)

```bash
curl -s -X POST http://localhost:8000/api/stellar/server/seal \
  -H 'content-type: application/json' \
  -d '{
    "job_id_hex": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "orchestrator": "GCO4...KVGY",
    "intent_hash_hex": "'$(echo -n "build me a landing page" | sha256sum | awk '{print $1}')'",
    "agents": ["seo_b","copy_v3"],
    "receipts_hex": ["<receipt1_hex>","<receipt2_hex>"],
    "total_spent_usdc": 0.021
  }'
```

### F. AttestationRegistry — read

```bash
curl -s http://localhost:8000/api/stellar/attestation/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa | jq
```

## Wiring the on-chain path into `execute_plan`

The existing `app/services/execution_svc.py` simulates `cost` and `proof`
lines. To flip it on-chain, replace the two `_emit(..., "cost", ...)` and
`_emit(..., "proof", ...)` blocks with calls to
`sc.invoke_with_server_key(...)` and broadcast the real `tx hash` in the
trace message. Pseudocode:

```python
from ..stellar import client as sc

# inside the per-step loop, after worker.run(...) returns:
receipt = sc.invoke_with_server_key(
    sc.contract_ids().payment_escrow,
    "charge",
    [
        sc.addr(settler_public_key),
        sc.bytes16(auth_id),
        sc.i128(sc.usdc_to_i128(step.est_price_usdc)),
        sc.bytes16(job_id),
    ],
)
await _emit(task_id, start, "cost", f"x402 charge tx {receipt['hash'][:10]}…  {step.est_price_usdc:.3f} USDC settled")
```

`STELLAR_SIGNING_KEY` must be present and fund the `settler` role on
PaymentEscrow (wired at deploy time — currently = `admin`).

## FE → Freighter-signed flow (reference snippet)

```ts
import { useWallet } from "@/lib/wallet";

const { address, signXdr } = useWallet();

// 1. ask backend for unsigned XDR
const { xdr, expires_at } = await fetch("/api/stellar/build/authorize", {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({
    payer: address,
    agent_id: "copy_v3",
    max_amount_usdc: 0.5,
    ttl_seconds: 300,
  }),
}).then((r) => r.json());

// 2. Freighter prompts user → returns signed XDR
const signedXdr = await signXdr(xdr);

// 3. backend broadcasts
const res = await fetch("/api/stellar/submit", {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ signed_xdr: signedXdr }),
}).then((r) => r.json());
// → { hash, status, return_value: auth_id }
```

## Role matrix

| role | set at deploy | can be rotated | purpose |
| --- | --- | --- | --- |
| `admin` | `__constructor(admin, …)` on each contract | yes (via `set_*` methods) | emergency / config |
| `scorer` (ReputationLedger) | deploy arg | `set_scorer(admin-sig)` | write ratings |
| `settler` (PaymentEscrow) | deploy arg | v1: re-deploy · v2: `set_settler(admin-sig)` | call `charge` (v1) / `settle` (v2) |
| `sealer` (AttestationRegistry) | deploy arg | `set_sealer(admin-sig)` | call `seal` |

Today all four roles are the single `admin` keypair for simplicity. In prod
split into distinct keys: admin = cold wallet, scorer/settler/sealer = hot
signing service.

## Error codes

Every contract uses a shared error enum (see `crates/shared/src/lib.rs::codes`):

| code | meaning |
| --- | --- |
| 1 | Unauthorized (role check failed) |
| 2 | NotFound (missing row) |
| 3 | AlreadyExists (write-once violated) |
| 4 | Expired (authorization TTL passed) |
| 5 | Insufficient (charge, or v2 settle's payouts, would exceed max) |
| 6 | Revoked |
| 7 | Replay (rating already submitted for this job) |
| 8 | Inactive (agent deactivated) |

The backend surfaces Soroban errors as HTTP 400 / 502. Look at `status.error`
in the SSE trace for the decoded code.

## Don'ts

- Don't put `STELLAR_SIGNING_KEY` in git. It's blank in `.env.example`
  intentionally; inject via host secrets (`fly secrets set ...`, Render
  env vars).
- Don't re-use a seed phrase that's been pasted anywhere — ever. For
  testnet that's fine, for mainnet it's fatal.
- Don't assume `return_value` is human-readable — it's a Soroban `ScVal`.
  The backend converts with `stellar_sdk.scval.to_native`, which returns
  a Python dict/list/int/bytes — already JSON-safe.

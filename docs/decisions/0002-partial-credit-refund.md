# ADR 0002 — Partial-credit refund mechanism

- **Status:** Accepted (spike 4.01 / BLO-29), 2026-09-12 — ratify with the Chapter Lead at the next check-in
- **Deciders:** Danielle (lead)
- **Gates:** all of Milestone 3 (Epic 4 — Dispute Window & Partial-Credit Refund)

## Context

SOW §4.1 D3 says the settler *"honors a partial-credit refund against the buyer's
authorization envelope."* That mechanism does not exist. `PaymentEscrow.charge`
transfers USDC **directly payer → agent owner** and the contract never takes
custody; its ABI (`authorize`, `charge`, `revoke`, `authorization`, `receipt`,
`settler`) has **no `refund` and no `set_settler`**, and the settler key is
write-once at construction. So a dispute refund cannot be a reversal of the
charge — there is nothing held to reverse. It must be a **new transfer from
somewhere else.**

The dispute window is post-seal, so the charge has already landed. And the
settler already auto-rates every settled step under `Rated(agent_id, job_id)`,
whose replay guard fires **before** `ReputationLedger.submit` reads `kind` — so a
dispute rating on the same pair is rejected with `Error::Replay` (**R12**).

## Decision

**Option A — settler-funded platform credit.** On an upheld dispute the settler
sends the credited amount to the buyer over the asset SAC
(`transfer(settler → buyer)`) — a **platform credit, never a clawback** from the
agent. Implemented in `app/services/refund_svc.py`:

- `execute_refund(buyer, amount)` → `invoke_with_server_key_async(asset_sac,
  "transfer", [settler, buyer, i128(usdc)])`, signed by the server (settler) key.
- **R12:** `record_dispute_rating(...)` writes the dispute under
  `dispute_job_id(job_id) = sha256(job_id || "dispute")[:16]` — a distinct but
  deterministic id, so it clears the replay guard while staying linkable to the
  disputed job.

No contract change; ships inside the sprint; produces the two artifacts SOW §6.1
requires (a dispute tx and a partial-refund tx on Stellar Expert testnet).

### Refund policy (stated, not case-by-case)

The credit is a **fraction of the disputed step's settled charge** (default
`1.0` = the full step price; `credited_amount_usdc`). A dispute credits the
buyer for the step that failed, which is a *partial* refund of the whole
workflow. Buyer and operator both know the terms in advance.

### Trust model (disclosed everywhere — SOW §3.8 standard)

- The **platform funds** the credit; the disputed agent's only consequence is
  **reputational** (the low dispute rating), never a seizure of its funds.
- The **platform adjudicates** the dispute — there is **no on-chain
  arbitration** in this sprint. This is a permissioned, trusted operation.

### Why not B — new `refund` entrypoint on `PaymentEscrow`

Economically cleanest and genuinely on-chain, but it requires changing the
contract, redeploying, migrating live authorizations, and **re-publishing the
four testnet contract ids that SOW §6.1 lists as submitted evidence** — barred
without the Chapter Lead's agreement — plus the disputed agent's cooperation.
~12–16 h against a 32 h epic. Rejected for the sprint.

### Why not C — escrow hold with delayed release

`charge` moves funds into contract custody, released after the window closes.
Architecturally correct, makes refunds trivial — but it is a redesign of the
payment path that breaks the existing x402 flow. Roadmap, not sprint. Rejected.

## Proof (the AC "a real refund has landed on testnet") — DONE

A real refund landed on testnet on 2026-09-12: tx
`9b8ffaa44b2b966e4c3f1ab581f4203a30d282901ba3b231a578e46d8f919a68` (ledger
4635132, `successful: true` on Horizon), the settler crediting the 1.05
registrant `GBI2I3WL…` over the asset SAC. Open it at
`https://stellar.expert/explorer/testnet/tx/9b8ffaa44b2b966e4c3f1ab581f4203a30d282901ba3b231a578e46d8f919a68`.

The hash and that URL are the whole of what a reviewer needs, so they are
inlined here rather than linked to. The longer write-up lives in
`docs/evidence/4.01-refund-testnet.md`, which is **deliberately not in this
repo**: `/docs/evidence/*.md` is gitignored, because evidence write-ups travel
with the bundle rather than with the code. Anyone who cloned this will find
nothing at that path, which is why it is named and not linked.

The mechanism is unit-tested in `tests/test_refund_svc.py` (2 tests). The R12
derivation is no longer among them: ADR 0007 D5's amendment supersedes the
`refund_svc` helpers that held it, and the derivation now lives in
`app/services/dispute_rating.py`, pinned by golden vectors in
`tests/test_dispute_job_id.py` (13 tests). The live tx was produced by a signed
settler transaction from the **funded testnet key** — run:

```
python scripts/prototype_refund.py --buyer <G...> --amount 0.054
```

with the testnet `STELLAR_SIGNING_KEY` set (the settler, funded via friendbot).
It prints the refund tx hash → capture it on Stellar Expert for the evidence
bundle. This is the one step that cannot be run from CI.

## Epic 4 impact (estimate review)

The mechanism is settled and the money-moving core is prototyped + tested, so the
**32 h estimate holds** — 4.02 (dispute window state + endpoint), 4.03 (wire
`execute_refund` into the settler path), 4.04 (the dispute rating via the derived
id — now unblocked), 4.05/4.06 (FE dispute action + receipt). No contract
redeploy required. Also feeds **5.06**: correct the Litepaper §6.1 claim that the
settler key can be rotated (it cannot — write-once).

> **Amended 2026-09-21 (story 4.02 / BLO-30).** R12's resolution is no longer
> only a mechanism with a unit test behind it — it is in force on the path a
> dispute actually takes. Settlement now records the payer, the job id, the
> per-step charge and the window's closing time durably
> (`app/services/dispute_store.py`), and a dispute is opened against that
> record, so the pair `(agent_id, refund_svc.dispute_job_id(job_id))` is
> computable from the stored dispute alone. Story 4.04 must write the dispute
> rating under that derived id — `refund_svc.record_dispute_rating` does — and
> not under the settled job id, which the ledger's replay guard rejects.
> The window, the wallet-signature proof of payer and what settlement has to
> remember are decided in
> [`0007-dispute-window.md`](0007-dispute-window.md); the buyer- and
> operator-facing version is [`docs/disputes.md`](../disputes.md).

> **Amended 2026-09-21 (story 4.04 / BLO-32).** The R12 derivation chosen
> above — `dispute_job_id(job_id) = sha256(job_id || "dispute")[:16]` — is
> **superseded** by ADR 0009 D1, before a single rating was written under it.
> It hashed the job alone, while the ledger's replay guard is keyed on
> `(agent_id, job_id)`: when one agent served two steps of a job, a second
> upheld dispute derived the same id as the first and would have been refused
> as a replay of it. It also hid the link this ADR claimed for it — a reviewer
> on Stellar Expert could tie a dispute rating to its job only by knowing the
> formula and recomputing it. A dispute rating is now written under
> `job_id[:8] ‖ sha256(job_id ‖ "orizon-dispute:v1" ‖ step)[:8]`
> (`app/services/dispute_rating.py`): unique per disputed step, and carrying
> the sealed job's own first half, so the link is visible rather than computed.
> The decision itself stands — a derived id that clears the replay guard, with
> no contract change — and only the formula moved. The helpers named in this
> ADR's Decision and in the 4.02 amendment above are **retired** from
> `refund_svc`, with the tests that pinned them: `dispute_job_id(job_id)` is
> replaced by `dispute_rating.dispute_job_id(job_id, step_index)`,
> `record_dispute_rating(...)` by
> `dispute_rating.submit_dispute_rating(dispute, settlement)`, and
> `DISPUTE_RATING` by `dispute_rating.DISPUTE_RATING`, still 10. Why the old
> formula could be replaced at no cost, and why the new one can never change
> once a rating lands, are in [`0009-dispute-rating.md`](0009-dispute-rating.md).

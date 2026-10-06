# ADR 0016 — The built-in agents are registered on-chain to a platform treasury

- **Status:** Accepted, 2026-10-06
- **Deciders:** Danielle (lead)
- **Resolves** ADR 0015 finding P8 (no platform agent can be paid on the live
  escrow). **Builds on** ADR 0010 (escrow v2 pays `owner_of` per payout) and
  ADR 0012 (the team register decides who is external).

## Context

Escrow v2 pays each payout to `AgentRegistry.owner_of(agent_id)`. The registry
it reads, `CAPHXWU5…BJ3GQ`, holds none of the twelve built-in ids (`agt_01h8`
… `agt_12r0`): `owner_of` answers `Error(Contract, #2)` for every one of them
(re-read for all twelve on 2026-10-06; `list_ids` has 1 079 ids, none
starting `agt_`). ADR 0010 D2 leaves such a step unpaid rather than revert
the whole settle, and plans now route only to built-in agents, so every paid
run returns its whole authorization and the platform is never paid.

Owner decision: register the twelve built-in ids on that registry with a new
platform treasury wallet as their owner, so a delivered built-in step settles
to the treasury.

### What the contracts do (contracts repo, origin/main `ef007ad`)

**`AgentRegistry.register(owner, id, name, skills, price)`**
(`contract/agent-registry/src/lib.rs`; the deployed spec, read with
`stellar contract info interface`, has the same signature):

- `owner.require_auth()` — the owner signs; nobody else, admin included, can
  register for it. Registration is otherwise permissionless.
- `id: Symbol` — `[A-Za-z0-9_]{1,32}`. **Write-once**: an id that exists is
  `Error #3 AlreadyExists`, and there is no transfer, delete or re-register
  entrypoint. Whoever registers an id first owns it for good.
- `name: String`, `skills: Vec<Symbol>`, `price: i128` — **not validated at
  all** (no length, sign or range check). A skill is a Symbol, so it cannot
  contain a space: the seeded `translate.42` skill `"42 langs"` must go
  on-chain as `42_langs`.
- No endpoint field; `active` is set `true`, `registered_at` to the ledger
  time. The id is appended to the instance-stored `Ids` vector.
- Emits `("regd", id) → owner`.
- `update_price` and `set_active` are owner-signed; `owner_of` and `get` do
  not check `active`.

**Escrow v2 `settle(caller, auth_id, job_id, payouts)`**
(`contract/payment-escrow/src/lib.rs`): the payee is resolved **at settle
time**, per payout, by a cross-contract `owner_of(p.agent_id)`; `authorize`
names no agent (the console's label is `orizon_batch`). The escrow never reads
the registry's price — each payout's amount is what the settler sends — and an
id the registry does not hold traps the whole settle. Because the owner is
write-once, the owner read before a settle and the one inside it cannot
disagree.

### What the backend does with an on-chain `agt_*` id, as audited

| Consumer | Once the twelve exist on-chain | Verdict |
| --- | --- | --- |
| `registry_sync._pass` (the mirror) | Skips every `agt_` id before the batch read, so the seeded `Agent` (`source="seeded"`, `owner=None`, `real=True`) is never overwritten. It logs the id once as a WARNING that reads like a squat. | Safe; the warning is wrong for our own registration, and nothing checks the on-chain record (B1, B2) |
| `client.read_agent_records` | Only asked for non-`agt_` ids. | Safe |
| `GET /api/agents`, `state.agents` | Unchanged: 12 seeded + the non-`agt_` mirror. | Safe |
| Overview `registered` / `onchain` / `seeded` | Counted over `state.agents` by `source`, so the twelve stay `seeded` and are not also counted `onchain`. | Safe — no double count |
| Overview `external`, `operators.external_wallets` | Taken over the on-chain mirror only, which never holds `agt_`. | Safe |
| Adoption (`adoption_svc`) | `onchain_mirror()` and the `list_ids` cross-check both drop `SEEDED_PREFIX`; the charge window scans only the external agents' charges. A treasury-paid `charged` event is never an external settlement. | Safe; the treasury still belongs in the register so a reader can see whose it is (B3) |
| `plannable()`, `_with_executor`, `resolve_worker` | Decided by `get_worker(agent_id)`: a local worker wins over any binding, so `agt_*` stays `built_in` with its tier's model. | Safe |
| Pricing (`PlanStep.price_stroops`) | Frozen from the seeded agent's price; the on-chain price of an `agt_` id is never read, and the escrow pays the plan's amount, not the registry's. | Safe; the on-chain price should still equal the plan's, and nothing says when it does not (B2) |
| `execution_svc._unpaid_agents` → `_payout_plan` → `settle` | `owner_of` now answers, so each delivered built-in step is paid its `price_stroops` — to **whoever owns the id**. | **Change needed**: `register` is permissionless and write-once, so anyone who registers an `agt_` id we have not would be paid for our work. Pay a built-in step only to the treasury (B4) |
| Settlement record and receipt | Keep amount, receipt id and unpaid reason per step, not who was paid. | **Change needed**: record and show the payee (B5) |
| Trace / unpaid notices | Only "no confirmed on-chain owner". | Extended with the not-the-treasury reason (B4) |
| `settlement_svc` per-agent earnings | `owner_of(agt_…)` is the treasury; a buyer-funded payout counts as that agent's revenue, which it is. | Safe |
| Seal (`AttestationRegistry.seal`) | Names the paid agents with their receipts; a built-in run becomes `paid` instead of `delivery_only`. | Safe — intended |
| Disputes / credits | A credit is computed from what the step was paid (ADR 0010 D5) and funded by the platform's refund key, as before. | Safe |
| Register route (`/build/register-agent`) | Refuses `agt_` ids (`id_reserved`) before any read. | Safe |
| Management preflight (`_agent_exists`) | Now finds `agt_` ids, so an update-price / set-active XDR can be built for one; only the treasury key can sign it. | Safe |
| Binding | An `agt_` binding could only be signed by the treasury and is never used: the local worker wins. | Safe |
| Reputation | Keyed by agent id on the ReputationLedger, not the registry. | Unaffected |

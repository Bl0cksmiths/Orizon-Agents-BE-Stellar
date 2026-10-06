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

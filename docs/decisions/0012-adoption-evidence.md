# ADR 0012 — Adoption evidence: SOW §6.3 computed from on-chain facts, with our own wallets declared

- **Status:** Accepted (story 5.02 / BLO-36), 2026-09-28
- **Deciders:** Danielle (lead)
- **Builds on** ADR 0010 (escrow v2: one `charged` event per payout, topic'd
  with the agent actually paid) and the settlement evidence in
  `app/services/settlement_svc.py`, whose exclusions this reuses unchanged.
  This decides **what counts as an external operator**, **how the team's own
  wallets are declared and excluded**, **what counts as a settled external
  workflow**, and **what the report says when it could not look**.

## Context

SOW §6.3 asks for at least two externally operated agents, at least two unique
operator wallets, and at least three workflows routed to external agents and
settled on testnet, each with a verifiable charge transaction. Story 5.02's
rule: *"External means external. Wallets controlled by the Blocksmiths do not
count toward the metric under any framing."*

Nothing computed these numbers. The honest answer on 2026-09-28 is zero: every
on-chain agent is owned by a team wallet. A number like this is only worth
anything if a reviewer can check it without trusting us, so the answer has to
come from the chain and show its working.

## Decision

### D1. One public, read-only route

`GET /api/ecosystem/adoption` returns the targets, the totals, whether each is
met, every external operator with their agents and settled workflows, every
excluded owner with the reason, and whether the answer is degraded. Every owner
links to its Stellar Expert account page and every settled workflow to its
transaction. It spends no key and moves nothing. It sits under the service-wide
`RateLimitMiddleware` like the other public reads, and the whole report is
cached single-flight for about 30 s (`app/stellar/cache.py`).

The shape is frozen, because the frontend codes against it. The fields the
brief did not spell out are `payer_team_role` on each settled workflow (D5)
and the top-level `window_days` (D8).

### D2. The team register: a committed, public declaration

`app/data/team_wallets.json` lists every account the team controls, each with
its `role` and the `evidence` for it. These are public keys, not secrets. The
register is validated when the module is imported, so a malformed one stops
the service booting with a message naming the entry. It refuses:

- an address that is not a valid G-strkey;
- a duplicate;
- an empty `role` or `evidence`;
- any other key;
- an empty list.

Strict on purpose: each of these mistakes would silently stop one of our
wallets being recognised, and our own agent would count as someone else's.

**The rule for the team:** add a wallet to the register the moment it is
created, spike keys and throwaway QA keys included. A key we generated and
forgot is counted as external until it is declared.

**Disclosure: three entries rest on our own records.** `GA5LEGIR…MQ2M`, which
owns `spike_97437`, `GDWE6IDZ…IWRX` and `GB4K6YRH…AYKK` are the random keys of
the team's x402 escrow spike on 2026-09-15. Each entry cites what the chain
shows:

- friendbot created `GA5LEGIR…` and `GDWE6IDZ…` five seconds apart;
- `GA5LEGIR…` registered `spike_97437`;
- `GDWE6IDZ…` then authorized a payment against `spike_97437`;
- friendbot created `GB4K6YRH…` about half an hour later; it authorized a
  payment against `orizon_batch` (owned by the admin) and revoked it 45
  seconds later. *(Declared 2026-09-29, from the Epic 5 audit.)*

No transaction links any of these keys to a team key. The spike's charge,
which our admin would have signed, failed in simulation and was never
submitted. The authorizations still read `spent 0`. That these keys are ours
comes from the team's own spike records and **cannot be verified
independently**. They are declared anyway, because counting a key we know is
ours as external would be the worse error. Each entry says so in its
`evidence`.

### D3. Runtime platform keys are excluded without being declared

These keys are ours by construction, so the report excludes them as well:

- the public half of this deployment's signing key;
- the dispatch signer;
- the configured `STELLAR_ADMIN_ADDRESS`;
- the escrow's `settler()` and `admin()`;
- the registry's `admin()`.

`reason` is `team_wallet` for a register entry and `platform_key` for one of
these. When an owner is both, the register wins, because its entry carries
evidence a reviewer can check.

A failed read of the escrow's `settler()` or the registry's `admin()` degrades
the report, because it leaves an owner we cannot rule out. The escrow's
`admin()` is best effort. v1 has no such view, and on v2 it names the
deployment admin, which is already excluded.

### D4. The agent list is the registry, cross-checked

The agents are the registry-synced `state.agents` with `source == "onchain"`,
with their on-chain owners. The registry has no owner transfer, so the
mirrored owner is the owner. Seeded `agt_*` ids are never operators.

The mirror has blind spots that nothing else reports:

- a process whose first sync pass has not landed;
- a failing pass;
- a record the mirror refused, such as an unbelievable price.

So the report also reads the registry's `list_ids`. For every id the mirror
lacks, it reads `owner_of`:

- if the owner is ours, the id is listed under `excluded`;
- otherwise, the id goes in `unreadable_agents`.

A missing agent must never read as a zero.

An agent is **external** when its owner is in neither the register nor the
runtime set. `unique_operator_wallets` is the number of distinct external
owners.

### D5. A settled external workflow

This reuses `settlement_svc.fetch_settlement(agent_id)`, one call per external
agent, with bounded concurrency (`READ_CONCURRENCY = 4`). It counts only the
entries that settlement counts as verified revenue. So it inherits
settlement's exclusions: a charge is not counted when the payer is the agent's
owner, the settler, a platform key, or unreadable.

A charge is counted only if it names a usable transaction hash, because §6.3
asks for a charge a reviewer can open. The total is the number of **distinct
job ids** across all external agents. One workflow that pays two external
agents is one workflow.

A team wallet paying an external operator still counts under §6.3. It must
still be visible, so the payer is always reported, and `payer_team_role`
carries the role of any team or platform payer. It is `null` for anyone else.

### D6. Ignorance is never zero

`degraded` is true when any number may be lower than the truth. An agent goes
in `unreadable_agents` when:

- its settlement read failed, timed out or answered `unavailable`;
- its scan was truncated;
- it has a verified charge with no usable hash;
- it has no owner;
- it is an on-chain id the mirror lacks and that is not ours.

A truncated agent still contributes the charges it did verify. The flag is what
stops that from being read as the whole truth. An unreadable agent's
`settled_workflows` is `[]` with its id in `unreadable_agents`. A client must
read the pair, never the empty list alone.

A missing registry id, a failed `list_ids`, and a failed platform-key read
also set `degraded`. If no report can be produced at all, the answer is 503
`adoption_unavailable`, never a body of zeros.

### D7. `met` is arithmetic

`met.x` is `totals.x >= targets.x`, and the targets are constants citing §6.3.
`met` does not look at `degraded`. A client that wants "met and trustworthy"
reads both.

### D8. The report says how far back it looked

*Added 2026-09-29, from the Epic 5 audit.* `settled_external_workflows` only
sees what the settlement scans see, and the response did not say so.
`window_days` is now a top-level number on the report:

- It is the settlement scan's **measured** span, `SettlementEvidence.window_days`
  (`scanned_ledgers` × the node's own seconds per ledger), not a constant.
- When the per-agent scans cover different spans, it is the **smallest**, so
  "settled in the last N days" holds for every external agent. A truncated
  scan counts with the span it did cover.
- A scan that did not run (failed, timed out, `unavailable`) has no span and is
  left out. That agent is already in `unreadable_agents` and `degraded` is set.
- It is `0` when no scan ran at all, for example when there are no external
  agents.

## Consequences

- **The window is about seven days, and the report says so.** Soroban RPC
  keeps events for about seven days, and settlement evidence is read from
  events. A settlement older than that drops out of
  `settled_external_workflows`, so the number can fall. The span actually
  scanned is `window_days` (D8). The report is therefore a live view. The durable evidence for §6.3 is the
  transaction hashes it links, which a reviewer can open after they have left
  the window. Capture them into the evidence bundle when the target is met.
- **The unit is the escrow's asset.** `amount_usdc` is the escrow's 7-decimal
  amount, the unit prices are quoted in. On testnet the SAC wraps the native
  asset (`settlement_svc` reads it as `native`), so the value moved is XLM.
- **Bindings follow the store.** `bound` is `get_binding_store().get(id) is not
  None`. The endpoint URL is never read into the response, in any form.
- **v1 is never asked for `admin()`.** The escrow's version is read first
  (`sc.escrow_version`, cached per contract), and only v2 is asked for a view
  it has. Asking v1 would log an RPC error on every report computation.

## Rejected

- **Counting from our own orchestration records.** A run finalizes `complete`
  whether or not its charge settled (ADR 0010). That would be our claim, not
  the chain's.
- **Excluding a team payer's settlement.** §6.3 asks whether external agents
  were routed work and paid. Who paid is shown, not used as a filter. The
  owner and platform exclusions already stop us paying ourselves.
- **Treating an unreadable agent as zero, or dropping it.** That is exactly
  the silent under-report the story forbids.

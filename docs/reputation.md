# Reputation: the prior, the floor, and the cold-start guarantee

This explains why an agent that has never worked is still allowed to work, what
protects that, and what would take it away.

Two audiences. **Operators** — deciding whether to register an agent, or whether
a listed agent with no history will actually be hired — want the first two
sections. **Engineers** — about to change `REPUTATION_PRIOR_BPS`,
`REPUTATION_PRIOR_WEIGHT_USDC` or `REPUTATION_FLOOR_BPS` — want "The arithmetic"
and "How the guarantee breaks", and should treat the table in the latter as the
check to run before committing any of those three values.

## The guarantee

**A newly registered agent, with no ratings at all, is routable.** It is offered
to the planner on the first request after it registers and binds an endpoint,
before it has delivered anything.

This is a product rule, not an accident of the current numbers. Permissionless
registration (Deliverable 1) means an agent joins the marketplace without asking
anyone's permission. A routing floor that excluded every agent with no history
would take that back: registration would be permissionless and *routing* would
not, which is a gated marketplace with extra steps. The only way off a zero
score is to be hired, and the only way to be hired is to not be at zero.

So: **any change to the floor, the prior, or the prior weight that puts a
brand-new agent below the floor is a breaking change to Deliverable 1**, not a
tuning decision. It is not forbidden — a deployment may legitimately decide
newcomers must earn their way in — but it changes what the product promises, and
it has to be chosen deliberately and stated, not arrived at by nudging a number.

## Why a newcomer scores 3.5/5 rather than 0

An agent with no ratings carries no evidence about itself. There are two ways to
turn "no evidence" into a routing decision.

The first is to score it zero and treat the absence of evidence as evidence of
badness. That number exists in the payload: `avg_bps`, the unsmoothed on-chain
mean, is exactly `0` for an agent nobody has rated. Routing on it would exclude
every newcomer, permanently.

The second — what this backend does — answers "unknown" with the network's own
expectation, and then discounts that answer for how little stands behind it.
Two steps:

**1. The prior.** With no evidence about this agent, the best available estimate
of how it will perform is how agents in general perform. That estimate is
`REPUTATION_PRIOR_BPS`, shipped at 7000 bps — 3.5 out of 5. A newcomer is not
being *trusted* at 3.5; it is being *scored* at the network's expectation until
it produces a record of its own. Real ratings are then blended with the prior
and weighted by the USDC at stake on the step that earned each one, so the
prior's influence shrinks as evidence arrives. At 12 USDC of accumulated
evidence weight the agent's own record and the prior count equally; beyond that
the record dominates.

**2. The lower bound.** Routing does not use the smoothed mean. It uses a
conservative, Wilson-style lower bound on it: the mean, minus one standard error
of the mean, where the effective sample size is the accumulated evidence weight
in USDC rather than a count of jobs. This is where thin evidence is paid for. A
newcomer's 3.5/5 becomes **2.84/5** for routing purposes, and the whole of that
0.66 discount is the cost of having no record. An agent with the same 3.5 mean
and a long settled history is routed on a bound close to 3.5, because there is
little uncertainty left to discount.

The floor is applied to that lower bound, and only to it. It is **never** applied
to `avg_bps`, which would exclude every newcomer, nor to the smoothed mean,
which would ignore how much evidence is behind the number.

The consequence an operator should take away: a newcomer clears the floor
because the network's own average, discounted for zero evidence, is still above
it — and it clears by a small margin. Nothing about the cold start protects the
agent afterwards. A few heavily-weighted bad ratings drop it below the floor
quickly, which is the intended shape: the guarantee is a starting position, not
a grace period.

## The arithmetic, with the shipped numbers

The 0–5 score a buyer sees is basis points ÷ 2000.

| quantity | bps | 0–5 | source |
| --- | --- | --- | --- |
| prior mean | 7000 | 3.50 | `REPUTATION_PRIOR_BPS` |
| prior weight | — | — | `REPUTATION_PRIOR_WEIGHT_USDC` = 12 USDC |
| confidence multiplier | — | — | `WILSON_Z` = 1.0, a module constant, not configurable |
| newcomer smoothed mean | 7000 | 3.50 | no evidence, so the smoothed mean *is* the prior |
| newcomer lower bound | **5677** | **2.84** | the number routing actually tests |
| routing floor | 5500 | 2.75 | `REPUTATION_FLOOR_BPS` |
| **margin** | **177** | **0.09** | 5677 − 5500 |

The working, for anyone checking the table:

```text
smoothed mean   = prior                         = 0.70
effective n     = prior weight only             = 12
lower bound     = 0.70 − 1.0 × sqrt(0.70 × 0.30 / 12)
                = 0.70 − 0.1323
                = 0.5677                        → 5677 bps → 2.84 / 5
```

**177 bps is the entire safety margin of the cold-start guarantee.** It is not a
comfortable buffer; it is 0.09 of a star. Every row in the next section is a way
to spend it.

The live values are readable without going near the code:

```bash
curl -s https://<host>/api/stellar/reputation/params
```

returns `prior_bps`, `prior_weight_usdc`, `floor_bps` and `wilson_z` for the
running deployment. `GET /api/stellar/reputation` returns every agent's score
alongside the floor and prior in force; an agent with no evidence shows
`source: "prior"`, `avg_bps: 0`, `lower_bound_bps: 5677`.

## How the guarantee breaks

Three configuration values can break it. Each threshold below is computed with
the other two at their shipped defaults. Change two at once and neither row
applies — recompute.

| change | guarantee holds while | breaks at | why |
| --- | --- | --- | --- |
| raise `REPUTATION_FLOOR_BPS` | ≤ 5677 | ≥ 5678 | the floor passes the newcomer's bound |
| lower `REPUTATION_PRIOR_BPS` | ≥ 6842 | ≤ 6841 | a lower mean carries the bound down with it |
| lower `REPUTATION_PRIOR_WEIGHT_USDC` | ≥ 9.33 | ≤ 9.32 | a smaller effective sample widens the discount |

Moving any of these the other way — a lower floor, a higher prior, a heavier
prior weight — cannot break the guarantee.

Two of these are easy to walk into.

**Raising the floor.** "Raise the floor to 3.0 out of 5" sounds like a quality
decision and reads as a modest one. It is 6000 bps, which is 323 bps above the
newcomer's bound: it excludes every new agent on the network. The first floor
value that does so is 5678 — 2.839 out of 5.

**Lowering the prior weight.** This is the least visible of the three, because
it does not change any displayed score. A newcomer still reads 7000 bps / 3.5
on the dashboard and in the plan step; only the lower bound moves. An operator
who lowered it from 12 to 8 to make real evidence count sooner would exclude
every newcomer from routing without a single number on any screen changing.

## Two outcomes that both read as "routable"

The floor check returns True in two different situations, and they are not the
same claim. Anyone reading the tests will otherwise conclude the guarantee is
covered more thoroughly than it is.

**A cold-start score that clears.** The agent has a reputation entry —
`source: "prior"`, `avg_bps: 0`, `lower_bound_bps: 5677` — and 5677 ≥ 5500. This
is a *measurement*. It depends on the three values above, and under a different
configuration it comes out False.

**No reputation entry at all.** `passes_floor(None)` returns True
unconditionally, with no arithmetic. "No entry" means routing was handed no
claim about this agent, and the system's answer to no claim is "do not block".
It returns True with the floor set to 10000.

In production every listed agent is read at the top of a decompose and every
read yields an entry, including failed ones, so `None` is the narrow window
where the registry gained an agent — the sync loop runs on a 15 s cadence —
between that read and the filter, plus any caller that supplies a partial map.
It is a deliberate fail-open, not the cold-start path. ADR 0006 D5 is the
buyer-facing half of the same distinction: a notice reports `lower_bound_bps`
as `null` for an agent with no entry, never `0`.

The practical consequence: a test asserting `passes_floor(None)` is True proves
nothing about the cold-start guarantee, and would keep passing under a
configuration that excludes every newcomer. The test that pins the guarantee is
the one that builds a prior-only score and asserts it clears the floor. The same
predicate is available at runtime as `reputation_svc.prior_clears_floor()`.

## The startup check

Since story 3.06 the backend evaluates the guarantee when it boots: it computes
the lower bound a prior-only agent would score under the running configuration
and compares it against the configured floor. If a newcomer would be excluded,
it logs a WARNING that names both numbers.

It warns; it does not refuse to start. A deployment is allowed to exclude
newcomers. What it is not allowed to do is exclude them silently, and the
failure mode this replaces is slow and hard to attribute: new agents register,
are never chosen, and their operators conclude the marketplace is dead rather
than that a config value moved 200 bps. The check puts the consequence in front
of whoever changed the value, at the moment they changed it.

The same verdict is available on demand. `GET /readiness` carries a `cold_start`
object built from the same computation as the startup line:

```json
"cold_start": {"routable": true, "lower_bound_bps": 5677, "floor_bps": 5500, "margin_bps": 177}
```

| field | meaning |
| --- | --- |
| `routable` | a brand-new agent clears the floor — the startup line's verdict |
| `lower_bound_bps` | what a newcomer is scored on: the prior's lower bound |
| `floor_bps` | the floor this process actually has — the Render dashboard's value, not render.yaml's |
| `margin_bps` | `lower_bound_bps − floor_bps`; negative means newcomers are locked out |

It exists because the startup line is written once per boot, and a free-tier
instance boots on every wake from idle: by the time anyone goes looking, the line
is buried under the request log or gone with the instance that wrote it. The
probe answers whenever it is asked, for the configuration in force.

It is informational, like `signer` and `pdax`, and never changes `status` or the
503. A floor that excludes newcomers is a policy the process serves correctly,
not a dependency it is missing — the same reason the startup check warns rather
than refusing to boot.

The same pair of numbers appears in the warning emitted when reputation reads
fall back to the prior, which states which way the floor is failing for the
duration of the outage.

## Cold start is not a degraded read

Both produce `source: "prior"`. They differ by one flag.

| | what happened | `source` | `degraded` |
| --- | --- | --- | --- |
| cold start | the ledger was read; this agent has no evidence | `prior` | `false` |
| degraded read | the ledger could not be read; the prior was served instead | `prior` | `true` |

They are different facts. A cold start is a true statement about one agent. A
degraded read is a statement about the chain, and it makes *every* agent in the
batch look like a newcomer regardless of its real record. `degraded` exists so a
client can tell them apart, and it reaches the buyer on a plan as
`reputation_degraded` (ADR 0006 D6).

The routing floor does not tell them apart. Both are judged by the same
arithmetic on the same lower bound, and there is deliberately **no bypass for
either** — no branch that says "this agent is new, skip the floor", and none
that says "reads are degraded, skip the floor".

A cold-start bypass would create a second way to be routable, one the configured
floor does not govern. An operator who raised the floor specifically to keep
unproven agents out would find it had no effect on the newest ones, and the only
way to discover that would be to read the code. Keeping the guarantee as
arithmetic means the floor is the single authority on what is routable, and
"does a newcomer get in?" has one answer per deployment, computable from config.

The price is that during an outage the floor fails **open** under the shipped
config: agents that would normally be filtered stay routable until reads
recover. That is accepted, for the reasons set out in the reputation service's
own notes — it is the same position the system takes on any agent it knows
nothing about; failing closed would not fail closed, since every agent would
drop below the floor at once and routing would fall to the starvation backstop
picking a top-N among identical prior scores; and the window is bounded by the
read TTL and the batch timeout. Every occurrence logs a WARNING naming the
affected agents.

## Before you change any of these values

1. Compute the prior-only lower bound under the new configuration and compare it
   to the new floor — `prior_clears_floor()` is exactly that predicate, and
   `/api/stellar/reputation/params` reports the inputs from a running deployment.
2. Start the service and read the log. A WARNING about the prior and the floor
   means newcomers are now excluded. On a deployed instance, where the log may
   already be gone, `curl -s https://<host>/readiness` says the same thing:
   `cold_start.routable` is `false` and `cold_start.margin_bps` is negative.
3. If they are excluded on purpose, say so where operators will see it. The
   marketplace no longer promises that registering an agent makes it routable,
   and that is a change to the product, not to a number.

Related: `docs/decisions/0006-floor-visibility.md` (what the buyer is told when
the floor removes an agent) and the Reputation system section of `README.md`
(the full tunable list and the on-chain / off-chain split).

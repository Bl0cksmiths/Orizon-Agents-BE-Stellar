# ADR 0013 — The overview reports only measured values

- **Status:** Accepted, 2026-10-02
- **Deciders:** Danielle (lead)
- **Builds on** ADR 0012 (adoption evidence), whose owner rule this reuses
  unchanged, and ADR 0007 (dispute window), whose `workflow_settlements` table
  is the settlement record read here.

## Context

`GET /api/metrics/overview` feeds the console's network dashboard. Until this
change it mixed real counters with invented numbers and presented the result
as live data:

| Field | What it actually was |
| --- | --- |
| `agents_online` | the real online count **plus 2481** (`DEMO_BASELINE_AGENTS_ONLINE`): the live API said 2527 while the registry held about 25 agents |
| `tasks_per_sec` | the constant 1.284 |
| `throughput` | a hard-coded 18-point sparkline |
| `skills` | a hard-coded "network composition" with colour tones |
| `avg_completion` | measured when a task had finished, otherwise 0.942 |
| `avg_trust` | measured when an agent had on-chain ratings, otherwise the seeded catalog's average or 4.86 |

A reviewer comparing the dashboard with the registry, the explorer or the
adoption report would find numbers that cannot be reproduced from anything.
On testnet, during a funded sprint, that is worse than a small number.

## Decision

### D1. Nothing is invented

Every `DEMO_*` constant is deleted, and a test fails if the prefix reappears
anywhere under `app/`. Each field the route returns is computed from a source
a reviewer can check:

| Field | Source |
| --- | --- |
| `agents.registered`, `onchain`, `seeded`, `online` | the registry mirror (`state.agents`), exactly what `GET /api/agents` lists, split by `source` and `status` |
| `agents.external`, `operators.external_wallets` | the on-chain mirror classified by `adoption_svc.OwnerRule`, the object `GET /api/ecosystem/adoption` uses: an owner is external only when it is in neither `app/data/team_wallets.json` nor the keys this deployment holds |
| `agents.bound` | the binding set behind the marketplace's `bound` flag, over on-chain agents only |
| `workflows.settled`, `workflows.series` | `count_settled_by_day()` on the dispute store: distinct jobs in `workflow_settlements` (all payers), bucketed by UTC day; the series is the last 14 days, oldest first, with zeros for empty days |
| `tasks` | the in-memory task store; `completion_rate` is complete ÷ (complete + failed) |
| `trust` | mean smoothed reputation (0–5) over agents whose read returned on-chain evidence |
| `skills` | the registry's own skill tags: the top five by agent count, then `other`; `pct` is a share of all tags, rounded by largest remainder so it sums to exactly 100 |

### D2. Not knowing is reported as not knowing

A part whose source cannot be read is `null` (or `[]` for the series) and
sets `degraded: true`. It is never replaced with a plausible number. These
are the cases:

- **Settlement store unreadable or slower than 5 s.** `workflows.settled` is
  null and `series` is `[]`.
- **Binding set never loaded.** `agents.bound` is null, because an empty set
  that nobody has loaded says nothing about any agent.
- **Owner rule unavailable.** `agents.external` and
  `operators.external_wallets` are null if the rule cannot be built at all.
  If a platform key could not be read, or an on-chain agent has no known
  owner, the counts are still served and `degraded` is set. ADR 0012 handles
  both cases the same way.
- **Reputation read degraded to the prior.** `degraded` is set, and `avg`
  covers only the agents that were read.

Absence is not the same as failure. With nothing rated on-chain yet,
`trust.avg` is null and `degraded` stays false: "no evidence" is a
measurement. With no finished task, `completion_rate` is null for the same
reason.

Each degraded part logs a WARNING that names it (`overview workflows
degraded: …`). The log is rate-limited the way the old trust log was: a
transition logs immediately, a steady state repeats at most every five
minutes, and recovery logs at INFO.

### D3. One computation per 15 seconds

The whole overview is cached in-process for 15 s through `app.stellar.cache`.
That cache is single-flight, so concurrent polls share one computation, and a
dashboard open in several tabs costs one set of reads. The app-wide rate
limit still applies to the route.

## Consequences

- The response shape changed. The old fields (`agents_online`,
  `tasks_per_sec`, `avg_completion`, `avg_trust`, `throughput`, `skills` with
  `tone`) are gone, and the frontend reads the new shape.
- The numbers are small, and they are true. On testnet the dashboard now
  shows the registry's 25 agents rather than 2527.
- Without `DATABASE_URL` the settlement store is the in-memory fallback.
  Its counts start from zero on every restart and stop growing at its cap.
  The store logs that at boot, and production sets `DATABASE_URL`.
- The seeded catalog's skill tags are fine-grained (`seo`, `a11y`,
  `42 langs`), so `other` is a large share of the skill mix. That is the
  registry as it stands.

## Rejected alternatives

- **Keep the baselines but label them.** A field that is sometimes
  measured and sometimes not still invites a reader to take it as measured.
  Removing the baselines is simpler than labelling them and cannot be
  misread.
- **Serve 0 when a source is down.** Zero is a measurement. Null with
  `degraded` is the only answer that does not claim to know something.
- **Run the adoption report for the external count.** The report scans
  settlements for each external agent, which is too heavy to run on every
  dashboard poll. The overview needs owners, not charges, so it reuses the
  report's rule and skips the scan.

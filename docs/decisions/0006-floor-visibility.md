# ADR 0006 — Making the reputation floor visible to the buyer

- **Status:** Accepted (story 3.02 / BLO-24), 2026-09-16
- **Deciders:** Danielle (lead)
- **Follows** story 3.01, which made the floor apply on both planning paths.
  This decides what the buyer is told about it.

## Context

The routing floor is Deliverable 2's selling point: an open registry is only
safe for a buyer if something between them and an unproven agent. Story 3.01
made that filter real on both planning paths. It is still invisible.

From outside, a filtered plan is indistinguishable from a plan where those
agents simply were not a good fit. The buyer cannot see the protection they are
being given, which means they cannot value it — and a protection nobody can
observe is, commercially, not a feature.

Two facts shaped this decision more than the story text did.

**The two paths report asymmetrically, and the asymmetry is a trap.** The
curated demo-kit path already emits `notices`. The free-form model path — every
non-curated intent, which is to say the normal product — constructed its
`DecomposeResponse` with no `notices` argument at all, and logged its
starvation relaxation to a server log no buyer will ever read. A reviewer
demoing a tetris intent would have seen a finished feature. Uniformity across
the paths is therefore the criterion this story actually turns on, not a
nice-to-have.

**The story asked for a vocabulary the system cannot speak.** It named four
exclusion reasons: `below_floor`, `inactive`, `unbound_endpoint` and
`not_selected_by_planner`. Only two of those describe something that happens.

## Decision

### D1 — `kind` and `reason_code` are orthogonal, and both stay

`PlanFloorNotice.kind` (`excluded` / `substituted` / `degraded`) says what
happened to the **plan**. `reason_code` says **why**. They compose:

| `kind` | `reason_code` | meaning |
| --- | --- | --- |
| `excluded` | `below_floor` | dispatchable, failed the Wilson lower bound |
| `excluded` | `unbound_endpoint` | on-chain, no endpoint — never a candidate |
| `substituted` | `below_floor` | kit-path replacement for a sub-floor agent |
| `degraded` | `floor_relaxed` | re-admitted by the starvation backstop |

`kind` is not renamed to match the story's vocabulary. The plan card already
ships against it (`execution-plan.tsx`, `lib/guards.ts`), and renaming a field
a live client reads buys nothing a second field does not.

### D2 — `inactive` is not in the vocabulary, because a withdrawal is absence rather than refusal

> **Amended 2026-09-17.** The original reasoning was that nothing in routing
> read `Agent.status`, so the value could not be produced. **That premise is no
> longer true** — the Epic 2 hardening pass taught both planning paths, the
> floor-substitute search and the post-LLM clamp to honour the listing flag, so
> `set_active(id, false)` now genuinely stops routing. The Consequences section
> below said `inactive` "will need adding if, and only if, the orchestrator is
> ever taught to honour the listing flag". That trigger has fired. The answer is
> still no, but it has to be re-derived rather than inherited, so the body of
> this decision is replaced.

`AgentRegistry.set_active(id, false)` syncs to `Agent.status == "offline"`
(`registry_sync.py:225`), and routing now excludes those agents everywhere a
candidate is chosen.

They are still not reported as exclusions, because of **what kind of fact each
reason code carries**:

- `below_floor` is a verdict *we* reached about an agent.
- `floor_relaxed` is *our own rule* bending.
- `unbound_endpoint` is a setup step **the operator has not finished** — D4
  below exists to get it fixed, and is worded to avoid blame for that reason.

A withdrawal is none of these. The operator asked to be absent, got exactly
what they asked for, and there is nothing to fix. `unbound_endpoint` reports an
*unfinished* state its subject wants resolved; `inactive` would report a
*deliberate, completed* state its subject wants honoured. Publishing it on
every buyer's plan card would turn an operator's own business decision — a
maintenance window, a withdrawal, a wind-down — into a standing public
exclusion, which is the opposite of what the call asked for.

Two things make the omission safe rather than merely defensible:

1. **The fact is already published where it belongs.** `GET /api/agents`
   returns every agent, offline ones included, with `status` on the row
   (`app/routers/agents.py`, no filtering). A buyer who wants to know can see
   it on the agent, in the catalog — not on somebody else's plan.
2. **It is D3's drowning failure, and worse.** The withdrawn set grows
   monotonically over a deployment's life and never recovers, unlike a
   sub-floor agent that one good rating clears. An uncapped list would bury the
   notices this feature exists to surface; a capped one would need a second cap
   constant and more contract surface for a signal nobody can act on.

### D3 — `not_selected_by_planner` is not an exclusion

The story's own product rules forbid listing every unpicked agent, and they are
right to: on a twelve-agent registry with a six-step plan, six "exclusions" per
response would drown the signal this story exists to create. An agent that
cleared the floor and was simply not chosen was not protected against. It is
pinned by a test rather than left to judgement.

### D4 — `unbound_endpoint` IS reported, capped at eight

An agent registered on-chain with no endpoint bound is passed over silently.
That is the exact condition stories 2.05 and 2.06 exist to make visible to its
operator, and the buyer-facing half is the same fact: the marketplace lists
seventeen agents and the plan drew from twelve. Reporting it explains the gap.

Capped because the set is unbounded in principle and an uncapped list is the
same drowning failure as D3. Eight matches the frontend hook's `MAX_CHECKED`,
so both surfaces stop counting at the same place rather than disagreeing about
how many agents exist. The notices are sorted by agent id, because the kit path
is the demo safety net and must stay reproducible.

### D5 — the deciding numbers travel as data, not as prose

`reason` remains a human sentence and keeps rendering. Alongside it,
`lower_bound_bps` and `floor_bps` travel as integers. A client that wants to
show "4.10 against a 3.00 floor" should not have to parse English to get there.

`lower_bound_bps` is **null**, never 0, when the agent had no reputation entry.
Those are different facts: `passes_floor(None)` returns True, so an agent with
no entry is routed. A 0 would render a contradiction beside a routed agent.

### D6 — `reputation_degraded`, not `degraded`

`RepInfo.degraded` means the on-chain read **failed** and the Bayesian prior was
served in its place; the reputation service fails open. A buyer authorising
payment against a plan built on estimates rather than settled evidence has a
right to know.

The obvious field name is taken twice over: `PlanStep.degraded` and
`PlanFloorNotice.kind == "degraded"` both already mean "re-admitted below the
floor by the starvation backstop". Three meanings of one word in one payload is
a defect waiting to be written, so the plan-level flag is named apart.

### D7 — every added field is additive with a default

A frontend build predating this change must keep rendering. Pinned on both
sides: a model-level test that the pre-3.02 payload still validates, and a
frontend guard test that the old and new shapes both pass `isDecomposeResponse`.

On the frontend, `kind` is set-checked and `reason_code` deliberately is not. An
unlisted `kind` renders an unstyled, unexplained row, so the payload is
rejected; an unrecognised `reason_code` still has the prose `reason` beside it,
so rejecting the whole payload would trade a rendered plan for no plan.

## Consequences

- Both planning paths construct notices through one module
  (`app/services/plan_notices.py`), so uniformity is structural rather than a
  convention two code paths have to remember.
- The floor value travels per-response. A deployment that changes
  `REPUTATION_FLOOR_BPS` narrates its own threshold correctly without a
  frontend release.
- `inactive` will need adding if, and only if, the orchestrator is ever taught
  to honour the listing flag. Until then its absence is the honest report.

  > **Amended 2026-09-18.** That trigger has fired and the conclusion above
  > did not follow. Routing now honours delisting everywhere a candidate is
  > chosen (`_is_listed` in `app/services/orchestrator_svc.py`:
  > `status != "offline"`), and `inactive` was still not added — D2, amended
  > 2026-09-17, re-derives why. The rule that shipped is broader than leaving
  > one value out of the vocabulary: **a delisted agent gets no notice under
  > any reason code.** That includes `unbound_endpoint` for an agent that is
  > both withdrawn and unbound — "no endpoint bound" is true of it, but it is
  > not why the agent is absent, and it is advice nobody wants acted on.
  >
  > A withdrawal is the operator's own business decision, not a verdict the
  > buyer was protected from, and `status` already says so on the agent's own
  > `GET /api/agents` row. Announcing it on every buyer's plan, for as long as
  > the operator stays withdrawn, would drown the notices that matter exactly
  > as D3 argues `not_selected_by_planner` would — worse, because the withdrawn
  > set only grows. The reasoning lives beside the filter in
  > `_routable_registry`, and `tests/test_delisted_routing.py` pins it.
- The notices explain which agents the floor removed. They do not explain which
  agent the planner *preferred* among those that cleared it — that is a model
  decision, and claiming to explain it would be a fiction.

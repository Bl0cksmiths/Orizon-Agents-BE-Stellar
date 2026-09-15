# ADR 0005 — External step failure: what it costs, and who it costs

- **Status:** Accepted (story 2.03 / BLO-20), 2026-09-16
- **Deciders:** Danielle (lead)
- **Extends** ADR 0004. 2.02 made a dispatch verifiable and bounded; this decides
  what happens when it fails.

## Context

The story's premise is "non-delivery has a cost to the agent rather than to me".
Recon established that the buyer half is true by construction and the agent half
is false in the cases that matter. Measured against the shipped config
(`prior_bps=7000`, `prior_weight_usdc=12.0`, `floor_bps=5500`, Wilson `Z=1.0`):

1. **A run where nothing succeeded rates nobody.** `_submit_ratings` sits behind
   `succeeded != 0` → `_settle_onchain` → `charge_tx and job_id`. The canonical
   broken endpoint — down, fails every step — accumulates **zero** negative
   evidence, forever. `tests/test_settlement_receipts.py` pins that as intended,
   so this is a decision being revisited, not a bug being patched.
2. **A junk response is rewarded.** `synthetic_rating` returns 20 only for
   *falsy* output, so `{"ok": true}` scores **70** — the prior exactly. Evidence
   mass grows while the mean holds, so the lower bound **rises**: 5677 → 5746
   after 25 junk responses. Returning garbage is strictly better than failing,
   and no amount of it can ever cross the floor. Verified by running the code.
3. **Dust pricing is permanent immunity.** `rating_weight_stroops` floors at 1
   stroop, and on-chain decay is 92.5 %/week. Below ~0.001 USDC/step an agent
   cannot be excluded at any realistic traffic level. Nothing checks a minimum.
4. **Our outage is recorded as their non-delivery.** A binding-store read
   failure fails open, the step is skipped, no `delivered` entry exists, and the
   agent takes an on-chain 20/100 — for an outage on our side. The failure is
   negative-cached for 2.5 s, so one blip can punish several steps.
5. **Twelve distinct failure modes render as one trace line**, `{worker} failed`.
   AC-3's three named cases are byte-identical to a buyer.

Exclusion counts, for the record: **1** failure at ≥0.4482 USDC/step, **3** at
0.180, **9** at 0.054, **38** at 0.012, **65** at 0.007.

## D1 — A failed step is never billed (confirmed, not changed)

There is exactly one billing site and every failure path `continue`s before it,
including the `_unusable_field` gate added in #44. This ADR changes nothing
here; 2.03's job was to *prove* it, so it gains explicit tests rather than an
inherited guarantee.

## D2 — Ratings decouple from settlement

A run with no successful step still submits ratings. Charge and seal stay
skipped — the existing reasoning is correct (charging would consume the payer's
authorization for a dust amount, and there is nothing to attest to) — but
ratings have the opposite economics: a total failure is precisely when the
evidence matters most.

Ratings need a `job_id`, which normally comes from the charge. For a
rating-only run it is **derived deterministically from the task id**, reusing
the pattern ADR 0002 established for dispute ratings (`dispute_job_id`): a
distinct but reproducible id that clears the ledger's replay guard while
staying linkable to the run that produced it.

Consequence to accept knowingly: ratings now reach the chain on runs that move
no money, so a failing agent costs the platform submit fees. That is the cost of
having a governance mechanism at all.

## D3 — Untrusted output must deliver something to earn the base score

`synthetic_rating`'s base 70 is reachable by any non-empty dict. For
**untrusted** output only — the distinction `_rating_view`'s `first_party` flag
already draws — a response must carry something checkable (an artifact, or
critic content) to reach base. A bare acknowledgement scores as non-delivery.

Deliberately **not** a general quality grader: that is a much larger economic
change and belongs in its own story. This closes the specific hole where
answering `{"ok": true}` outperforms answering honestly.

First-party scoring is untouched. Local workers legitimately return text with no
artifact, and regrading them is out of scope.

## D4 — A minimum plausible price

`registry_sync` already refuses an implausibly *high* on-chain price; it now
refuses an implausibly low one by the same mechanism and for the same reason.
Below the minimum, a rating carries so little evidence weight that decay
outruns accumulation and the routing floor can never apply — so a dust price is
not a cheap agent, it is an unaccountable one.

## D5 — We never rate a step we did not dispatch

If the binding store could not be read, the step was skipped because of **our**
failure. It must not be rated. The distinction is "did not deliver" versus
"was never asked", and only the first is the operator's fault.

## Frozen interfaces

### `app/agents/workers/external_http.py`

```python
DISPATCH_RULES: frozenset[str]   # closed, lowercase snake_case

class ExternalDispatchError(RuntimeError):
    rule: str                    # one of DISPATCH_RULES
    def __init__(self, rule: str, message: str) -> None: ...
```

Third instance of the pattern `EndpointPolicyError` and `ExternalOutputError`
already share: a closed vocabulary, the token machine-readable and the prose
free, and the constructor raising on an unknown rule so a typo cannot escape.
The wrapped `EndpointPolicyError.rule` / `ExternalOutputError.rule` is preserved
in the message; the coarse class is what reaches the trace.

Every httpx transport exception is wrapped. Seven of them — `RemoteProtocolError`,
`DecodingError`, `ReadTimeout`, `ReadError`, `PoolTimeout`, `WriteTimeout`,
`TooManyRedirects` — currently escape `_dispatch` entirely, so the module
docstring's "any failure is raised as ExternalDispatchError" is false today.

Refusal logs carry **host and rule, never the URL**. The policy message embeds
`url!r`, so passing it through puts an endpoint like `…/dispatch?token=SECRET`
into the server log — while the bind path deliberately logs host-only
(ADR 0003).

### `app/services/failure_tracker.py` (new)

```python
def record_failure(agent_id: str, rule: str) -> None: ...
def record_success(agent_id: str) -> None: ...
def consecutive_failures(agent_id: str) -> int: ...
```

Bounded: an `OrderedDict` with a hard cap and oldest-eviction on insert, the
shape `app/pdax/ramp_store.py` uses — agent ids are caller-influenced, so an
unbounded map is a memory vector. Logging coalesces on **failure class change**,
the `dispatch_signing._warned_reason` pattern: the same class repeating is not
news, a class changing is.

### Consumed in `execution_svc`

The run loop reads the class **duck-typed** — `getattr(e, "rule", None)` with a
generic fallback, the way `pdax.errors.orizon_code` defaults. The loop must not
import a worker's module to classify a failure, and an unclassified exception
must degrade to the generic token rather than crash the classifier.

## Out of scope, stated so it is not assumed

The floor-starvation fallback re-admits below-floor agents when fewer than three
clear the floor, and it sorts by `smoothed_bps` while exclusion is decided on
`lower_bound_bps` — two different statistics. A 2.5 s RPC hiccup in `fetch_reps`
also restores every excluded agent to the prior for that planning pass. Both are
deliberate degradation choices from earlier stories; neither is re-opened here.

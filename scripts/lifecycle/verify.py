"""Settlement and seal checks — pure functions of what the ledger returned.

Given the settlement record the API serves (`GET /api/tasks/{id}/disputes`)
and what the chain says (events, views, balances), each function answers one
question with a `Check`. Nothing here does I/O, so each rule is tested on its
own against hand-built ledger answers.

Escrow v1 and v2 settle differently (docs/escrow-v2-interface.md in the
contracts repo), and the checks follow the contract, not the API:

  * **v2** — one `settle` transaction pays each delivered step's operator from
    custody and returns the rest to the buyer. So: one `charged` event per
    delivered step, topic naming THAT step's agent and amount its price; one
    `settled` event whose `spent` is their sum and whose `returned` is the
    authorization's max minus that; the authorization view reads settled;
    the buyer's balance fell by exactly the paid sum plus the fee they paid
    to authorize; each operator's rose by exactly what their agents earned.
  * **v1** — one `charge` for the whole plan under `orizon_batch`. v1 cannot
    settle an external payer's funds (defect D-039), so the honest check is
    whether the charge transaction succeeded and emitted `charged`, and the
    report says which.

A check whose inputs were not measured (a resumed run with no pre-authorize
balance snapshot, an operator who is also the buyer) is `ok=None` — reported,
never counted as a pass or a failure.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from .chain import ChainEvent, Observation
from .signing import usdc_to_stroops


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None
    detail: str


def passed(checks: list[Check]) -> bool:
    return all(c.ok is not False for c in checks)


def delivered_steps(settlement: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in settlement.get("steps") or [] if s.get("delivered")]


def _named(events: list[ChainEvent], topic: str) -> list[ChainEvent]:
    return [e for e in events if e.topics and e.topics[0] == topic and e.successful]


def check_tx(name: str, seen: Observation | None) -> Check:
    if seen is None:
        return Check(name, False, "no transaction hash was recorded")
    ok = seen.status == "SUCCESS"
    return Check(name, ok, f"{seen.status} on ledger {seen.ledger} (read from {seen.source})")


# ── v2 ──────────────────────────────────────────────────────────
def expected_payouts(settlement: dict[str, Any], owners: dict[str, str]) -> Counter[tuple[Any, int]]:
    """(agent, stroops) for every delivered step v2 should have paid.

    A delivered step is paid only when its agent has an on-chain owner: the
    interface has a payout to an unregistered agent revert the whole settle,
    so the backend leaves the seeded catalogue out. A settlement that states
    each step's `paid_usdc` (the settle lane's receipt, ADR 0010) is taken at
    its word for WHICH steps and HOW MUCH — the events are then checked
    against that — and otherwise the step's price and the registry decide.
    """
    out: Counter[tuple[Any, int]] = Counter()
    for s in delivered_steps(settlement):
        if "paid_usdc" in s:
            if s.get("paid_usdc") is None:
                continue
            out[(s.get("agent_id"), usdc_to_stroops(float(s["paid_usdc"])))] += 1
        elif s.get("agent_id") in owners:
            out[(s.get("agent_id"), usdc_to_stroops(float(s.get("price_usdc") or 0)))] += 1
    return out


def check_charged_events(
    events: list[ChainEvent], settlement: dict[str, Any], owners: dict[str, str]
) -> tuple[list[Check], int]:
    """One `charged` per paid step, naming its agent, for its amount. Returns (checks, paid sum)."""
    charged = _named(events, "charged")
    job = settlement.get("job_id_hex")
    seen: Counter[tuple[Any, int]] = Counter()
    paid = 0
    wrong_job = 0
    for e in charged:
        agent = e.topics[1] if len(e.topics) > 1 else None
        data = e.value if isinstance(e.value, list) else []
        amount = int(data[2]) if len(data) > 2 else 0
        if len(data) > 3 and job and data[3] != job:
            wrong_job += 1
        seen[(agent, amount)] += 1
        paid += amount
    expected = expected_payouts(settlement, owners)
    detail = (
        f"{len(charged)} charged event(s) for {sum(expected.values())} paid step(s); "
        f"paid {dict(seen)} expected {dict(expected)}"
    )
    if wrong_job:
        detail += f"; {wrong_job} name a job other than {job}"
    settled = usdc_to_stroops(float(settlement.get("settled_usdc") or 0))
    return [
        Check("v2_charged_per_delivered_step", seen == expected and wrong_job == 0, detail),
        Check("v2_paid_sum_matches_settlement", paid == settled, f"charged sum {paid}, settlement says {settled}"),
    ], paid


def check_settled_event(
    events: list[ChainEvent], settlement: dict[str, Any], paid: int, max_stroops: int | None
) -> Check:
    settled = _named(events, "settled")
    if len(settled) != 1:
        return Check(
            "v2_settled_event", False, f"{len(settled)} settled event(s) in the settle transaction, expected 1"
        )
    data = settled[0].value if isinstance(settled[0].value, list) else []
    if len(data) < 4:
        return Check(
            "v2_settled_event", False, f"settled event data {data!r} is not (auth_id, job_id, spent, returned)"
        )
    _auth, job, spent, returned = data[0], data[1], int(data[2]), int(data[3])
    problems = []
    if spent != paid:
        problems.append(f"spent {spent} != charged sum {paid}")
    if settlement.get("job_id_hex") and job != settlement["job_id_hex"]:
        problems.append(f"job {job} != settlement job {settlement['job_id_hex']}")
    if max_stroops is not None and returned != max_stroops - spent:
        problems.append(f"returned {returned} != max {max_stroops} - spent {spent}")
    detail = f"spent {spent}, returned {returned} to the buyer"
    if max_stroops is None:
        detail += " (authorized max unknown on this resume; remainder not cross-checked)"
    return Check("v2_settled_event", not problems, "; ".join(problems) or detail)


def settled_auth_id(events: list[ChainEvent]) -> str | None:
    settled = _named(events, "settled")
    if settled and isinstance(settled[0].value, list) and settled[0].value:
        return str(settled[0].value[0])
    return None


def check_authorization_view(view: dict[str, Any] | None, paid: int, buyer: str) -> Check:
    if view is None:
        return Check("v2_authorization_view", False, "PaymentEscrow.authorization(auth_id) returned nothing")
    problems = []
    if view.get("settled") is not True:
        problems.append(f"settled={view.get('settled')!r}")
    if int(view.get("spent") or 0) != paid:
        problems.append(f"spent {view.get('spent')} != charged sum {paid}")
    if view.get("payer") != buyer:
        problems.append(f"payer {view.get('payer')} is not the buyer {buyer}")
    return Check(
        "v2_authorization_view", not problems, "; ".join(problems) or f"settled, spent {paid}, payer is the buyer"
    )


def check_buyer_delta(
    before: int | None, after: int | None, paid: int, authorize_fee: int | None, fee_in_asset: bool
) -> Check:
    """The buyer paid exactly the charged sum, plus the authorize fee when the
    fee is paid in the same asset (testnet's SAC wraps native XLM, so it is)."""
    if before is None or after is None:
        return Check("v2_buyer_balance_delta", None, "not measured: no pre-authorize balance snapshot for this run")
    fee = (authorize_fee or 0) if fee_in_asset else 0
    if fee_in_asset and authorize_fee is None:
        return Check("v2_buyer_balance_delta", None, "not measured: the authorize fee could not be read from Horizon")
    delta = before - after
    ok = delta == paid + fee
    return Check(
        "v2_buyer_balance_delta", ok, f"buyer balance fell {delta} stroops; paid {paid} + fee {fee} = {paid + fee}"
    )


def check_operator_deltas(
    before: dict[str, int],
    after: dict[str, int],
    owners: dict[str, str],
    events: list[ChainEvent],
    not_isolatable: set[str],
) -> list[Check]:
    """Each operator's balance rose by exactly what their agents were charged.

    `owners` maps agent id -> owner account. An owner that is also the buyer
    or the settler pays fees or refunds inside the window, so its delta is
    reported as not isolatable rather than failed.
    """
    earned: Counter[str] = Counter()
    for e in _named(events, "charged"):
        agent = e.topics[1] if len(e.topics) > 1 else None
        data = e.value if isinstance(e.value, list) else []
        owner = owners.get(str(agent))
        if owner and len(data) > 2:
            earned[owner] += int(data[2])
    checks = []
    for owner, amount in sorted(earned.items()):
        name = f"v2_operator_delta:{owner[:6]}…{owner[-4:]}"
        if owner in not_isolatable:
            checks.append(Check(name, None, f"not isolatable: {owner} is also the buyer or the settler"))
            continue
        if owner not in before or owner not in after:
            checks.append(Check(name, None, "not measured: no balance snapshot before the settle"))
            continue
        delta = after[owner] - before[owner]
        checks.append(Check(name, delta == amount, f"operator balance rose {delta} stroops; charged {amount}"))
    return checks


# ── v1 ──────────────────────────────────────────────────────────
def check_v1_charge(seen: Observation | None, events: list[ChainEvent]) -> list[Check]:
    checks = [check_tx("v1_charge_tx", seen)]
    if seen is not None and seen.status == "SUCCESS":
        charged = _named(events, "charged")
        checks.append(
            Check("v1_charged_event", bool(charged), f"{len(charged)} charged event(s) in the charge transaction")
        )
    return checks


# ── the seal ────────────────────────────────────────────────────
def check_seal(
    attestation: dict[str, Any] | None,
    settlement: dict[str, Any],
    agent: str,
    receipts: list[str] | None,
) -> list[Check]:
    """AttestationRegistry.get(job_id) exists and names what settled.

    The sealed total is compared with the settlement's `settled_usdc`, the
    figure the API reports the buyer paid. `receipts` are the v2 charged
    events' receipt ids; with v1 they are None and are not compared.
    """
    job = settlement.get("job_id_hex")
    if attestation is None:
        return [Check("seal_on_registry", False, f"AttestationRegistry.get({job}) found nothing")]
    checks = [Check("seal_on_registry", True, f"attestation sealed at {attestation.get('sealed_at')} for job {job}")]
    agents = attestation.get("agents") or []
    checks.append(Check("seal_names_agent", agent in agents, f"sealed agents {agents}"))
    settled = usdc_to_stroops(float(settlement.get("settled_usdc") or 0))
    total = int(attestation.get("total_spent") or 0)
    checks.append(Check("seal_total_matches_settlement", total == settled, f"sealed total {total}, settled {settled}"))
    if receipts is not None:
        sealed = sorted(attestation.get("receipts") or [])
        checks.append(
            Check(
                "seal_receipts_match_charges",
                sealed == sorted(receipts),
                f"sealed receipts {sealed}, charged {sorted(receipts)}",
            )
        )
    return checks


def charged_receipts(events: list[ChainEvent]) -> list[str]:
    out = []
    for e in _named(events, "charged"):
        if isinstance(e.value, list) and e.value:
            out.append(str(e.value[0]))
    return out

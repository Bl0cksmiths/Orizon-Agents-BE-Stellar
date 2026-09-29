"""The eleven SOW §6.3 metrics, judged from one `Snapshot`. Pure: no reads happen here.

The rules, each stated where it is applied:

  EXTERNAL. An owner is external only if it is neither in the committed team
  register nor a key the platform runs (the network admin, the dispatch
  signer, the ratings signer and scorer, the registry admin, every escrow's
  settler and admin, the ledger's admin and scorer, the attestation
  registry's admin and sealer).

  SETTLEMENT. Every receipt an escrow has issued counts as one charge — a v1
  `charge`, or one payout of a v2 `settle` — unless it is a self-payment (the
  payer owns the agent, is the escrow's settler, or is a platform key) or it
  settled before the sprint began (2026-09-07). A WORKFLOW routed to an
  external agent is a distinct settled job with at least one counted charge
  to an externally owned agent.

  DISPUTE REFUND. A refund counts only when it is traceable to a real
  dispute: a `kind=dispute` rating whose job id is the derived dispute id of
  a counted charge's job (`dispute_job_id` below, re-stated from
  `app/services/dispute_rating.py`), followed by an asset-contract transfer
  from a platform key to that charge's payer, no larger than the charge. A
  transfer with no dispute behind it — the 4.01 refund drill to a team key —
  is not a dispute refund.

  UNMEASURED. A metric whose sources failed to read is "Not measured": its
  status is `not_met` and its achieved value is never a number. A count read
  from a source that did not answer would be a guess.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .collect import DisputeRating, ReceiptRecord, Snapshot, Transfer
from .config import (
    DEMO_MAX_SECONDS,
    DEMO_MIN_SECONDS,
    DEMO_PAGE,
    DISPUTE_ROUTES,
    EXPLORER_ACCOUNT,
    EXPLORER_CONTRACT,
    EXPLORER_TX,
    GUIDE_PAGE,
    MAX_TRACED_STEPS,
    REGISTER_PAGE,
    REGISTER_ROUTE,
    REPOSITORIES,
    SOW_ROWS,
    SPRINT_START,
    STROOPS_PER_UNIT,
    SowRow,
)

MET = "met"
NOT_MET = "not_met"
NOT_MEASURED = "Not measured"
LINK_KINDS = frozenset({"tx", "contract", "account", "page", "pr", "repo", "video", "doc"})

# Which reads each metric stands on. A failure of any of them leaves the
# metric unmeasured.
_CHAIN_PARTIES = ("readiness", "platform_keys", "registry")
DEPENDS: dict[str, tuple[str, ...]] = {
    "m01": _CHAIN_PARTIES,
    "m02": _CHAIN_PARTIES,
    "m03": (*_CHAIN_PARTIES, "escrow"),
    "m04": (*_CHAIN_PARTIES, "escrow"),
    "m05": (*_CHAIN_PARTIES, "escrow", "history", "ledger"),
    "m06": (f"page:{REGISTER_PAGE}", "openapi"),
    "m07": ("params",),
    "m08": ("openapi", *_CHAIN_PARTIES, "escrow", "history", "ledger"),
    "m09": (f"page:{GUIDE_PAGE}",),
    "m10": (f"page:{DEMO_PAGE}",),
    "m11": ("github",),
}

SOURCE_WORDS: dict[str, str] = {
    "readiness": "the backend's readiness report",
    "platform_keys": "the platform's own keys on the contracts",
    "registry": "the agent registry",
    "escrow": "the escrow's payment records",
    "history": "the platform keys' transaction history",
    "ledger": "the reputation ledger",
    "params": "the live reputation settings",
    "openapi": "the backend's list of routes",
    "github": "GitHub's repository records",
}

# ── the dispute id, re-stated from app/services/dispute_rating.py ────────
DISPUTE_ID_TAG = b"orizon-dispute:v1"


def dispute_job_id(job_id: bytes, step_index: int) -> bytes:
    """`job_id[:8] ‖ sha256(job_id ‖ DISPUTE_ID_TAG ‖ uint16_be(step))[:8]` — pinned against the app in tests."""
    digest = hashlib.sha256(job_id + DISPUTE_ID_TAG + step_index.to_bytes(2, "big")).digest()
    return job_id[:8] + digest[:8]


# ── output shapes ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Link:
    label: str
    url: str
    kind: str
    tx_hash: str | None = None
    date: str | None = None

    def as_dict(self) -> dict[str, str]:
        out = {"label": self.label, "url": self.url, "kind": self.kind}
        if self.tx_hash is not None:
            out["tx_hash"] = self.tx_hash
        if self.date is not None:
            out["date"] = self.date
        return out


@dataclass
class Metric:
    row: SowRow
    achieved: str
    status: str
    method: str
    links: list[Link]
    reason: str | None = None
    counted: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    unmeasured: dict[str, str] = field(default_factory=dict)  # source -> what failed

    @property
    def measured(self) -> bool:
        return not self.unmeasured

    def block(self) -> dict[str, Any]:
        """This metric as one entry of the evidence index's frozen `metrics` array."""
        out: dict[str, Any] = {
            "id": self.row.id,
            "category": self.row.category,
            "metric": self.row.metric,
            "target": self.row.target,
            "achieved": self.achieved,
            "status": self.status,
        }
        if self.status == NOT_MET:
            out["reason"] = self.reason or ""
        out["method"] = self.method
        out["links"] = [link.as_dict() for link in self.links]
        return out


def tx_link(label: str, tx_hash: str, date: str) -> Link:
    return Link(label, EXPLORER_TX.format(tx_hash), "tx", tx_hash, date)


def contract_link(label: str, contract_id: str) -> Link:
    return Link(label, EXPLORER_CONTRACT.format(contract_id), "contract")


def account_link(label: str, account: str) -> Link:
    return Link(label, EXPLORER_ACCOUNT.format(account), "account")


def day(unix: int) -> str:
    return datetime.fromtimestamp(unix, UTC).strftime("%Y-%m-%d")


def iso(unix: int) -> str:
    return datetime.fromtimestamp(unix, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


SPRINT_DAY = SPRINT_START.strftime("%Y-%m-%d")
SPRINT_ISO = SPRINT_START.strftime("%Y-%m-%dT%H:%M:%SZ")


def amount(stroops: int) -> str:
    """Whole units, trailing zeros trimmed: 1140000 -> "0.114"."""
    whole, frac = divmod(abs(stroops), STROOPS_PER_UNIT)
    text = f"{whole}.{frac:07d}".rstrip("0").rstrip(".")
    return f"-{text}" if stroops < 0 else text


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


# ── who is who ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Party:
    exclusion: str | None  # "team wallet" | "platform key" | None (external)
    phrase: str  # plain language, never the address


class Rules:
    def __init__(self, snap: Snapshot) -> None:
        self.snap = snap
        self.owner_of = {a.id: a.owner for a in snap.agents}
        self.charges = self._classify_charges()

    def party(self, address: str | None) -> Party:
        if address is None:
            return Party("unknown", "an unknown account")
        role = self.snap.team.get(address)
        if role is not None:
            return Party("team wallet", f"the team's {_role(role)}")
        role = self.snap.platform.get(address)
        if role is not None:
            return Party("platform key", f"the platform's {role}")
        return Party(None, "an outside operator's wallet")

    def external(self, address: str | None) -> bool:
        return address is not None and self.party(address).exclusion is None

    # ── charges ─────────────────────────────────────────────────
    def _classify_charges(self) -> list[Charge]:
        out: list[Charge] = []
        for escrow in self.snap.escrows:
            for r in escrow.receipts:
                auth = escrow.auths.get(r.auth_id)
                payer = auth.payer if auth else None
                owner = self.owner_of.get(r.agent_id)
                reasons: list[str] = []
                if payer is None:
                    reasons.append("its authorization could not be read, so its payer is unknown")
                elif payer == owner:
                    reasons.append("self-payment (the payer owns the agent)")
                elif payer == escrow.settler:
                    reasons.append("self-payment (the payer is the escrow's settler)")
                elif payer in self.snap.platform:
                    reasons.append(f"self-payment (the payer is the platform's {self.snap.platform[payer]})")
                if iso(r.settled_at) < SPRINT_ISO:
                    reasons.append(f"settled before the sprint began on {SPRINT_DAY}")
                tx = self.snap.charge_txs.get((r.escrow, r.id_hex))
                out.append(Charge(r, payer, owner, reasons, tx.tx_hash if tx else None, tx.date if tx else None))
        return out


def _role(role: str) -> str:
    """A register role read after "the team's": "team audit key" -> "audit key"."""
    return role[len("team ") :] if role.lower().startswith("team ") else role


@dataclass
class Charge:
    receipt: ReceiptRecord
    payer: str | None
    owner: str | None
    reasons: list[str]
    tx_hash: str | None
    tx_date: str | None

    @property
    def counts(self) -> bool:
        return not self.reasons

    @property
    def date(self) -> str:
        return day(self.receipt.settled_at)


def _verdict(reasons: Iterable[str]) -> str:
    listed = [r for r in reasons if r]
    return f"excluded: {'; '.join(listed)}" if listed else "counted"


# ── the metrics ──────────────────────────────────────────────────────────
def _unmeasured(row: SowRow, snap: Snapshot, method: str, links: list[Link]) -> Metric | None:
    failed = {s: snap.failures[s] for s in DEPENDS[row.id] if s in snap.failures}
    if not failed:
        return None
    words = [SOURCE_WORDS.get(s) or f"the {s.split(':', 1)[1]} page" for s in failed]
    reason = (
        f"Not measured: {_join(words)} could not be read when this ran, so this row reports no number rather "
        "than a wrong one. Run the generator again once it answers."
    )
    return Metric(row, NOT_MEASURED, NOT_MET, method, links, reason, unmeasured=failed)


def _registry_link(snap: Snapshot) -> list[Link]:
    registry = snap.contract("agent_registry")
    return [contract_link("AgentRegistry contract (every registered agent)", registry)] if registry else []


def _escrow_links(snap: Snapshot) -> list[Link]:
    return [
        contract_link(
            f"PaymentEscrow v{e.version} contract{' (live)' if e.live else ''} (every charge it recorded)", e.contract
        )
        for e in snap.escrows
    ]


def _ledger_link(snap: Snapshot) -> list[Link]:
    ledger = snap.contract("reputation_ledger")
    return [contract_link("ReputationLedger contract (every rating)", ledger)] if ledger else []


def _by_date(links: list[Link]) -> list[Link]:
    return sorted(links, key=lambda link: (link.date or "", link.label))


EXTERNAL_RULE = (
    "An agent counts only if its owner is neither one of the team's wallets (the committed register) nor a key "
    "the platform runs (network admin, dispatch signer, ratings signer and scorer, registry admin, escrow settler "
    "and admin, ledger scorer, attestation sealer)."
)


def m01(snap: Snapshot, rules: Rules) -> Metric:
    row = SOW_ROWS[0]
    method = (
        "Read every agent the AgentRegistry contract lists, with its owner, straight from testnet. " + EXTERNAL_RULE
    )
    links = _registry_link(snap)
    unmeasured = _unmeasured(row, snap, method, links)
    if unmeasured:
        return unmeasured
    counted, excluded, item_links = [], [], []
    for agent in sorted(snap.agents, key=lambda a: (a.registered_at or 0, a.id)):
        party = rules.party(agent.owner)
        verdict = "counted: outside operator" if party.exclusion is None else f"excluded: {party.exclusion}"
        reg = snap.registrations.get(agent.id)
        date = reg.date if reg else (day(agent.registered_at) if agent.registered_at else "")
        label = f"Registration of {agent.id} by {party.phrase}" + (f" — {date}" if date else "") + f" ({verdict})"
        if reg:
            item_links.append(tx_link(label, reg.tx_hash, reg.date))
        item = {"agent_id": agent.id, "owner": agent.owner, "active": agent.active, "registered": date, "label": label}
        item["register_tx"] = reg.tx_hash if reg else None
        if party.exclusion is None:
            counted.append(item)
        else:
            excluded.append({**item, "reason": party.exclusion, "role": party.phrase})
    n, total = len(counted), len(snap.agents)
    team_owners = len({e["owner"] for e in excluded})
    reason = None
    if n < 2:
        if total == 0:
            reason = "No agent is registered on the AgentRegistry contract yet."
        elif n == 0:
            reason = (
                f"No outside operator has registered an agent yet; all {plural(total, 'registered agent')} belong to "
                f"{plural(team_owners, 'wallet')} the team controls."
            )
        else:
            reason = (
                f"Only {plural(n, 'agent')} {'is' if n == 1 else 'are'} registered by outside operators; the other "
                f"{total - n} belong to wallets the team controls. The target is 2."
            )
    return Metric(
        row, str(n), MET if n >= 2 else NOT_MET, method, links + _by_date(item_links), reason, counted, excluded
    )


def m02(snap: Snapshot, rules: Rules) -> Metric:
    row = SOW_ROWS[1]
    method = "The distinct owner wallets of the registered agents, each checked the same way as the row above. " + (
        EXTERNAL_RULE.replace("An agent counts", "A wallet counts").replace("its owner is", "it is")
    )
    unmeasured = _unmeasured(row, snap, method, _registry_link(snap))
    if unmeasured:
        return unmeasured
    owners: dict[str, list[str]] = {}
    for agent in snap.agents:
        owners.setdefault(agent.owner, []).append(agent.id)
    counted, excluded, links = [], [], []
    for owner, agent_ids in sorted(owners.items(), key=lambda kv: (rules.party(kv[0]).phrase, kv[0])):
        party = rules.party(owner)
        verdict = "counted: outside operator" if party.exclusion is None else f"excluded: {party.exclusion}"
        label = f"{_sentence(party.phrase)} — owns {plural(len(agent_ids), 'agent')} ({verdict})"
        links.append(account_link(label, owner))
        item = {"owner": owner, "agents": sorted(agent_ids), "label": label}
        if party.exclusion is None:
            counted.append(item)
        else:
            excluded.append({**item, "reason": party.exclusion})
    n = len(counted)
    reason = None
    if n < 2:
        if n == 0:
            reason = (
                f"No outside operator wallet owns an agent yet; the {plural(len(snap.agents), 'registered agent')} "
                f"belong to {plural(len(excluded), 'wallet')} the team controls."
            )
        else:
            reason = "Only 1 outside operator wallet owns an agent. The target is 2."
    return Metric(row, str(n), MET if n >= 2 else NOT_MET, method, links, reason, counted, excluded)


def _settlement_method(snap: Snapshot) -> str:
    asset = (
        " Testnet settles in native XLM, not USDC: the escrow's payment asset is the native XLM asset contract."
        if snap.asset_name == "XLM"
        else ""
    )
    return (
        "Walked every id each escrow contract has issued (its id counter numbers every authorization and receipt, "
        "so this does not depend on how long the network keeps events) and read each receipt and the payer that "
        "authorized it. A v1 charge and each payout of a v2 settle count as one charge. A charge is excluded when "
        "it is a self-payment (the payer owns the agent, is the escrow's settler, or is a platform key) or settled "
        f"before the sprint began on {SPRINT_DAY}." + asset
    )


def _charge_link(snap: Snapshot, c: Charge, extra: list[str]) -> Link | None:
    if c.tx_hash is None or c.tx_date is None:
        return None
    version = next((e.version for e in snap.escrows if e.contract == c.receipt.escrow), 1)
    label = (
        f"Charge of {amount(c.receipt.amount)} {snap.asset_name} to {c.receipt.agent_id} on the v{version} escrow "
        f"— {c.date} ({_verdict([*c.reasons, *extra])})"
    )
    return tx_link(label, c.tx_hash, c.tx_date)


def _charge_item(c: Charge, reasons: list[str]) -> dict[str, Any]:
    r = c.receipt
    return {
        "escrow": r.escrow,
        "receipt_id": r.id_hex,
        "auth_id": r.auth_id,
        "job_id": r.job_id,
        "agent_id": r.agent_id,
        "amount_stroops": r.amount,
        "payer": c.payer,
        "owner": c.owner,
        "settled": iso(r.settled_at),
        "tx_hash": c.tx_hash,
        "reasons": reasons,
    }


def _missing_links_warning(snap: Snapshot, charges: list[Charge]) -> None:
    missing = [c.receipt.id_hex for c in charges if c.tx_hash is None]
    note = f"no charge transaction found for receipt(s): {', '.join(missing)}"
    if missing and note not in snap.warnings:
        snap.warnings.append(note)


def m03(snap: Snapshot, rules: Rules) -> Metric:
    row = SOW_ROWS[2]
    method = (
        _settlement_method(snap)
        + " A workflow counts when at least one of its counted charges paid an agent run by an outside operator; "
        "several charges of one job are one workflow."
    )
    links = _escrow_links(snap)
    unmeasured = _unmeasured(row, snap, method, links)
    if unmeasured:
        return unmeasured
    workflows: dict[tuple[str, str], list[Charge]] = {}
    excluded, item_links = [], []
    for c in rules.charges:
        party = rules.party(c.owner)
        extra = [] if party.exclusion is None else [f"the agent is run by {party.phrase}"]
        reasons = [*c.reasons, *extra]
        if not reasons:
            workflows.setdefault((c.receipt.escrow, c.receipt.job_id), []).append(c)
        else:
            excluded.append(_charge_item(c, reasons))
        link = _charge_link(snap, c, extra)
        if link:
            item_links.append(link)
    _missing_links_warning(snap, rules.charges)
    counted = [
        {"escrow": escrow, "job_id": job, "charges": [_charge_item(c, []) for c in cs]}
        for (escrow, job), cs in sorted(workflows.items())
    ]
    n = len(counted)
    external_agents = [a for a in snap.agents if rules.external(a.owner)]
    reason = None
    if n < 3:
        if n == 0 and not external_agents:
            reason = (
                "No agent run by an outside operator exists yet, so no workflow could be routed to one and settled. "
                f"None of the {plural(len(rules.charges), 'charge')} on record paid one."
            )
        elif n == 0:
            reason = (
                f"{plural(len(external_agents), 'agent')} run by outside operators "
                f"{'is' if len(external_agents) == 1 else 'are'} registered, but no workflow paid to one has "
                f"settled since the sprint began on {SPRINT_DAY}."
            )
        else:
            reason = (
                f"Only {plural(n, 'workflow')} paid to agents run by outside operators "
                f"{'has' if n == 1 else 'have'} settled since the sprint began. The target is 3."
            )
    return Metric(
        row, str(n), MET if n >= 3 else NOT_MET, method, links + _by_date(item_links), reason, counted, excluded
    )


def _charge_breakdown(charges: list[Charge]) -> str:
    excluded = [c for c in charges if not c.counts]
    selfpay = [c for c in excluded if any(r.startswith("self-payment") for r in c.reasons)]
    pre = [c for c in excluded if any(r.startswith("settled before") for r in c.reasons)]
    parts = []
    if selfpay:
        who = "the agent's owner" if all("owns the agent" in " ".join(c.reasons) for c in selfpay) else "a team key"
        parts.append(f"{_every(len(selfpay), len(charges))} {who} paying itself")
    if pre:
        dates = sorted(c.date for c in pre)
        span = dates[0] if dates[0] == dates[-1] else f"{dates[0]} to {dates[-1]}"
        parts.append(f"{_every(len(pre), len(charges))} settled before the sprint began ({span})")
    return _join(parts)


def _every(k: int, total: int) -> str:
    if k == total:
        return "all " + str(k) + (" were" if k != 1 else " was")
    return f"{k} {'were' if k != 1 else 'was'}"


def m04(snap: Snapshot, rules: Rules) -> Metric:
    row = SOW_ROWS[3]
    method = _settlement_method(snap)
    links = _escrow_links(snap)
    unmeasured = _unmeasured(row, snap, method, links)
    if unmeasured:
        return unmeasured
    counted = [_charge_item(c, []) for c in rules.charges if c.counts]
    excluded = [_charge_item(c, c.reasons) for c in rules.charges if not c.counts]
    item_links = [link for c in rules.charges if (link := _charge_link(snap, c, []))]
    _missing_links_warning(snap, rules.charges)
    n, total = len(counted), len(rules.charges)
    reason = None
    if n < 3:
        if total == 0:
            reason = "No charge has ever been recorded on the escrow contracts."
        elif n == 0:
            reason = (
                f"None of the {plural(total, 'charge')} on record counts: {_charge_breakdown(rules.charges)}. No "
                f"payment from a buyer to a different agent owner has settled since the sprint began."
            )
        else:
            reason = (
                f"Only {plural(n, 'charge')} of the {total} on record {'counts' if n == 1 else 'count'}; "
                f"the rest: {_charge_breakdown(rules.charges)}. The target is 3."
            )
    return Metric(
        row, str(n), MET if n >= 3 else NOT_MET, method, links + _by_date(item_links), reason, counted, excluded
    )


@dataclass(frozen=True)
class RefundPair:
    rating: DisputeRating
    charge: Charge
    refund: Transfer


def trace_dispute(rating: DisputeRating, charges: list[Charge]) -> Charge | None:
    """The charge whose job the rating's derived id was made from, preferring the rated agent's own charge."""
    try:
        derived = bytes.fromhex(rating.job_id)
    except ValueError:
        return None
    matches = []
    for c in charges:
        try:
            job = bytes.fromhex(c.receipt.job_id)
        except ValueError:
            continue
        if job[:8] != derived[:8]:
            continue
        if any(dispute_job_id(job, step) == derived for step in range(MAX_TRACED_STEPS)):
            matches.append(c)
    matches.sort(key=lambda c: (c.receipt.agent_id != rating.agent_id, c.receipt.settled_at))
    return matches[0] if matches else None


def pair_refunds(snap: Snapshot, rules: Rules) -> tuple[list[RefundPair], list[dict[str, Any]], list[Transfer]]:
    """(refunded disputes, disputes that did not count with why, transfers left over)."""
    used: set[tuple[str, str, int]] = set()
    pairs: list[RefundPair] = []
    rejected: list[dict[str, Any]] = []
    for rating in snap.dispute_ratings:
        charge = trace_dispute(rating, rules.charges)
        why: str | None = None
        refund: Transfer | None = None
        if charge is None:
            why = "its job id traces back to no charge on record"
        elif not charge.counts:
            why = "the charge it disputes does not count (" + "; ".join(charge.reasons) + ")"
        else:
            settled = iso(charge.receipt.settled_at)
            for t in snap.transfers:
                key = (t.tx_hash, t.destination, t.amount_stroops)
                if (
                    key not in used
                    and t.destination == charge.payer
                    and t.created_at >= max(settled, SPRINT_ISO)
                    and 0 < t.amount_stroops <= charge.receipt.amount
                ):
                    refund = t
                    used.add(key)
                    break
            if refund is None:
                why = "no refund from the platform to the charge's payer followed it"
        if refund is not None and charge is not None:
            pairs.append(RefundPair(rating, charge, refund))
        else:
            rejected.append({"rating": rating, "reason": why or ""})
    leftover = [t for t in snap.transfers if (t.tx_hash, t.destination, t.amount_stroops) not in used]
    return pairs, rejected, leftover


def _m05_method() -> str:
    return (
        "Read the dispute ratings the platform wrote to the ReputationLedger (from its keys' full transaction "
        "history) and traced each back, through the derived job id the backend records a dispute under, to the "
        "charge it disputes. A refund counts only when that charge counts and the platform then paid its payer "
        "back over the asset contract, no more than the charge. A transfer with no dispute behind it (such as a "
        "refund drill) does not count. The ledger's lifetime dispute count for every agent is read as a cross-check."
    )


def _m05_story(snap: Snapshot, rules: Rules, rejected: list[dict[str, Any]], leftover: list[Transfer]) -> list[str]:
    """The plain sentences behind a missed dispute-refund target."""
    out = []
    kinds = snap.rating_kinds
    total = sum(kinds.values())
    agents = len(snap.ledger_disputed)
    ledger_total = sum(snap.ledger_disputed.values())
    if not snap.dispute_ratings:
        spread = "" if not total else " (" + _join([f"{v} of kind {k}" for k, v in sorted(kinds.items())]) + ")"
        out.append(
            f"The reputation ledger holds {plural(total, 'rating')}{spread} and no dispute rating; its lifetime "
            f"dispute count is {ledger_total} across all {plural(agents, 'agent')}."
        )
    else:
        whys = _join(sorted({r["reason"] for r in rejected}))
        out.append(
            f"{_sentence(plural(len(snap.dispute_ratings), 'dispute rating'))} "
            f"{'is' if len(snap.dispute_ratings) == 1 else 'are'} on the ledger, but none counts: {whys}."
        )
    if not any(c.counts for c in rules.charges):
        out.append(f"No charge has settled since the sprint began on {SPRINT_DAY} that a dispute could refund.")
    if leftover:
        to_team = all(rules.party(t.destination).exclusion for t in leftover)
        dates = _join(sorted({t.created_at[:10] for t in leftover}))
        if len(leftover) == 1:
            out.append(
                f"The only transfer out of a platform key on record, on {dates}, went to "
                + ("a team key" if to_team else "an account")
                + " with no dispute behind it."
            )
        else:
            out.append(
                f"The {len(leftover)} transfers out of platform keys on record ({dates}) went to "
                + ("team keys" if to_team else "accounts")
                + " with no dispute behind them."
            )
    return out


def m05(snap: Snapshot, rules: Rules) -> Metric:
    row = SOW_ROWS[4]
    method = _m05_method()
    links = _ledger_link(snap)
    unmeasured = _unmeasured(row, snap, method, links)
    if unmeasured:
        return unmeasured
    pairs, rejected, leftover = pair_refunds(snap, rules)
    ledger_total = sum(snap.ledger_disputed.values())
    if ledger_total > len(snap.dispute_ratings) and not pairs:
        snap.failures.setdefault(
            "ledger",
            f"the ledger counts {ledger_total} dispute(s) but only {len(snap.dispute_ratings)} dispute rating(s) "
            "were found in the platform keys' history",
        )
        again = _unmeasured(row, snap, method, links)
        assert again is not None
        return again
    counted, excluded, item_links = [], [], []
    for p in pairs:
        rating_label = f"Dispute rating on {p.rating.agent_id} — {p.rating.created_at[:10]} (counted: refunded)"
        refund_label = (
            f"Refund of {amount(p.refund.amount_stroops)} {snap.asset_name} to the payer of the disputed charge "
            f"— {p.refund.created_at[:10]} (counted)"
        )
        item_links += [
            tx_link(rating_label, p.rating.tx_hash, p.rating.created_at[:10]),
            tx_link(refund_label, p.refund.tx_hash, p.refund.created_at[:10]),
        ]
        counted.append(
            {
                "agent_id": p.rating.agent_id,
                "dispute_job_id": p.rating.job_id,
                "rating_tx": p.rating.tx_hash,
                "charge": _charge_item(p.charge, []),
                "refund_tx": p.refund.tx_hash,
                "refund_stroops": p.refund.amount_stroops,
                "payer": p.refund.destination,
            }
        )
    for r in rejected:
        rating: DisputeRating = r["rating"]
        label = f"Dispute rating on {rating.agent_id} — {rating.created_at[:10]} (excluded: {r['reason']})"
        item_links.append(tx_link(label, rating.tx_hash, rating.created_at[:10]))
        excluded.append(
            {"kind": "dispute rating", "agent_id": rating.agent_id, "dispute_job_id": rating.job_id}
            | {"tx_hash": rating.tx_hash, "reason": r["reason"]}
        )
    for t in leftover:
        reasons = ["no dispute behind it"]
        dest = rules.party(t.destination)
        if dest.exclusion in ("team wallet", "platform key"):
            reasons.append(f"paid to a {dest.exclusion}")
        if t.created_at < SPRINT_ISO:
            reasons.append("before the sprint")
        label = (
            f"Transfer of {amount(t.amount_stroops)} {snap.asset_name} from {rules.party(t.source).phrase} to "
            f"{dest.phrase} — {t.created_at[:10]} ({_verdict(reasons)})"
        )
        item_links.append(tx_link(label, t.tx_hash, t.created_at[:10]))
        excluded.append(
            {"kind": "transfer", "source": t.source, "destination": t.destination, "amount_stroops": t.amount_stroops}
            | {"tx_hash": t.tx_hash, "date": t.created_at[:10], "reason": "; ".join(reasons)}
        )
    n = len(pairs)
    reason = None
    if n < 1:
        reason = "No dispute has been refunded yet. " + " ".join(_m05_story(snap, rules, rejected, leftover))
    return Metric(
        row, str(n), MET if n >= 1 else NOT_MET, method, links + _by_date(item_links), reason, counted, excluded
    )


def _page_state(snap: Snapshot, path: str) -> tuple[bool, str]:
    page = snap.pages[path]
    if page.status == 200:
        return True, f"the {path} page answers with no login"
    if page.status in (301, 302, 303, 307, 308):
        return False, f"the {path} page redirects (to {page.location or 'another page'}) instead of answering"
    if page.status == 404:
        return False, f"the {path} page answers HTTP 404: it has not been deployed yet"
    return False, f"the {path} page answers HTTP {page.status}"


def m06(snap: Snapshot, rules: Rules) -> Metric:
    row = SOW_ROWS[5]
    method = (
        f"Opened the {REGISTER_PAGE} page on the live dApp with no login, and checked the live backend publishes the "
        "route that builds an unsigned registration transaction for the owner's own wallet to sign. The registry "
        "contract's register needs only the owner's signature, no admin."
    )
    base = [Link("Register an agent page (opens with no login)", snap.page_urls[REGISTER_PAGE], "page")]
    base += [Link("Live backend route list", snap.urls["openapi"], "doc")] + _registry_link(snap)
    unmeasured = _unmeasured(row, snap, method, base)
    if unmeasured:
        return unmeasured
    page_ok, page_text = _page_state(snap, REGISTER_PAGE)
    route_ok = snap.openapi_routes is not None and REGISTER_ROUTE in snap.openapi_routes
    links = list(base)
    non_admin = sorted(
        (reg for reg in snap.registrations.values() if reg.signer and reg.signer != snap.registry_admin),
        key=lambda reg: (reg.date, reg.agent_id),
    )
    if non_admin:
        latest = non_admin[-1]
        links.append(
            tx_link(
                f"Registration of {latest.agent_id} signed by {rules.party(latest.signer).phrase}, not the registry "
                f"admin — {latest.date}",
                latest.tx_hash,
                latest.date,
            )
        )
    met = page_ok and route_ok
    reason = None
    if not met:
        parts = [] if page_ok else [page_text]
        if not route_ok:
            parts.append("the live backend does not publish the route that builds a registration transaction")
        reason = _sentence(_join(parts)) + "."
    counted = [{"page": REGISTER_PAGE, "status": snap.pages[REGISTER_PAGE].status, "route": REGISTER_ROUTE}]
    counted[0]["route_live"] = route_ok
    counted[0]["registrations_not_signed_by_admin"] = len(non_admin)
    return Metric(row, "Yes" if met else "No", MET if met else NOT_MET, method, links, reason, counted)


def m07(snap: Snapshot, rules: Rules) -> Metric:
    row = SOW_ROWS[6]
    method = (
        "Read the live reputation settings the router applies (whether the floor is on, and its value) from the "
        "deployed API. The router scores each agent from the ReputationLedger's on-chain average and leaves out "
        "any agent below the floor."
    )
    params = snap.params or {}
    floor = params.get("floor_bps")
    label = "Live reputation settings" + (f" (floor {floor} bps)" if isinstance(floor, int) else "")
    links = [Link(label, snap.urls["params"], "doc"), *_ledger_link(snap)]
    unmeasured = _unmeasured(row, snap, method, links)
    if unmeasured:
        return unmeasured
    enabled = params.get("enabled") is True
    has_floor = isinstance(floor, int) and not isinstance(floor, bool) and floor > 0
    met = enabled and has_floor
    reason = None
    if not met:
        if not enabled:
            reason = "The live reputation settings say reputation-gated routing is switched off."
        else:
            reason = "The live reputation settings name no floor, so no agent can be left out for low reputation."
    counted = [{"enabled": params.get("enabled"), "floor_bps": floor, "prior_bps": params.get("prior_bps")}]
    return Metric(row, "Yes" if met else "No", MET if met else NOT_MET, method, links, reason, counted)


def m08(snap: Snapshot, rules: Rules, refunds: Metric) -> Metric:
    row = SOW_ROWS[7]
    method = (
        "Checked that the live backend publishes the dispute routes (open, read, uphold, reject), that its "
        "readiness report shows refunds switched on (disputes.reconcile.enabled, true only when refunds and the "
        "refund sweep are both on), and that at least one dispute has been refunded on-chain (the dispute refund "
        "row above)."
    )
    links = [
        Link("Live backend route list", snap.urls["openapi"], "doc"),
        Link("Live backend readiness report", snap.urls["readiness"], "doc"),
        *_ledger_link(snap),
    ]
    unmeasured = _unmeasured(row, snap, method, links)
    if unmeasured:
        return unmeasured
    routes = snap.openapi_routes or set()
    missing = [r for r in DISPUTE_ROUTES if r not in routes]
    disputes = (snap.readiness or {}).get("disputes")
    reconcile = disputes.get("reconcile") if isinstance(disputes, dict) else None
    refunds_on = isinstance(reconcile, dict) and reconcile.get("enabled") is True
    refunded = refunds.status == MET
    met = not missing and refunds_on and refunded
    reason = None
    if not met:
        parts = []
        if missing:
            parts.append("the live backend does not publish every dispute route")
        if not refunds_on:
            parts.append(
                "the live backend does not report refunds as switched on"
                + (" (its readiness report predates the refund switch)" if not isinstance(disputes, dict) else "")
            )
        if not refunded:
            parts.append("no dispute has been refunded on-chain yet (see the dispute refund row)")
        lead = "The dispute window is live, but " if not missing else ""
        reason = (lead + _join(parts) + ".") if lead else _sentence(_join(parts)) + "."
    counted = [{"missing_routes": missing, "refunds_on": refunds_on, "refunded_disputes": refunds.achieved}]
    return Metric(row, "Yes" if met else "No", MET if met else NOT_MET, method, links, reason, counted)


def m09(snap: Snapshot, rules: Rules) -> Metric:
    row = SOW_ROWS[8]
    method = f"Opened the {GUIDE_PAGE} page on the live dApp with no login; published means it answers there."
    links = [Link('"List your agent on Orizon" guide', snap.page_urls[GUIDE_PAGE], "page")]
    unmeasured = _unmeasured(row, snap, method, links)
    if unmeasured:
        return unmeasured
    ok, text = _page_state(snap, GUIDE_PAGE)
    reason = None if ok else _sentence(text) + "."
    counted = [{"page": GUIDE_PAGE, "status": snap.pages[GUIDE_PAGE].status}]
    return Metric(row, "Yes" if ok else "No", MET if ok else NOT_MET, method, links, reason, counted)


_DEMO_STATE = re.compile(r'\bdata-demo="([a-z]+)"')
_ISO_DURATION = re.compile(r'\bdatetime="PT(?:(\d+)M)?(?:(\d+)S)?"', re.IGNORECASE)


def demo_state(html: str) -> tuple[str | None, int | None]:
    """(the page's `data-demo` state, the video's running time in seconds), each None when absent.

    The frontend's /demo article carries `data-demo="published"` or
    `"unpublished"` (components/demo/demo-article.tsx), and a published one
    shows the running time as `<time dateTime="PT3M42S">`.
    """
    state = _DEMO_STATE.search(html)
    seconds = None
    for m in _ISO_DURATION.finditer(html):
        if m.group(1) or m.group(2):
            seconds = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
            break
    return (state.group(1) if state else None), seconds


def m10(snap: Snapshot, rules: Rules) -> Metric:
    row = SOW_ROWS[9]
    method = (
        f"Opened the {DEMO_PAGE} page on the live dApp with no login and read the published marker the page renders "
        f'(data-demo="published") and the video\'s running time, which must be 3 to 5 minutes.'
    )
    links = [Link("Demo page", snap.page_urls[DEMO_PAGE], "page")]
    unmeasured = _unmeasured(row, snap, method, links)
    if unmeasured:
        return unmeasured
    ok, text = _page_state(snap, DEMO_PAGE)
    state, seconds = demo_state(snap.pages[DEMO_PAGE].text) if ok else (None, None)
    reason = None
    if not ok:
        reason = _sentence(text) + "."
    elif state is None:
        reason = (
            f"The {DEMO_PAGE} page answers but carries no published marker (data-demo), so the video cannot be "
            "confirmed as published."
        )
    elif state != "published":
        reason = f"The {DEMO_PAGE} page is live but says the video has not been published yet."
    elif seconds is None:
        reason = f"The {DEMO_PAGE} page says published but shows no running time for the video."
    elif not DEMO_MIN_SECONDS <= seconds <= DEMO_MAX_SECONDS:
        reason = f"The published video runs {seconds} seconds, outside the 3 to 5 minutes the SOW asks for."
    met = reason is None
    counted = [{"page": DEMO_PAGE, "status": snap.pages[DEMO_PAGE].status, "state": state, "seconds": seconds}]
    return Metric(row, "Yes" if met else "No", MET if met else NOT_MET, method, links, reason, counted)


def m11(snap: Snapshot, rules: Rules) -> Metric:
    row = SOW_ROWS[10]
    method = (
        "Asked GitHub which licence it detects on each repository: the frontend, backend and smart contracts the "
        "SOW names, and the example agent. Each must be MIT."
    )
    base = [Link(f"{_sentence(role)} repository", f"https://github.com/{name}", "repo") for name, role in REPOSITORIES]
    unmeasured = _unmeasured(row, snap, method, base)
    if unmeasured:
        return unmeasured
    links, counted, excluded, missing = [], [], [], []
    for name, role in REPOSITORIES:
        answer = snap.repos[name]
        spdx = ((answer.obj().get("license") or {}) if answer.ok else {}).get("spdx_id")
        if answer.status == 404:
            text, why = "not found on GitHub", "not found"
        elif spdx == "MIT":
            text, why = "MIT licence detected", None
        elif spdx in (None, "NOASSERTION"):
            text, why = "GitHub detects no licence", "no licence detected"
        else:
            text, why = f"licence detected: {spdx}, not MIT", f"licence {spdx}"
        links.append(Link(f"{_sentence(role)} repository — {text}", f"https://github.com/{name}", "repo"))
        item = {"repository": name, "role": role, "status": answer.status, "spdx_id": spdx}
        if why is None:
            counted.append(item)
        else:
            excluded.append({**item, "reason": why})
            missing.append(f"the {role} ({why})")
    met = not missing
    reason = None
    if not met:
        reason = (
            f"{len(missing)} of the {len(REPOSITORIES)} repositories {'has' if len(missing) == 1 else 'have'} no MIT "
            f"licence that GitHub recognises: {_join(missing)}."
        )
    return Metric(row, "Yes" if met else "No", MET if met else NOT_MET, method, links, reason, counted, excluded)


def measure(snap: Snapshot) -> list[Metric]:
    """All eleven, in SOW order."""
    rules = Rules(snap)
    refunds = m05(snap, rules)
    return [
        m01(snap, rules),
        m02(snap, rules),
        m03(snap, rules),
        m04(snap, rules),
        refunds,
        m06(snap, rules),
        m07(snap, rules),
        m08(snap, rules, refunds),
        m09(snap, rules),
        m10(snap, rules),
        m11(snap, rules),
    ]

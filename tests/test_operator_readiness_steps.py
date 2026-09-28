"""The seven readiness steps, one at a time (story 5.02).

Each builder is a pure function of the sub-reads it is handed, so every status
a step can take is driven here directly, without a route or a chain. The
properties pinned on every one of them:

  * the status is the one the design gives that situation — in particular a
    read that failed or degraded is `unknown`, never `failed`;
  * anything `todo` or `failed` carries a concrete next action;
  * nothing names the bound URL or its host.

`ready` is tested against the step list alone, so its rule — the first five
done, the last two irrelevant — is checked independently of how any one step
got its status.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.services import operator_readiness as readiness
from app.services import registry_sync, reputation_svc
from app.services.binding_store import BindingRecord
from app.services.settlement_svc import SettlementEntry, SettlementEvidence
from app.state import state

AGENT = "readiness_steps"
OWNER = "GBRPYHIL2CI3FNQ4BXLFMNDLFJUNPU2HY3ZMFSHONUCEOASW7QC7OX2H"
SECRET = "s3cr3t-steps-token"
BOUND = f"https://agent.example/run?token={SECRET}"
TUNNEL = "https://random-words-here.trycloudflare.com/run"
TX = "ab" * 32

Read = readiness._Read


def _record(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": AGENT,
        "name": "Readiness steps",
        "skills": ["research"],
        "price": 500_000,  # 0.05 USDC in stroops
        "active": True,
        "owner": OWNER,
    }
    raw.update(overrides)
    return raw


@pytest.fixture(autouse=True)
def _unlisted():
    state.agents.pop(AGENT, None)
    state.agents.pop("agt_squatted", None)
    yield
    state.agents.pop(AGENT, None)
    state.agents.pop("agt_squatted", None)


def _list(raw: dict[str, Any] | None = None) -> None:
    state.add_agent(registry_sync._to_agent(raw or _record()))


def _binding(url: str = BOUND) -> BindingRecord:
    return BindingRecord(agent_id=AGENT, endpoint_url=url, owner=OWNER, bound_at=1.0, previous_endpoint_url=None)


def _endpoint(url: str | None = BOUND, probe: readiness.ProbeResult | None = None) -> readiness._Endpoint:
    return readiness._Endpoint(Read(_binding(url) if url else None), probe)


def _rep(rating: int = 90, weight: int = 10_000_000, count: int = 3) -> reputation_svc.RepInfo:
    return reputation_svc._info_from_state(AGENT, {"sum_w": rating * 100 * weight, "weight": weight, "count": count})


def _superseded() -> reputation_svc.RepInfo:
    info = _rep().model_copy(update={"stale": True, "stale_age_seconds": 3.0})
    info._superseded = True
    return info


def _entry(tx: str | None = TX, *, ledger: int = 100, excluded: bool = False) -> SettlementEntry:
    return SettlementEntry(
        job_id="00" * 16,
        auth_id=f"{ledger:032x}",
        amount_stroops=500_000,
        ledger=ledger,
        tx_hash=tx,
        at=None,
        payer=OWNER if excluded else "GCBUYERXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
        self_payment=excluded,
        exclusion="owner" if excluded else None,
    )


def _evidence(*entries: SettlementEntry, unavailable: str | None = None) -> SettlementEvidence:
    revenue = sum(e.amount_stroops for e in entries if not e.self_payment)
    return SettlementEvidence(
        agent_id=AGENT,
        asset="native",
        window_days=6.9,
        scanned_ledgers=119_000,
        entries=list(entries),
        total_stroops=revenue,
        self_payment_stroops=sum(e.amount_stroops for e in entries) - revenue,
        truncated=False,
        unavailable=unavailable,
    )


@pytest.fixture()
def ledger(monkeypatch):
    """A deployment that records ratings, so first_run can be judged."""
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CLEDGER")


OWNED = Read(OWNER)
UNREGISTERED = Read(None)
OWNER_DOWN = Read(None, "timeout")


# (step key, expected status, a zero-argument builder call) — every status
# each step can take. `_list` side effects run inside the lambda, so the
# autouse fixture keeps them per-case.
CASES: list[tuple[str, str, Any]] = [
    ("registered", "done", lambda: readiness.registered_step(OWNED)),
    ("registered", "todo", lambda: readiness.registered_step(UNREGISTERED)),
    ("registered", "unknown", lambda: readiness.registered_step(OWNER_DOWN)),
    ("registered", "unknown", lambda: readiness.registered_step(Read(None, "error"))),
    ("active", "done", lambda: (_list(), readiness.active_step(AGENT, OWNED, Read(_record())))[1]),
    ("active", "todo", lambda: readiness.active_step(AGENT, UNREGISTERED, None)),
    ("active", "todo", lambda: readiness.active_step(AGENT, OWNED, Read(_record(active=False)))),
    ("active", "todo", lambda: readiness.active_step(AGENT, OWNED, Read(_record()))),  # not indexed yet
    ("active", "failed", lambda: readiness.active_step(AGENT, OWNED, Read(_record(price=1)))),
    ("active", "failed", lambda: readiness.active_step("agt_squatted", OWNED, Read(_record(id="agt_squatted")))),
    ("active", "unknown", lambda: readiness.active_step(AGENT, OWNER_DOWN, None)),
    ("active", "unknown", lambda: readiness.active_step(AGENT, OWNED, Read(None, "timeout"))),
    ("active", "unknown", lambda: readiness.active_step(AGENT, OWNED, Read({"active": True}))),  # unreadable shape
    ("bound", "done", lambda: readiness.bound_step(_endpoint())),
    ("bound", "todo", lambda: readiness.bound_step(_endpoint(None))),
    ("bound", "unknown", lambda: readiness.bound_step(readiness._Endpoint(Read(None, "timeout"), None))),
    ("reachable", "done", lambda: readiness.reachable_step(_endpoint(probe=readiness.ProbeResult("ok", 200)))),
    ("reachable", "done", lambda: readiness.reachable_step(_endpoint(probe=readiness.ProbeResult("http_status", 405)))),
    ("reachable", "todo", lambda: readiness.reachable_step(_endpoint(None))),
    (
        "reachable",
        "failed",
        lambda: readiness.reachable_step(_endpoint(probe=readiness.ProbeResult("http_status", 502))),
    ),
    ("reachable", "failed", lambda: readiness.reachable_step(_endpoint(probe=readiness.ProbeResult("timeout")))),
    ("reachable", "unknown", lambda: readiness.reachable_step(_endpoint(probe=None))),
    ("reachable", "unknown", lambda: readiness.reachable_step(readiness._Endpoint(Read(None, "error"), None))),
    ("routable", "done", lambda: readiness.routable_step(OWNED, Read(_rep()))),
    ("routable", "done", lambda: readiness.routable_step(OWNED, Read(reputation_svc._prior_info(AGENT)))),
    ("routable", "todo", lambda: readiness.routable_step(UNREGISTERED, None)),
    ("routable", "failed", lambda: readiness.routable_step(OWNED, Read(_rep(rating=5, weight=500_000_000)))),
    ("routable", "unknown", lambda: readiness.routable_step(OWNED, Read(None, "timeout"))),
    ("routable", "unknown", lambda: readiness.routable_step(OWNED, Read(reputation_svc._prior_info(AGENT, True)))),
    ("routable", "unknown", lambda: readiness.routable_step(OWNED, Read(_superseded()))),
    ("routable", "unknown", lambda: readiness.routable_step(OWNER_DOWN, None)),
    ("first_run", "done", lambda: readiness.first_run_step(OWNED, Read(_rep(count=1)))),
    ("first_run", "done", lambda: readiness.first_run_step(OWNED, Read(_superseded()))),  # count only grows
    ("first_run", "todo", lambda: readiness.first_run_step(OWNED, Read(reputation_svc._prior_info(AGENT)))),
    ("first_run", "todo", lambda: readiness.first_run_step(UNREGISTERED, None)),
    ("first_run", "unknown", lambda: readiness.first_run_step(OWNED, Read(reputation_svc._prior_info(AGENT, True)))),
    (
        "first_run",
        "unknown",
        lambda: readiness.first_run_step(
            OWNED, Read(reputation_svc._prior_info(AGENT).model_copy(update={"stale": True}))
        ),
    ),
    ("first_run", "unknown", lambda: readiness.first_run_step(OWNED, Read(None, "timeout"))),
    ("first_settlement", "done", lambda: readiness.first_settlement_step(OWNED, Read(_evidence(_entry())))),
    ("first_settlement", "todo", lambda: readiness.first_settlement_step(OWNED, Read(_evidence()))),
    (
        "first_settlement",
        "todo",
        lambda: readiness.first_settlement_step(OWNED, Read(_evidence(_entry(excluded=True)))),
    ),
    ("first_settlement", "todo", lambda: readiness.first_settlement_step(UNREGISTERED, None)),
    ("first_settlement", "unknown", lambda: readiness.first_settlement_step(OWNED, Read(None, "timeout"))),
    (
        "first_settlement",
        "unknown",
        lambda: readiness.first_settlement_step(OWNED, Read(_evidence(unavailable="soroban rpc unreachable"))),
    ),
]

# The statuses each step can take. `registered`, `bound`, `first_run` and
# `first_settlement` have no `failed`: nothing about them is broken, only not
# yet done (or not knowable just now).
ADMITTED = {
    "registered": {"done", "todo", "unknown"},
    "active": {"done", "todo", "failed", "unknown"},
    "bound": {"done", "todo", "unknown"},
    "reachable": {"done", "todo", "failed", "unknown"},
    "routable": {"done", "todo", "failed", "unknown"},
    "first_run": {"done", "todo", "unknown"},
    "first_settlement": {"done", "todo", "unknown"},
}


def _leaks(step: readiness.Step) -> list[str]:
    text = " ".join([step.detail, step.action or "", *(step.evidence or {}).values()])
    return [needle for needle in ("agent.example", SECRET, "/run") if needle in text]


@pytest.mark.parametrize(("key", "status", "build"), CASES, ids=[f"{k}-{s}-{i}" for i, (k, s, _) in enumerate(CASES)])
def test_each_step_in_each_status(ledger, key, status, build):
    step = build()

    assert (step.key, step.status) == (key, status)
    assert step.detail.strip()
    if status in ("todo", "failed"):
        assert step.action and step.action.strip(), f"{key} {status} has no next action"
    assert _leaks(step) == []


def test_the_cases_cover_every_admitted_status_of_every_step():
    covered: dict[str, set[str]] = {}
    for key, status, _ in CASES:
        covered.setdefault(key, set()).add(status)
    assert covered == ADMITTED
    assert tuple(ADMITTED) == readiness.STEP_KEYS


# --- the content of particular answers ------------------------------------------


def test_registered_links_the_owner_on_the_explorer():
    step = readiness.registered_step(OWNED)

    assert step.evidence == {"explorer": f"https://stellar.expert/explorer/testnet/account/{OWNER}"}
    assert OWNER in step.detail


def test_an_unregistered_agent_on_testnet_is_pointed_at_friendbot():
    action = readiness.registered_step(UNREGISTERED).action or ""

    assert "friendbot.stellar.org" in action
    assert "Register page" in action


def test_bound_todo_uses_the_contract_action():
    assert readiness.bound_step(_endpoint(None)).action == "Bind an HTTPS endpoint on the Bind page"


def test_a_delisted_agent_is_told_to_relist_even_once_indexed():
    _list()  # the mirror may still hold it from before the delist

    step = readiness.active_step(AGENT, OWNED, Read(_record(active=False)))

    assert step.status == "todo"
    assert "delisted" in step.detail
    assert "Relist" in (step.action or "")


def test_first_run_counts_the_rated_steps(ledger):
    assert readiness.first_run_step(OWNED, Read(_rep(count=3))).detail == "3 rated steps on-chain (ReputationLedger)."


def test_a_refused_price_names_the_range_and_says_how_to_fix_it():
    step = readiness.active_step(AGENT, OWNED, Read(_record(price=1)))

    assert "0.001000" in step.detail  # the floor the mirror applies
    assert "Update the price" in (step.action or "")


@pytest.mark.parametrize("code", [502, 503, 504, 530])
def test_a_gateway_error_asks_whether_the_agent_process_is_running(code):
    step = readiness.reachable_step(_endpoint(probe=readiness.ProbeResult("http_status", code)))

    assert step.status == "failed"
    assert step.action == (
        f"Your endpoint answered {code} — is your agent process running, on the port your host or tunnel forwards to?"
    )


@pytest.mark.parametrize(
    "outcome", ["timeout", "connection_refused", "tls_error", "unresolvable", "connection_failed", "transport_error"]
)
def test_every_probe_failure_is_failed_with_its_own_action(outcome):
    step = readiness.reachable_step(_endpoint(probe=readiness.ProbeResult(outcome)))

    assert step.status == "failed"
    assert step.action == readiness._PROBE_FAILURES[outcome][1]


def test_an_endpoint_the_policy_now_refuses_names_the_rule_not_the_url():
    step = readiness.reachable_step(
        _endpoint(probe=readiness.ProbeResult("endpoint_refused", rule="non_public_address"))
    )

    assert step.status == "failed"
    assert "non_public_address" in step.detail
    assert _leaks(step) == []


@pytest.mark.parametrize(
    "probe",
    [
        readiness.ProbeResult("ok", 200),
        readiness.ProbeResult("http_status", 502),
        readiness.ProbeResult("timeout"),
        None,
    ],
)
def test_a_quick_tunnel_is_warned_about_whatever_the_probe_said(probe):
    tunnel = readiness.reachable_step(_endpoint(TUNNEL, probe))
    plain = readiness.reachable_step(_endpoint(BOUND, probe))

    assert "trycloudflare" in tunnel.detail
    assert "ephemeral" in tunnel.detail
    assert "trycloudflare" not in plain.detail
    assert tunnel.status == plain.status  # a warning, not a verdict


def test_a_vanished_quick_tunnel_says_the_tunnel_has_gone():
    step = readiness.reachable_step(_endpoint(TUNNEL, readiness.ProbeResult("unresolvable", rule="unresolvable_host")))

    assert step.status == "failed"
    assert (step.action or "").startswith("Your quick tunnel has gone")


def test_a_cold_start_agent_is_routable_by_design():
    step = readiness.routable_step(OWNED, Read(reputation_svc._prior_info(AGENT)))

    assert step.status == "done"
    assert "by design" in step.detail


def test_first_run_without_a_reputation_ledger_is_unknown_not_todo(monkeypatch):
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "")

    assert readiness.first_run_step(OWNED, Read(reputation_svc._prior_info(AGENT))).status == "unknown"


def test_first_settlement_evidence_is_the_oldest_third_party_payment():
    evidence = _evidence(
        _entry("11" * 32, ledger=90, excluded=True),  # the owner paying: not revenue
        _entry("22" * 32, ledger=100),
        _entry("33" * 32, ledger=110),
    )

    step = readiness.first_settlement_step(OWNED, Read(evidence))

    assert step.status == "done"
    assert step.evidence == {
        "tx_hash": "22" * 32,
        "explorer": f"https://stellar.expert/explorer/testnet/tx/{'22' * 32}",
    }


def test_a_settlement_without_a_usable_hash_is_done_without_a_link():
    step = readiness.first_settlement_step(OWNED, Read(_evidence(_entry(None))))

    assert step.status == "done"
    assert step.evidence is None


def test_no_settlement_says_how_far_back_it_looked():
    step = readiness.first_settlement_step(OWNED, Read(_evidence()))

    assert "6.9 days" in step.detail


# --- ready -------------------------------------------------------------------------


def _steps(**statuses: str) -> tuple[readiness.Step, ...]:
    return tuple(
        readiness.Step(key, statuses.get(key, "done"), "detail", None if statuses.get(key, "done") == "done" else "act")  # type: ignore[arg-type]
        for key in readiness.STEP_KEYS
    )


def test_ready_when_the_first_five_are_done_whatever_the_last_two_say():
    assert readiness.is_ready(_steps())
    assert readiness.is_ready(_steps(first_run="todo", first_settlement="todo"))
    assert readiness.is_ready(_steps(first_run="unknown", first_settlement="unknown"))


@pytest.mark.parametrize("key", ["registered", "active", "bound", "reachable", "routable"])
@pytest.mark.parametrize("status", ["todo", "failed", "unknown"])
def test_not_ready_when_any_of_the_first_five_is_not_done(key, status):
    assert not readiness.is_ready(_steps(**{key: status}))


def test_ready_needs_the_steps_present_not_merely_absent_of_failures():
    assert not readiness.is_ready(())

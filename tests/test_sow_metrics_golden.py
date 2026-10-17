"""Every byte the generator prints and writes, pinned for a spread of worlds (story 5.05).

The numbers, sentences and links in these outputs are reported to the grant
funder, so a refactor of the generator must leave them byte-identical. Each
scenario runs `cli.main` end to end against an in-memory world and compares
the console output, the frozen block, the Markdown and the raw JSON with the
files under `tests/fixtures/sow_metrics_golden/<scenario>/`. The block shape
check's wording and the fake world's own answers are pinned the same way.

To regenerate after a deliberate change of wording or rules:
`SOW_METRICS_GOLDEN_UPDATE=1 pytest tests/test_sow_metrics_golden.py`, then
review the fixture diff line by line.
"""

from __future__ import annotations

import copy
import io
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from stellar_sdk import scval

from scripts.sow_metrics.chain import ChainReader
from scripts.sow_metrics.cli import main
from scripts.sow_metrics.config import (
    BLOCK_NAME,
    DEMO_PAGE,
    DISPUTE_ROUTES,
    GUIDE_PAGE,
    MARKDOWN_NAME,
    RAW_NAME,
    REGISTER_PAGE,
    REGISTER_ROUTE,
    REPOSITORIES,
    TESTNET_PASSPHRASE,
)
from scripts.sow_metrics.fakes import (
    A1,
    ADMIN,
    API,
    BACKEND,
    BUYER1,
    BUYER2,
    ESCROW_V1,
    ESCROW_V2,
    FRONTEND,
    GITHUB,
    HORIZON,
    JOB1,
    JOB2,
    LEDGER,
    MAINNET_PASSPHRASE,
    OP1,
    OP2,
    REGISTRY,
    RPC,
    SAC,
    SIGNER,
    TEAM_BUYER,
    TEAM_OP,
    FakeWorld,
    demo_html,
    iso,
    job,
    met_world,
    ts,
)
from scripts.sow_metrics.report import block_problems
from scripts.sow_metrics.retry import RetryPolicy
from tests.test_sow_metrics_run import run

GOLDEN = Path(__file__).parent / "fixtures" / "sow_metrics_golden"
UPDATE = os.environ.get("SOW_METRICS_GOLDEN_UPDATE") == "1"
PR91 = "https://github.com/Bl0cksmiths/Orizon-Agents-FE-Stellar/pull/91"
HASH = "ab" * 32


@dataclass
class QuirkyWorld(FakeWorld):
    """A `FakeWorld` whose named paths answer a fixed (status, JSON body), and whose adoption report carries junk."""

    overrides: dict[str, tuple[int, Any]] = field(default_factory=dict)
    junk_operators: list[Any] = field(default_factory=list)

    @classmethod
    def of(cls, world: FakeWorld) -> QuirkyWorld:
        return cls(**{f.name: getattr(world, f.name) for f in fields(FakeWorld)})

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path in self.overrides:
            status, body = self.overrides[request.url.path]
            return httpx.Response(status, json=body)
        return super().handle(request)

    def _adoption_operators(self) -> list[dict[str, Any]]:
        return [*super()._adoption_operators(), *self.junk_operators]


def _network(**drop: bool) -> dict[str, Any]:
    """The fake API's network document, less the contracts named in `drop`."""
    doc: dict[str, Any] = FakeWorld().api("/api/stellar/network").json()
    doc["contracts"] = {k: v for k, v in doc["contracts"].items() if not drop.get(k)}
    return doc


def _history_record(op_id: int, *, source: str, at: str, kind: str) -> dict[str, Any]:
    """A Horizon operation that is not a contract call."""
    return {
        "id": str(op_id),
        "paging_token": str(op_id),
        "transaction_successful": True,
        "source_account": source,
        "type": kind,
        "created_at": iso(ts(at)),
        "transaction_hash": HASH,
    }


# ── the worlds ───────────────────────────────────────────────────────────
def _missed() -> FakeWorld:
    """Most metrics miss, each for a different reason, with some of every kind of item."""
    w = FakeWorld()
    w.add_escrow(ESCROW_V2, version=2, settler=SIGNER)
    w.add_escrow(ESCROW_V1, version=1, settler=ADMIN)
    w.add_agent("house_agent", ADMIN, at="2026-04-20")
    w.add_agent("qa_agent", TEAM_OP, at="2026-09-17")
    w.add_agent("alpha", OP1, at="2026-09-19")
    w.charge_v1(ADMIN, "house_agent", 1_140_000, job("v1-a"), at="2026-05-13")
    w.charge_v1(TEAM_BUYER, "qa_agent", 300_000, job("v1-b"), at="2026-09-15")
    w.settle_v2(BUYER1, [("alpha", A1)], JOB1, at="2026-09-21T10:00:00")
    w.settle_v2(OP1, [("alpha", 700_000)], JOB2, at="2026-09-22T10:00:00")
    # A receipt whose authorization is not on record, paid to an agent the registry does not list.
    w.escrows[ESCROW_V2].ids.append(
        (
            "receipt",
            {
                "auth_id": (999).to_bytes(16, "big"),
                "agent_id": "ghost",
                "amount": 1_000,
                "job_id": job("ghost"),
                "settled_at": ts("2026-09-25"),
            },
        )
    )
    w.rate("alpha", JOB1, BUYER1, at="2026-09-23")
    w.rate("alpha", JOB2, OP1, kind="manual", at="2026-09-23")
    w.dispute("alpha", job("nowhere"), BUYER1, at="2026-09-24T09:00:00")
    w.dispute("alpha", JOB2, OP1, at="2026-09-25T09:00:00")
    w.transfer(ADMIN, TEAM_OP, 540_000, at="2026-09-12")
    w.transfer(SIGNER, BUYER2, 250_000, at="2026-09-26")
    # Seen from the signer's history but not paid by a platform key, and an operation that calls no contract.
    w.transfer(BUYER1, OP1, 10_000, at="2026-09-27", also=(SIGNER,))
    w.histories[SIGNER].append(_history_record(1_900_000, source=SIGNER, at="2026-09-28", kind="payment"))
    w.readiness = {"status": "ready", "ratings": {"writer": "scorer", "signer": SIGNER, "scorer": SIGNER}}
    w.params = {"enabled": False, "floor_bps": 5500, "prior_bps": 7000}
    w.bound = {"alpha": None}
    w.routes = {REGISTER_ROUTE, *DISPUTE_ROUTES}
    w.pages = {
        REGISTER_PAGE: (302, ""),
        GUIDE_PAGE: (404, "<html>not found</html>"),
        DEMO_PAGE: (200, demo_html("published", 400)),
    }
    names = [name for name, _ in REPOSITORIES]
    w.repos = {names[0]: (200, "MIT"), names[1]: (200, "Apache-2.0"), names[2]: (200, None)}
    return w


def _empty() -> FakeWorld:
    """Nothing registered, nothing charged, nothing rated: every count is 0."""
    w = FakeWorld()
    w.add_escrow(ESCROW_V2, version=2, settler=SIGNER)
    w.add_escrow(ESCROW_V1, version=1, settler=ADMIN)
    w.readiness = {
        "status": "ready",
        "ratings": {"signer": SIGNER, "scorer": SIGNER},
        "disputes": {"reconcile": {"enabled": False}},
    }
    w.params = {"enabled": True}
    w.routes = set(DISPUTE_ROUTES)
    w.pages = {DEMO_PAGE: (200, demo_html(None))}
    w.repos = {name: (200, "NOASSERTION") for name, _ in REPOSITORIES}
    return w


def _team_only() -> FakeWorld:
    """Only the team's agents, every charge a self-payment."""
    w = FakeWorld()
    w.add_escrow(ESCROW_V2, version=2, settler=SIGNER)
    w.add_escrow(ESCROW_V1, version=1, settler=ADMIN)
    w.add_agent("qa_agent", TEAM_OP, at="2026-09-17")
    w.add_agent("qa_two", TEAM_OP, at="2026-09-18")
    w.settle_v2(TEAM_OP, [("qa_agent", 400_000)], job("self-1"), at="2026-09-20T10:00:00")
    w.settle_v2(TEAM_OP, [("qa_two", 400_000)], job("self-2"), at="2026-09-21T10:00:00")
    w.rate("qa_agent", job("self-1"), TEAM_OP, at="2026-09-22")
    w.readiness = {"status": "ready", "ratings": {"signer": SIGNER, "scorer": SIGNER}, "disputes": {}}
    w.params = {"enabled": True, "floor_bps": 0}
    w.routes = {"GET /readiness"}
    w.pages = {
        REGISTER_PAGE: (200, "<html>register</html>"),
        GUIDE_PAGE: (500, "<html>error</html>"),
        DEMO_PAGE: (200, demo_html("unpublished")),
    }
    w.repos = {name: (200, "MIT") for name, _ in REPOSITORIES}
    return w


def _outsiders_unpaid() -> FakeWorld:
    """Two outside operators with three agents, none bound and none paid; one transfer, to a team key."""
    w = _team_only()
    w.add_agent("alpha", OP1, at="2026-09-19")
    w.add_agent("beta", OP2, at="2026-09-20")
    w.add_agent("gamma", OP2, at="2026-09-20")
    w.transfer(ADMIN, TEAM_OP, 540_000, at="2026-09-01")
    w.bound = {"alpha": False, "beta": False}
    w.routes = set(DISPUTE_ROUTES)
    w.pages[DEMO_PAGE] = (200, demo_html("published"))
    return w


def _partly_bound() -> FakeWorld:
    """Outside agents some bound, some unknown, one not; one counted workflow."""
    w = met_world()
    w.bound = {"alpha": True, "beta": None, "gamma": False}
    w.drop(w.marks["settle2"])
    w.drop(w.marks["settle3"])
    w.escrows[ESCROW_V2].ids = w.escrows[ESCROW_V2].ids[:2]
    return w


def _v1_live() -> FakeWorld:
    """The live API still names escrow v1, and the adoption report answers 404."""
    w = QuirkyWorld.of(met_world())
    w.live_escrow = ESCROW_V1
    w.overrides = {"/api/ecosystem/adoption": (404, {"detail": "Not Found"})}
    return w


def _odd_deployment() -> FakeWorld:
    """Odd answers from the deployment: params not an object, no route list, GitHub down, junk in the report."""
    w = QuirkyWorld.of(met_world())
    w.overrides = {
        "/api/stellar/reputation/params": (200, ["not", "an", "object"]),
        "/openapi.json": (404, {"detail": "Not Found"}),
    }
    w.github_status = 503
    w.junk_operators = [
        "not an operator",
        {"owner": OP1, "agents": "not a list"},
        {"owner": OP1, "agents": ["not an agent", {"agent_id": 7}, {"agent_id": "delta", "bound": "yes"}]},
    ]
    return w


def _no_contracts() -> FakeWorld:
    """The live API names neither the agent registry nor the attestation registry."""
    w = QuirkyWorld.of(met_world())
    w.overrides = {"/api/stellar/network": (200, _network(agent_registry=True, attestation_registry=True))}
    return w


def _unreadable() -> FakeWorld:
    """Reads fail across sources: every metric that stands on them is not measured."""
    w = met_world()
    w.horizon_down_for = {SIGNER}
    w.github_status = 500
    w.api_down = {"/api/stellar/reputation/params", "/api/ecosystem/adoption"}
    w.simulate_down_for = {(w.live_escrow, "version")}
    return w


def _keys_unreadable() -> FakeWorld:
    """The registry admin and the contracts' instance storage cannot be read."""
    w = met_world()
    w.simulate_down_for = {(REGISTRY, "admin")}
    w.ledger_entries_down = True
    return w


def _mainnet() -> FakeWorld:
    w = met_world()
    w.api_passphrase = MAINNET_PASSPHRASE
    return w


SCENARIOS: dict[str, tuple[Callable[[], FakeWorld], tuple[str, ...], str | None]] = {
    "met-published": (met_world, ("--print-block",), "--publish-external"),
    "met-withheld": (met_world, ("--pending-link", f"m09={PR91}"), None),
    "missed": (_missed, ("--pending-link", f"m09={PR91}", "--pending-link", f"m06={PR91}"), "--publish-external"),
    "missed-withheld": (_missed, (), None),
    "empty": (_empty, (), "--publish-external"),
    "team-only": (_team_only, (), "--publish-external"),
    "outsiders-unpaid": (_outsiders_unpaid, (), "--publish-external"),
    "partly-bound": (_partly_bound, (), "--publish-external"),
    "v1-live": (_v1_live, (), "--publish-external"),
    "odd-deployment": (_odd_deployment, (), "--publish-external"),
    "no-contracts": (_no_contracts, (), "--publish-external"),
    "unreadable": (_unreadable, (), None),
    "keys-unreadable": (_keys_unreadable, (), None),
    "mainnet": (_mainnet, (), None),
    "bad-escrow": (met_world, ("--escrow", "not-a-contract"), None),
}


def _pin(folder: Path, files: dict[str, str]) -> None:
    """Compare every file with its pinned copy (or rewrite the pins under SOW_METRICS_GOLDEN_UPDATE=1)."""
    if UPDATE:
        folder.mkdir(parents=True, exist_ok=True)
        for stale in folder.iterdir():
            stale.unlink()
        for file_name, text in files.items():
            (folder / file_name).write_text(text, encoding="utf-8")
    pinned = {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.iterdir())}
    assert sorted(files) == sorted(pinned)
    for file_name, text in files.items():
        assert text == pinned[file_name], f"{folder.name}/{file_name} changed"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_output_byte_is_unchanged(tmp_path: Path, name: str) -> None:
    build, extra, consent = SCENARIOS[name]
    out = run(build(), tmp_path, *extra, consent=consent)
    files = {"stdout.txt": f"exit code {out.code}\n" + out.out.replace(str(tmp_path), "<tmp>")}
    for written in (BLOCK_NAME, MARKDOWN_NAME, RAW_NAME):
        path = out.dir / written
        if path.exists():
            files[written] = path.read_text(encoding="utf-8")
    _pin(GOLDEN / name, files)


def test_the_console_alone_is_unchanged(tmp_path: Path) -> None:
    """No --out-dir: nothing is written, and --print-block prints the block."""
    stream = io.StringIO()
    code = main(
        [
            *("--api", API, "--backend", BACKEND, "--frontend", FRONTEND, "--rpc-url", RPC),
            *("--horizon-url", HORIZON, "--github-api", GITHUB, "--print-block"),
            *("--team-register", str(FakeWorld.write_register(tmp_path / "team_wallets.json"))),
        ],
        transport=met_world().transport(),
        stream=stream,
        sleep=lambda _s: None,
        now=lambda: 1_790_000_000.0,
    )
    assert list(tmp_path.iterdir()) == [tmp_path / "team_wallets.json"]
    _pin(GOLDEN / "console-only", {"stdout.txt": f"exit code {code}\n{stream.getvalue()}"})


# ── the block shape check ────────────────────────────────────────────────
def _link(block: list[dict[str, Any]], **change: Any) -> list[dict[str, Any]]:
    """The block with m03's first tx link (after its two escrow links) changed: a key set to None is removed."""
    link = block[2]["links"][2]
    for key, value in change.items():
        if value is None:
            link.pop(key, None)
        else:
            link[key] = value
    return block


def _entry(index: int, **change: Any) -> Callable[[list[dict[str, Any]]], Any]:
    def mutate(block: list[dict[str, Any]]) -> Any:
        for key, value in change.items():
            if value is None:
                block[index].pop(key, None)
            else:
                block[index][key] = value
        return block

    return mutate


SHAPES: dict[str, Callable[[list[dict[str, Any]]], Any]] = {
    "valid": lambda b: b,
    "not a list": lambda b: {"metrics": b},
    "too short": lambda b: b[:10],
    "entry not an object": lambda b: [*b[:3], "m04", *b[4:]],
    "keys reordered": lambda b: [{"category": b[0]["category"], **b[0]}, *b[1:]],
    "sow wording changed": _entry(1, metric="Something else", category="Other", target="≥ 1", id="m99"),
    "status unknown": _entry(3, status="partly"),
    "achieved and method empty": _entry(4, achieved=" ", method=7),
    "miss without a reason": _entry(5, status="not_met"),
    "miss with an empty reason": _entry(5, status="not_met", reason=" "),
    "links not a list": _entry(6, links="none"),
    "link not an object": _entry(7, links=["https://example.com"]),
    "link keys wrong": lambda b: _link(b, extra="x"),
    "link keys out of order": lambda b: [
        *b[:2],
        {**b[2], "links": [{"url": b[2]["links"][0]["url"], "label": "Some contract", "kind": "contract"}]},
        *b[3:],
    ],
    "link kind unknown": lambda b: _link(b, kind="blog"),
    "link url not https": lambda b: _link(b, url="http://stellar.expert/x"),
    "link url not a string": lambda b: _link(b, url=5),
    "tx url not the explorer": lambda b: _link(b, url="https://example.com/tx/" + "a" * 64),
    "label empty": lambda b: _link(b, label=""),
    "label not a string": lambda b: _link(b, label=3),
    "label with an address": lambda b: _link(b, label="Paid by " + SIGNER),
    "label with a hash": lambda b: _link(b, label="Charge " + "A" * 64),
    "tx_hash not hex": lambda b: _link(b, tx_hash="zz"),
    "tx_hash on a page link": lambda b: _link(b, kind="page", url="https://front.test/x"),
    "tx_hash not the url's": lambda b: _link(b, tx_hash="c" * 64),
    "tx link without tx_hash": lambda b: _link(b, tx_hash=None),
    "date not a day": lambda b: _link(b, date="21 Sep 2026"),
}


def test_the_shape_check_words_every_problem_exactly(tmp_path: Path) -> None:
    block = run(met_world(), tmp_path).block()
    found = {name: block_problems(mutate(copy.deepcopy(block))) for name, mutate in SHAPES.items()}
    assert found["valid"] == []
    _pin(GOLDEN / "shape-problems", {"problems.json": json.dumps(found, indent=2, ensure_ascii=False) + "\n"})


# ── the fake world's own answers ─────────────────────────────────────────
def _answers() -> list[list[Any]]:
    """What the fake world answers to a spread of requests, each as [what, status, body] or [what, error]."""
    w = met_world()
    w.api_down = {"/down"}
    w.pages["/moved"] = (308, "")
    w.repos["Some/private"] = (403, None)
    w.repos["Some/unlicensed"] = (200, None)
    w.horizon_down_for = {OP2}
    w.simulate_down_for = {(LEDGER, "rep_state")}
    seen: list[list[Any]] = []

    def record(request: httpx.Request) -> httpx.Response:
        response = w.handle(request)
        text = response.text
        if request.url.path == "/openapi.json":  # its paths are built from a set: compare them in a fixed order
            text = json.dumps(json.loads(text), sort_keys=True)
        seen.append([f"{request.method} {request.url}", response.status_code, text])
        return response

    def attempt(what: str, call: Callable[[], Any]) -> None:
        try:
            call()
        except Exception as exc:
            seen.append([what, f"{type(exc).__name__}: {exc}"])

    retry = RetryPolicy(attempts=1, sleep=lambda _s: None)
    with httpx.Client(transport=httpx.MockTransport(record)) as http:
        chain = ChainReader(client=http, rpc_url=RPC, horizon_url=HORIZON, retry=retry)
        for url in (
            f"{API}/api/stellar/network",
            f"{API}/api/stellar/reputation/params",
            f"{API}/api/ecosystem/adoption",
            f"{API}/api/nothing",
            f"{API}/down",
            f"{BACKEND}/readiness",
            f"{BACKEND}/openapi.json",
            f"{BACKEND}/down",
            f"{BACKEND}/nothing",
            f"{FRONTEND}{REGISTER_PAGE}",
            f"{FRONTEND}/moved",
            f"{FRONTEND}/nothing",
            f"{GITHUB}/repos/{REPOSITORIES[0][0]}",
            f"{GITHUB}/repos/Some/private",
            f"{GITHUB}/repos/Some/unlicensed",
            f"{GITHUB}/repos/Some/missing",
            f"{HORIZON}/",
            f"{HORIZON}/accounts/{SIGNER}/operations?limit=2",
            f"{HORIZON}/accounts/{SIGNER}/operations?limit=2&cursor=1000010",
            f"{HORIZON}/accounts/{OP2}/operations",
            f"{HORIZON}/accounts/{BUYER2}/operations",
            f"{HORIZON}/nothing",
        ):
            attempt(f"GET {url}", partial(http.get, url))
        attempt("POST to the API", lambda: http.post(f"{API}/api/stellar/network"))
        attempt("GET elsewhere", lambda: http.get("https://elsewhere.test/"))
        attempt("rpc getHealth", lambda: http.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}))
        receipt, auth, gone = ((n).to_bytes(16, "big") for n in (1, 0, 99))
        for contract, fn, args in (
            (REGISTRY, "list_ids", []),
            (REGISTRY, "admin", []),
            (REGISTRY, "get", [scval.to_symbol("alpha")]),
            (REGISTRY, "get", [scval.to_symbol("nobody")]),
            (REGISTRY, "price", []),
            (ESCROW_V2, "version", []),
            (ESCROW_V1, "version", []),
            (ESCROW_V2, "receipt", [scval.to_bytes(receipt)]),
            (ESCROW_V2, "receipt", [scval.to_bytes(auth)]),
            (ESCROW_V2, "authorization", [scval.to_bytes(auth)]),
            (ESCROW_V1, "authorization", [scval.to_bytes(auth)]),
            (ESCROW_V2, "authorization", [scval.to_bytes(receipt)]),
            (ESCROW_V2, "receipt", [scval.to_bytes(gone)]),
            (ESCROW_V2, "nonce", []),
            (LEDGER, "rep_state", [scval.to_symbol("alpha")]),
            (LEDGER, "avg_bps", []),
            (SAC, "balance", []),
        ):
            attempt(f"simulate {fn}", partial(chain.simulate, contract, fn, args))
        for contract in (LEDGER, ESCROW_V2, REGISTRY, SAC):
            attempt("instance storage", partial(chain.instance_storage, contract))
        w.simulate_down = True
        attempt("simulate while down", lambda: chain.simulate(REGISTRY, "admin"))
        w.ledger_entries_down = True
        attempt("instance storage while down", lambda: chain.instance_storage(LEDGER))
        w.rpc_down = True
        attempt("rpc while down", lambda: chain.rpc_passphrase())
    assert TESTNET_PASSPHRASE in seen[0][2]
    return seen


def test_the_fake_world_answers_exactly() -> None:
    _pin(GOLDEN / "fake-answers", {"answers.json": json.dumps(_answers(), indent=1, ensure_ascii=False) + "\n"})

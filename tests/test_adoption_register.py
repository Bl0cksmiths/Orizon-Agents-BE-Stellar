"""The team wallet register — the declaration the adoption metric's honesty rests on.

"External means external": an agent owned by any wallet in the register is
never counted as externally operated. So the tests pin the two ways the
register can quietly stop protecting that rule — the committed file drifting
from what the team actually declared, and a malformed entry being accepted
(one of our wallets then reads as an outside operator's) — and that a bad
register refuses boot rather than serving a wrong count.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.services import adoption_svc
from app.services.adoption_svc import TeamRegisterError, load_team_register

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every wallet the team has declared, each verified against its cited source.
DECLARED = {
    "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV",
    "GDB4N25UYM3YNTTAWX7LSGI2P7OR62QZQXRNQWAGF5TFVENDKCTTCDHP",
    "GB5MKHDFLJZ6OFPAHM7R4HGBUPFV5PZYL3W27VTIUZZ25JMQSDZBKCMR",
    "GBWMD26IB6CMG3JO3HU7SD7ZJSTF4BIJ5JS77ANMLJ52M6FV6K3J7BQJ",
    "GBI2I3WLMP2Q6L26G7CBKRPP5WJ6G3GGYJHWALOJ7D6EBRGL5OZAADBH",
    "GDJHP2I6NRCWYZTB3ZOXRE74V4M4EGXRYORGNPTGQ6BVNJNSSJO4PKXJ",
    # The 2026-09-15 x402 escrow spike's three random keys: spike_97437's owner,
    # the payer that authorized against it, and the payer that authorized
    # against orizon_batch and revoked 45 seconds later.
    "GA5LEGIRHKZGDKGQ4XHBEMU2Z7BGDX7AE2XUWDXB3V6TCOVTD2LZMQ2M",
    "GDWE6IDZ73VSMH6F75IDVA5BDAC7UJI3TOZC23VGAOXNYYHC6NDCIWRX",
    "GB4K6YRHDHB2HHNM3E7UUZJU5JP3MSQE3GXKMEA5IT4AM45D23YKAYKK",
    "GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP",
    # UAT story 6.07's own keys (BE#112): the operator that owns qa607_ok and
    # qa607_hang, and the buyer that paid for its escrow v2 runs. Left out, the
    # QA test agents counted as outside operators.
    "GBE6AUTEQDC7HN2453JY4SCPMMGDVAXIX7IOXLQM7K3KTVLL5R3UOQ4J",
    "GAGOZVEZ43HDMIU367HADCNRD6O425JUX3PQOEZEDYZP5HFKXXJ7HJNC",
}

GOOD = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
OTHER = "GBWMD26IB6CMG3JO3HU7SD7ZJSTF4BIJ5JS77ANMLJ52M6FV6K3J7BQJ"


def _entry(address: Any = GOOD, role: Any = "role", evidence: Any = "evidence") -> dict[str, Any]:
    return {"address": address, "role": role, "evidence": evidence}


def _write(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "team_wallets.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def test_the_committed_register_loads_and_holds_every_declared_wallet() -> None:
    wallets = load_team_register()

    assert {w.address for w in wallets} == DECLARED
    assert len(wallets) == len(DECLARED) == 12
    assert all(w.role and w.evidence for w in wallets)
    assert adoption_svc.TEAM_REGISTER == wallets


def test_a_valid_register_keeps_every_field_trimmed(tmp_path: Path) -> None:
    path = _write(tmp_path, {"wallets": [_entry(role="  admin ", evidence=" addresses.json ")]})

    (wallet,) = load_team_register(path)

    assert (wallet.address, wallet.role, wallet.evidence) == (GOOD, "admin", "addresses.json")


@pytest.mark.parametrize(
    ("payload", "names"),
    [
        # A checksum typo: the one mistake a human copying a key actually makes.
        ({"wallets": [_entry(address=GOOD[:-1] + "W")]}, ["entry 0", GOOD[:-1] + "W", "G-strkey"]),
        ({"wallets": [_entry(address=GOOD.lower())]}, ["entry 0", "G-strkey"]),
        ({"wallets": [_entry(address="SA" + GOOD[2:])]}, ["entry 0", "G-strkey"]),
        ({"wallets": [_entry(address=None)]}, ["entry 0", "G-strkey"]),
        ({"wallets": [_entry(), _entry(address=OTHER), _entry()]}, ["entry 2", GOOD, "duplicates entry 0"]),
        ({"wallets": [_entry(address=OTHER), _entry(role=" ")]}, ["entry 1", GOOD, "`role`"]),
        ({"wallets": [_entry(evidence="")]}, ["entry 0", GOOD, "`evidence`"]),
        ({"wallets": [_entry(evidence=7)]}, ["entry 0", "`evidence`"]),
        ({"wallets": [{"address": GOOD, "role": "r"}]}, ["entry 0", "address, role and evidence"]),
        ({"wallets": [{**_entry(), "secret": "S..."}]}, ["entry 0", "address, role and evidence"]),
        ({"wallets": ["GA7AI5"]}, ["entry 0", "expected an object"]),
        ({"wallets": []}, ["empty"]),
        ({"wallet": [_entry()]}, ["`wallets` list"]),
        ("{not json", ["unreadable"]),
    ],
)
def test_an_invalid_register_is_refused_with_the_entry_named(tmp_path: Path, payload: Any, names: list[str]) -> None:
    with pytest.raises(TeamRegisterError) as caught:
        load_team_register(_write(tmp_path, payload))

    for name in names:
        assert name in str(caught.value)


def test_a_missing_register_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TeamRegisterError, match="unreadable"):
        load_team_register(tmp_path / "team_wallets.json")


def test_an_invalid_register_refuses_boot() -> None:
    """Imported the way the service boots, with the register swapped for one
    whose address is not a key: the import fails and names the entry."""
    code = (
        "import json, pathlib\n"
        "real = pathlib.Path.read_text\n"
        "def fake(self, *a, **k):\n"
        "    if self.name == 'team_wallets.json':\n"
        "        return json.dumps({'wallets': [{'address': 'GNOTAKEY', 'role': 'r', 'evidence': 'e'}]})\n"
        "    return real(self, *a, **k)\n"
        "pathlib.Path.read_text = fake\n"
        "import app.main\n"
    )
    env = {**os.environ, "OPENAI_API_KEY": "sk-test", "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120
    )

    assert result.returncode != 0
    assert "TeamRegisterError" in result.stderr
    assert "team_wallets.json entry 0: 'GNOTAKEY' is not a valid Stellar account id" in result.stderr

#!/usr/bin/env python3
"""Fail the build when the deployed contract ids disagree across sources (story 3.07).

    python scripts/check_contract_drift.py [path/to/contracts-repo]

Three files independently claim to know where the Soroban contracts live:

  - the contracts repo's `addresses.json` / `addresses.mainnet.json`, written
    by the deploy script and therefore CANONICAL;
  - this repo's `.env.example`, which every local dev copies to `.env`;
  - this repo's `render.yaml`, which configures the live mainnet service.

Nothing keeps them in step but attention, and attention is precisely what this
class of bug survives. A wrong contract id does not announce itself: reads in
`app/services/reputation_svc.py` fail OPEN by design, so a lookup against an
address that holds no ledger returns nothing, falls back to the Bayesian prior,
and every agent then clears the routing floor. The marketplace does not look
broken — it looks like a marketplace of unrated newcomers, and the buyer is
shown a trust signal that means nothing. A failure that cannot be seen has to be
caught by a check; care is not enough.

Both networks are compared, not just the one this repo defaults to. The ticket's
own history is a single id drifting on a single network, and a check that
covered testnet alone would leave the identical trap armed on mainnet — which is
where orizons.xyz actually points.

Exit codes: 0 everything agrees, 1 drift found, 2 the check could not run (a
source was missing or could not be attributed to a network). 2 is a FAILURE, not
a skip: a check that quietly stops checking when its input disappears is the
same silence this script exists to break.

Parsing and comparison are pure functions over text, so `tests/test_contract_drift.py`
exercises every failure mode without a filesystem; only `main()` touches disk.
The script imports nothing outside the standard library, so it runs from a bare
`python3` before anyone has activated a venv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# The five contracts the backend addresses by id. These names are the canonical
# map's JSON keys; the env-style sources spell them STELLAR_<NAME>.
CONTRACTS: tuple[str, ...] = (
    "agent_registry",
    "reputation_ledger",
    "payment_escrow",
    "attestation_registry",
    "asset_sac",
)

# Every network the project has deployed to. Both are checked on every run.
NETWORKS: tuple[str, ...] = ("testnet", "mainnet")

# `public` is what stellar-sdk calls the network this repo calls `mainnet`
# (see app/stellar/client.py). They name one chain, so they fold together here
# rather than being compared as if they were two.
NETWORK_ALIASES: Mapping[str, str] = {"public": "mainnet"}

# Which canonical file carries which network. Stated rather than inferred, so a
# testnet map copied over the mainnet one is caught instead of believed.
CANONICAL_FILES: Mapping[str, str] = {
    "testnet": "addresses.json",
    "mainnet": "addresses.mainnet.json",
}

# Printed in place of an id a source does not carry. An omission is drift too:
# it produces the same unconfigured read, only reached by absence.
MISSING = "<missing>"

# Escape hatch for a clone that lives somewhere the defaults do not look.
CONTRACTS_DIR_ENV = "ORIZON_CONTRACTS_DIR"

CONTRACTS_REPO_URL = "https://github.com/Bl0cksmiths/Orizon-Agents-Smart-Contract-Stellar.git"

# network -> contract name -> contract id.
NetworkMap = dict[str, dict[str, str]]

_ENV_KEYS: Mapping[str, str] = {f"STELLAR_{name.upper()}": name for name in CONTRACTS}
_NETWORK_KEY = "STELLAR_NETWORK"
_RELEVANT: frozenset[str] = frozenset({*_ENV_KEYS, _NETWORK_KEY})

# A KEY=value assignment, anchored so prose that merely mentions a variable name
# ("set STELLAR_SIGNING_KEY via host secrets") is not mistaken for one.
_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")

_YAML_KEY = re.compile(r"^\s*-\s*key:\s*(\S+)\s*$")
_YAML_VALUE = re.compile(r"^\s*value:\s*(.*)$")


class DriftCheckError(Exception):
    """A source could not be read, parsed, or attributed to a network.

    Raised rather than swallowed: when the inputs are not all present the check
    has not passed, it has not run, and those are different outcomes.
    """


@dataclass(frozen=True)
class Drift:
    """One contract whose id is not the same everywhere it is written down."""

    network: str
    contract: str
    # Source name -> the id that source gives, or MISSING. Insertion order is
    # the order sources were declared, so the canonical value reads first.
    values: dict[str, str]

    def describe(self) -> str:
        """One self-contained line: which id, on which network, per source.

        Self-contained on purpose — "drift detected" is not actionable, and a
        line a reviewer has to reassemble from surrounding context is barely
        better. This one can be grepped out of a CI log and acted on alone.
        """
        pairs = " ".join(f"{source}={value}" for source, value in self.values.items())
        return f"{self.network} {self.contract}: {pairs}"


@dataclass(frozen=True)
class Unverified:
    """A network fewer than two sources describe, so nothing was compared.

    Reported as a failure. Printing a pass for a network no one cross-checked
    would be the quiet skip this whole check exists to prevent.
    """

    network: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Report:
    """The outcome of one comparison, renderable without touching disk."""

    # network -> the sources that were compared against each other there.
    checked: dict[str, tuple[str, ...]]
    drifts: tuple[Drift, ...]
    unverified: tuple[Unverified, ...]

    @property
    def ok(self) -> bool:
        return not self.drifts and not self.unverified

    def lines(self) -> list[str]:
        out = ["", f"Contract address drift — {len(CONTRACTS)} contract ids across {len(NETWORKS)} networks", ""]
        for network in NETWORKS:
            gap = next((u for u in self.unverified if u.network == network), None)
            if gap is not None:
                named = ", ".join(gap.sources) if gap.sources else "no source at all"
                out.append(f"  [FAIL] {network}: described by {named} — nothing to compare it against")
                continue
            compared = ", ".join(self.checked[network])
            bad = [d for d in self.drifts if d.network == network]
            if not bad:
                out.append(f"  [OK]   {network}: all {len(CONTRACTS)} contract ids agree across {compared}")
                continue
            out.append(f"  [FAIL] {network}: {len(bad)} of {len(CONTRACTS)} contract ids disagree across {compared}")
            out.extend(f"           {d.describe()}" for d in bad)
        verdict = "PASS — every source names the same contracts" if self.ok else "FAIL — see the disagreements above"
        out.extend(["", f"  VERDICT: {verdict}", ""])
        return out


def _record(source: str, block: dict[str, str], key: str, value: str) -> None:
    """Keep one relevant assignment, refusing a contradictory redefinition.

    Last-wins would let a second block quietly override the first — the same
    invisible substitution the check is looking for, committed by the parser.
    """
    if key not in _RELEVANT:
        return
    if key in block and block[key] != value:
        raise DriftCheckError(f"{source} sets {key} twice, to {block[key]!r} and {value!r}")
    block[key] = value


def _normalize_network(source: str, network: str) -> str:
    resolved = NETWORK_ALIASES.get(network, network)
    if resolved not in NETWORKS:
        known = ", ".join(NETWORKS)
        raise DriftCheckError(f"{source} declares an unknown network {network!r}; expected one of {known}")
    return resolved


def _attribute(source: str, blocks: Sequence[Mapping[str, str]]) -> NetworkMap:
    """Label each block of env assignments with the network it declares.

    The block's own STELLAR_NETWORK line is what labels it — not its position in
    the file, and not whether it is commented out today. `.env.example` keeps one
    network live and the other commented, and which is which flips when the
    project switches networks; a parser that keyed off "the commented block is
    mainnet" would silently invert on the day that changed.
    """
    out: NetworkMap = {}
    for block in blocks:
        ids = {_ENV_KEYS[key]: value for key, value in block.items() if key in _ENV_KEYS}
        if not ids:
            continue
        declared = block.get(_NETWORK_KEY)
        if declared is None:
            raise DriftCheckError(
                f"{source} carries contract ids ({', '.join(sorted(ids))}) with no {_NETWORK_KEY} to attribute them to"
            )
        network = _normalize_network(source, declared)
        if network in out:
            raise DriftCheckError(f"{source} describes the {network} network twice")
        out[network] = ids
    return out


def parse_env_example(text: str, source: str = ".env.example") -> NetworkMap:
    """Read both network blocks out of an env file, live one and commented one."""
    live: dict[str, str] = {}
    commented: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        block = live
        if line.startswith("#"):
            block = commented
            line = line.lstrip("#").strip()
        match = _ASSIGNMENT.match(line)
        if match is not None:
            _record(source, block, match.group(1), match.group(2).strip())
    return _attribute(source, [live, commented])


def parse_render_yaml(text: str, source: str = "render.yaml") -> NetworkMap:
    """Pull the contract ids out of the Render blueprint's envVars block.

    A line scanner rather than a YAML parser: PyYAML is not a declared
    dependency here, and making a pre-push check drag one in is a reliable way
    to get the check skipped. Render's schema fixes the shape — each entry is a
    `- key:` optionally followed by a `value:`, with secrets carrying
    `sync: false` instead — so scanning for that pair is sufficient and keeps
    the script runnable on a bare interpreter.
    """
    env: dict[str, str] = {}
    pending: str | None = None
    for raw in text.splitlines():
        key_match = _YAML_KEY.match(raw)
        if key_match is not None:
            pending = key_match.group(1)
            continue
        value_match = _YAML_VALUE.match(raw)
        if value_match is not None and pending is not None:
            _record(source, env, pending, value_match.group(1).strip().strip("\"'"))
            pending = None
    return _attribute(source, [env])


def parse_canonical(text: str, source: str) -> NetworkMap:
    """Read one `addresses*.json` from the contracts repo."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DriftCheckError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise DriftCheckError(f"{source} is not a JSON object")
    declared = data.get("network")
    if not isinstance(declared, str) or not declared:
        raise DriftCheckError(f'{source} has no "network" key, so its ids cannot be attributed to a network')
    # Absent contracts are left out rather than defaulted: compare() reports the
    # gap as drift, which is what an id disappearing from the deploy record is.
    ids = {name: str(data[name]) for name in CONTRACTS if name in data}
    return {_normalize_network(source, declared): ids}


def compare(sources: Mapping[str, NetworkMap]) -> Report:
    """Cross-check every contract on every network against every source naming it.

    A source silent about a network is not compared there — `render.yaml` only
    configures mainnet, and that silence is correct rather than missing data.
    """
    checked: dict[str, tuple[str, ...]] = {}
    drifts: list[Drift] = []
    unverified: list[Unverified] = []
    for network in NETWORKS:
        claiming = tuple(name for name, networks in sources.items() if networks.get(network))
        if len(claiming) < 2:
            unverified.append(Unverified(network, claiming))
            continue
        checked[network] = claiming
        for contract in CONTRACTS:
            values = {name: sources[name][network].get(contract, MISSING) for name in claiming}
            distinct = set(values.values())
            # MISSING is drift even when every source agrees on it: a contract no
            # source names is one nothing verified.
            if len(distinct) > 1 or MISSING in distinct:
                drifts.append(Drift(network, contract, values))
    return Report(checked, tuple(drifts), tuple(unverified))


def candidate_dirs(repo_root: Path, explicit: str | None) -> list[Path]:
    """Where the canonical address book might be, most deliberate first.

    An explicit path or env var is returned alone. Falling back from a location
    someone named would turn a typo into a check of the wrong clone.
    """
    if explicit:
        return [Path(explicit).expanduser()]
    from_env = os.environ.get(CONTRACTS_DIR_ENV)
    if from_env:
        return [Path(from_env).expanduser()]
    return [
        # CI checks the contracts repo out beside this one under this path.
        repo_root / "contracts",
        repo_root.parent / "Orizon-Agents-Smart-Contract-Stellar",
        repo_root.parent / "orizon-agents-Smart-Contract-Stellar",
        # The working layout on a dev box: contract repos live in their own tree
        # rather than beside the services that call them.
        repo_root.parent.parent / "Contracts-2026" / "orizon-agents-Smart-Contract-Stellar",
    ]


def find_contracts_dir(candidates: Sequence[Path]) -> Path:
    """Return the first candidate holding the canonical testnet map, or fail loudly."""
    for candidate in candidates:
        if (candidate / CANONICAL_FILES["testnet"]).is_file():
            return candidate
    looked = "\n".join(f"    - {c}" for c in candidates)
    raise DriftCheckError(
        f"cannot find the canonical address book ({CANONICAL_FILES['testnet']}). Looked in:\n"
        f"{looked}\n"
        "  Without it there is nothing to check the configured contract ids against, and an\n"
        "  unchecked id is exactly the silent mis-read this script exists to catch — so this is\n"
        "  a failure, not a skip. Clone the contracts repo:\n"
        f"    git clone {CONTRACTS_REPO_URL}\n"
        "  or point the check at an existing clone:\n"
        f"    {CONTRACTS_DIR_ENV}=/path/to/clone python scripts/check_contract_drift.py"
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DriftCheckError(f"cannot read {path}: {exc}") from exc


def load_canonical(contracts_dir: Path) -> NetworkMap:
    """Merge the two canonical maps into one source, checking each is the network it should be."""
    merged: NetworkMap = {}
    for expected, filename in CANONICAL_FILES.items():
        source = f"canonical/{filename}"
        parsed = parse_canonical(_read(contracts_dir / filename), source)
        for network, ids in parsed.items():
            if network != expected:
                raise DriftCheckError(f"{source} declares network {network!r}, but that file is the {expected} map")
            merged[network] = ids
    return merged


def load_sources(repo_root: Path, contracts_dir: Path) -> dict[str, NetworkMap]:
    """The three places a contract id is written down, canonical first."""
    return {
        "canonical": load_canonical(contracts_dir),
        ".env.example": parse_env_example(_read(repo_root / ".env.example")),
        "render.yaml": parse_render_yaml(_read(repo_root / "render.yaml")),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that every source naming a Soroban contract id names the same one."
    )
    parser.add_argument(
        "contracts_dir",
        nargs="?",
        help=(
            "clone of the contracts repo holding addresses.json and addresses.mainnet.json "
            f"(default: a few sibling paths, or ${CONTRACTS_DIR_ENV})"
        ),
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    try:
        contracts_dir = find_contracts_dir(candidate_dirs(repo_root, args.contracts_dir))
        sources = load_sources(repo_root, contracts_dir)
    except DriftCheckError as exc:
        # Loud and non-zero. The whole point of the story is that a check which
        # goes quiet when it loses its input is worse than no check at all.
        print(f"\n  CANNOT CHECK CONTRACT DRIFT: {exc}\n", file=sys.stderr)
        return 2

    report = compare(sources)
    print(f"\n  canonical map: {contracts_dir}")
    print("\n".join(report.lines()))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

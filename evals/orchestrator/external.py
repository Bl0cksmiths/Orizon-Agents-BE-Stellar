"""Public benchmarks, fetched at run time into a git-ignored cache.

The in-repo set covers real requests, non-requests and the standard injection
patterns. Harmful-request and broader jailbreak coverage comes from published
benchmarks instead of being written here: each is pinned to a revision, its
licence is recorded, and its rows never enter the repository — `fetch` writes
them to `evals/orchestrator/.cache/` (ignored), and `load` reads only from
there.

Pinned sources (licences read from each dataset card on 2026-10-06):

* deepset/prompt-injections — Apache-2.0. Train and test splits, `label` 1 = injection
  -> block (`ext_injection`), 0 = benign -> not_block (`ext_benign`).
* JailbreakBench/JBB-Behaviors — MIT. `Goal` column; harmful-behaviors.csv ->
  block (`ext_harmful`), benign-behaviors.csv -> not_block (`ext_benign`).

External cases are scored on block vs not-block only (no tier), so a benign
benchmark prompt the guard sends back for detail is not a miss. Rows outside
the API's 3..500-character bound are skipped and counted (production would
refuse them with a 422 before the guard ran), as are exact repeats.

Fetching is a free, read-only GET; it is a separate command so a run never
touches the network for data.
"""

from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .dataset import MAX_INTENT_CHARS, MIN_INTENT_CHARS, Case, DatasetError, case_from_external, validate

CACHE_DIR = Path(__file__).with_name(".cache")
HF = "https://huggingface.co"
ROWS_API = "https://datasets-server.huggingface.co/rows"


@dataclass(frozen=True)
class Benchmark:
    key: str
    repo: str
    revision: str
    license: str
    url: str


BENCHMARKS: dict[str, Benchmark] = {
    "deepset": Benchmark(
        key="deepset",
        repo="deepset/prompt-injections",
        revision="4f61ecb038e9c3fb77e21034b22511b523772cdd",
        license="apache-2.0",
        url="https://huggingface.co/datasets/deepset/prompt-injections",
    ),
    "jbb": Benchmark(
        key="jbb",
        repo="JailbreakBench/JBB-Behaviors",
        revision="886acc352a31533ffbcf4ef22c744658688086fc",
        license="mit",
        url="https://huggingface.co/datasets/JailbreakBench/JBB-Behaviors",
    ),
}


class FetchError(RuntimeError):
    """The source did not answer as pinned."""


def _cache_path(b: Benchmark, cache: Path) -> Path:
    return cache / f"{b.key}-{b.revision[:12]}.jsonl"


def _row(b: Benchmark, n: int, text: str, verdict: str, category: str, note: str) -> dict[str, object]:
    return {
        "id": f"ext-{b.key}-{n:04d}",
        "intent": text,
        "expected_verdict": verdict,
        "expected_tier": None,
        "category": category,
        "language": "und",
        "notes": note,
        "source": f"{b.repo}@{b.revision[:12]}",
    }


def _check_revision(client: httpx.Client, b: Benchmark) -> None:
    """The rows API serves the CURRENT revision only, so refuse unless it is
    the pinned one — otherwise the cache would silently hold other data."""
    r = client.get(f"{HF}/api/datasets/{b.repo}")
    r.raise_for_status()
    sha = r.json().get("sha")
    if sha != b.revision:
        raise FetchError(f"{b.repo} is at {sha}, not the pinned {b.revision}; re-pin after reviewing the change")


def _deepset_rows(client: httpx.Client, b: Benchmark) -> Iterator[tuple[str, str, str, str]]:
    _check_revision(client, b)
    for split in ("train", "test"):  # the whole set: nothing here is trained on
        offset = 0
        while True:
            r = client.get(
                ROWS_API,
                params={"dataset": b.repo, "config": "default", "split": split, "offset": offset, "length": 100},
            )
            r.raise_for_status()
            page = r.json().get("rows", [])
            for item in page:
                row = item["row"]
                if int(row["label"]) == 1:
                    yield row["text"], "block", "ext_injection", f"deepset {split} label 1 (injection)"
                else:
                    yield row["text"], "not_block", "ext_benign", f"deepset {split} label 0 (benign)"
            if len(page) < 100:
                break
            offset += 100


def _jbb_rows(client: httpx.Client, b: Benchmark) -> Iterator[tuple[str, str, str, str]]:
    for name, verdict, category in (
        ("harmful-behaviors.csv", "block", "ext_harmful"),
        ("benign-behaviors.csv", "not_block", "ext_benign"),
    ):
        r = client.get(f"{HF}/datasets/{b.repo}/resolve/{b.revision}/data/{name}", follow_redirects=True)
        r.raise_for_status()
        for row in csv.DictReader(io.StringIO(r.text)):
            yield row["Goal"], verdict, category, f"JBB {name.split('-')[0]} behavior ({row.get('Category', '')})"


_SOURCES = {"deepset": _deepset_rows, "jbb": _jbb_rows}


@dataclass(frozen=True)
class FetchReport:
    benchmark: str
    revision: str
    license: str
    url: str
    kept: int
    skipped_length: int
    skipped_duplicate: int
    fetched_at: str


def fetch(key: str, *, client: httpx.Client | None = None, cache: Path = CACHE_DIR) -> FetchReport:
    b = BENCHMARKS[key]
    cache.mkdir(parents=True, exist_ok=True)
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        kept: list[dict[str, object]] = []
        skipped = duplicates = 0
        seen: set[str] = set()
        for text, verdict, category, note in _SOURCES[key](http, b):
            if not MIN_INTENT_CHARS <= len(text.strip()) <= MAX_INTENT_CHARS:
                skipped += 1
                continue
            norm = " ".join(text.casefold().split())
            if norm in seen:
                duplicates += 1
                continue
            seen.add(norm)
            kept.append(_row(b, len(kept) + 1, text, verdict, category, note))
    finally:
        if own:
            http.close()
    path = _cache_path(b, cache)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8")
    report = FetchReport(
        benchmark=b.repo,
        revision=b.revision,
        license=b.license,
        url=b.url,
        kept=len(kept),
        skipped_length=skipped,
        skipped_duplicate=duplicates,
        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    path.with_suffix(".manifest.json").write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    return report


def load(keys: list[str], *, cache: Path = CACHE_DIR) -> list[Case]:
    cases: list[Case] = []
    for key in keys:
        if key not in BENCHMARKS:
            raise DatasetError(f"unknown benchmark {key!r}; known: {', '.join(BENCHMARKS)}")
        path = _cache_path(BENCHMARKS[key], cache)
        if not path.exists():
            raise DatasetError(f"{key} is not cached; run `python -m evals.orchestrator fetch-external {key}` first")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                cases.append(case_from_external(json.loads(line), f"{path.name}:{n}"))
    return validate(cases)

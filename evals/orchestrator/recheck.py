# ruff: noqa: E501 — a report template: its table rows read best as one f-string each.
"""The release re-check of 2026-10-06: the new guard policy and the changed workers, live.

    python -m evals.orchestrator recheck --campaign evals/orchestrator/runs/2026-10-06 \\
        --runs evals/orchestrator/runs/2026-10-06-recheck --out evals/orchestrator/reports/2026-10-06-recheck

Compares a fresh jev pass over the in-repo set under the policy that shipped
after the campaign (block line 0.40, low->moderate-only round-up, anchored
complexity tiers) with the campaign's stage 1, and records the worker
re-runs. Hand-written passages are kept between narrative markers, as in the
campaign report.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .campaign import TIER_MODEL, _jsonable, guard_block, list_price, misclassified, narratives, tier_misses, usd
from .dataset import TIERS
from .metrics import TIER_COLUMNS, ok_rows
from .runner import read_jsonl

_CUT_OFF = re.compile(r"stopped at an estimated \$([0-9.]+)")

ROWS = (
    ("verdict accuracy", "verdict_accuracy"),
    ("injection recall", "injection_recall"),
    ("block precision", "injection_precision"),
    ("false blocks, legitimate requests", "false_block_rate"),
    ("false blocks, borderline security asks", "false_block_security"),
    ("false blocks, Tagalog/Taglish", "false_block_filipino"),
    ("needs-detail recall", "needs_detail_recall"),
    ("non-requests allowed", "needs_detail_allowed"),
    ("tier accuracy (legit, allowed)", "tier_accuracy"),
    ("under-tier rate", "under_tier_rate"),
    ("over-tier rate", "over_tier_rate"),
)


def _cell(text: str) -> str:
    """Text safe inside one markdown table cell."""
    return " ".join(text.split()).replace("|", "\\|")


def _routes(rows: list[dict[str, Any]], legit_only: bool) -> dict[str, int]:
    pool = [
        r
        for r in ok_rows(rows)
        if r["meta"]["observed"]["verdict"] == "allow"
        and (not legit_only or r["meta"]["expected"]["verdict"] == "allow")
    ]
    tiers = Counter(r["meta"]["observed"]["tier"] for r in pool)
    return {t: tiers.get(t, 0) for t in TIERS}


CODE_CEILING_TOKENS = 9_000  # code_gen.MAX_TOKENS after the length fix
STREAM_BUDGET_S = 100.0  # claude_step.STREAM_BUDGET_SECONDS
_DEFERRED_LIST = re.compile(r"Deferred:.*$")
_DEFERRED = re.compile(r"\b(defer|deferred|later|not yet|out of scope|omitted|left out|future)\b", re.I)


def _saved_html(d: Path, stem: str) -> tuple[str | None, str]:
    """The run's saved HTML file name and text: the first `<stem>__*.html`
    in `d`, or (None, "") when the run saved none."""
    html_files = sorted(d.glob(f"{stem}__*.html"))
    if not html_files:
        return None, ""
    return html_files[0].name, html_files[0].read_text(encoding="utf-8")


def _code_run(rec: dict[str, Any], html_name: str | None, html: str, validator: list[str] | None) -> dict[str, Any]:
    """One measured code-worker run: its record's first call, its saved HTML
    and the validator result chosen for it."""
    call = rec["calls"][0] if rec["calls"] else {}
    summary = str((rec.get("output") or {}).get("summary") or "")
    deferred = _DEFERRED_LIST.search(summary)
    return {
        "label": rec.get("label") or rec["worker"],
        "worker": rec["worker"],
        "served_model": call.get("response_model"),
        "effort": call.get("effort"),
        "first_token_s": (call["first_token_ms"] / 1000) if call.get("first_token_ms") is not None else None,
        "wall_s": rec["latency_s"],
        "output_tokens": call.get("output_tokens"),
        "cost_usd": rec["cost_usd"],
        "unreported_estimated_usd": rec.get("unreported_estimated_usd", 0.0),
        "lines": len(html.splitlines()) if html else None,
        "bytes": len(html.encode()) if html else None,
        "validator": validator,
        "hit_token_ceiling": call.get("stop_reason") == "max_tokens",
        "hit_stream_budget": "did not finish within" in str(rec.get("error") or ""),
        "summary": summary,
        "deferred_listed": bool(_DEFERRED.search(summary)),
        "deferred_quote": deferred.group(0).strip() if deferred else None,
        "chars_per_line": round(len(html) / len(html.splitlines()), 1) if html else None,
        "html": html_name,
        "error": rec.get("error"),
    }


def _code_length(d: Path, *, revalidate: bool) -> list[dict[str, Any]]:
    """Per run of a complex code.gen re-measure: what the record and the saved
    HTML say. With `revalidate`, the validator as it stands now is run over the
    saved file; without, the result found at run time is kept (the directory's
    `validator_at_run.json`, else what the worker recorded) — so a later
    validator change cannot rewrite an earlier section's findings."""
    if not d.is_dir():
        return []
    from app.agents.workers.code_validator import validate_html

    frozen_path = d / "validator_at_run.json"
    frozen = json.loads(frozen_path.read_text(encoding="utf-8")) if frozen_path.exists() else {}
    out = []
    # code.gen drafts first, in run order, then the critic that polished one.
    for p in sorted(d.glob("*.json"), key=lambda p: (p.stem.startswith("code_critic"), p.stem)):
        if p.name == frozen_path.name:
            continue
        rec = json.loads(p.read_text(encoding="utf-8"))
        if "skipped" in rec:
            out.append({"label": rec.get("label"), "skipped": rec["skipped"]})
            continue
        html_name, html = _saved_html(d, p.stem)
        if revalidate:
            validator = validate_html(html) if html else None
        else:
            validator = frozen.get(html_name or "", _recorded_violations(rec))
        out.append(_code_run(rec, html_name, html, validator))
    return out


def _recorded_violations(rec: dict[str, Any]) -> list[str] | None:
    """The violations the worker reported at run time: code.gen's on its draft;
    code.critic's on the draft it was given (it does not re-validate its own)."""
    out = rec.get("output") or {}
    if "validator_violations" in out:
        return list(out["validator_violations"])
    if "critic_violations" in out:
        return [f"on the draft it polished: {v}" for v in out["critic_violations"]]
    return None


def _ceilings() -> dict[str, int]:
    """The code workers' output ceilings as the code stands now (the final check's)."""
    from app.agents.workers import code_critic, code_gen

    return {"code.gen": code_gen.MAX_TOKENS, "code.critic": code_critic.MAX_TOKENS}


def _block(name: str, keep: dict[str, str]) -> str:
    return f"<!-- BEGIN narrative:{name} -->\n{keep.get(name, '_(to be written)_\n')}<!-- END narrative:{name} -->"


def _spend(
    guard_rows: list[dict[str, Any]],
    workers: dict[str, dict[str, Any]],
    code_length: list[dict[str, Any]],
    final: list[dict[str, Any]],
) -> dict[str, Any]:
    """The re-check's spend: the guard at list price from its recorded tokens,
    the workers as the API reported them, and each cut-off stream's estimate."""
    guard_usd = sum(list_price(c) for r in guard_rows for c in r["meta"]["calls"])
    worker_usd = sum(w.get("cost_usd", 0.0) for w in workers.values())
    # A stream the budget guard cut off was billed but never reported back;
    # its error carries the guard's running estimate at the cut-off.
    cut_off = {
        name: float(m.group(1)) for name, w in workers.items() if (m := _CUT_OFF.search(str(w.get("error") or "")))
    }
    return {
        "guard_usd": guard_usd,
        "workers_reported_usd": worker_usd,
        "by_worker_reported_usd": {k: w.get("cost_usd", 0.0) for k, w in workers.items()},
        "cut_off_estimated_usd": cut_off,
        "measured_total_usd": guard_usd + worker_usd,
        "total_with_estimates_usd": guard_usd + worker_usd + sum(cut_off.values()),
        "code_length_usd": sum(r.get("cost_usd", 0.0) + r.get("unreported_estimated_usd", 0.0) for r in code_length),
        "final_usd": sum(r.get("cost_usd", 0.0) + r.get("unreported_estimated_usd", 0.0) for r in final),
    }


def _copy_files(src: Path, dst: Path) -> None:
    """Copy every file directly in `src` into `dst` (created if missing), in name order."""
    dst.mkdir(exist_ok=True)
    for p in sorted(src.iterdir()):
        if p.is_file():
            shutil.copyfile(p, dst / p.name)


def build(campaign_runs: Path, runs: Path, out: Path) -> dict[str, Any]:
    old_all = read_jsonl(campaign_runs / "s1-guard" / "results.jsonl")
    old = [r for r in old_all if r.get("rep", 0) == 0]
    new = read_jsonl(runs / "r1-guard" / "results.jsonl")
    errors = read_jsonl(runs / "r1-guard" / "errors.jsonl")
    g_old, g_pool, g_new = guard_block(old), guard_block(old_all), guard_block(new)
    workers = {}
    wdir = runs / "r2-workers"
    for p in sorted(wdir.glob("*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        workers[rec["worker"]] = rec
    code_length = _code_length(runs / "r3-code-length", revalidate=False)
    final = _code_length(runs / "r4-final", revalidate=True)
    analysis = {
        "campaign_rep0": g_old,
        "campaign_pooled": g_pool,
        "recheck": g_new,
        "routes": {
            "campaign_legit": _routes(old, True),
            "recheck_legit": _routes(new, True),
            "campaign_all_allowed": _routes(old, False),
            "recheck_all_allowed": _routes(new, False),
        },
        "misclassified": misclassified({"s1-guard": new}),
        "tier_misses": tier_misses(new),
        "errors": len(errors),
        "workers": workers,
        "code_length": code_length,
        "final": final,
        "final_ceilings": _ceilings() if final else {},
        "spend": _spend(new, workers, code_length, final),
    }

    out.mkdir(parents=True, exist_ok=True)
    (out / "r1-guard").mkdir(exist_ok=True)
    for name in ("results.jsonl", "errors.jsonl"):
        src = runs / "r1-guard" / name
        (out / "r1-guard" / name).write_text(src.read_text(encoding="utf-8") if src.exists() else "", encoding="utf-8")
    _copy_files(wdir, out / "r2-workers")
    (out / "metrics.json").write_text(
        json.dumps(_jsonable(analysis), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for sub in ("r3-code-length", "r4-final"):
        if (runs / sub).is_dir():
            _copy_files(runs / sub, out / sub)
    (out / "costs.json").write_text(json.dumps(analysis["spend"], indent=2) + "\n", encoding="utf-8")
    report = out / "REPORT.md"
    keep = narratives(report.read_text(encoding="utf-8")) if report.exists() else {}
    report.write_text(render(analysis, keep), encoding="utf-8")
    return analysis


def _worker_row(name: str, w: dict[str, Any]) -> str:
    """One worker re-run: the model that served it (none when its stream was
    cut off before the reply), and its error or the start of its summary."""
    call = w["calls"][0] if w["calls"] else {}
    model = call.get("response_model") or call.get("model") or "not recorded (stream cut off)"
    outcome = w["error"] or str((w["output"] or {}).get("summary", ""))[:200]
    return f"| {name} | {w.get('tier') or 'default'} | {model} | {w['latency_s']:.1f} s | {usd(w['cost_usd'])} | {outcome} |"


def _code_length_row(r: dict[str, Any]) -> str:
    """One row of the complex code.gen re-measure table."""
    viol = r["validator"]
    return (
        f"| {r['label']} | {r['served_model'] or 'not recorded'} | {r['effort'] or '—'} | "
        f"{r['first_token_s']:.2f} s | {r['wall_s']:.1f} s | {r['output_tokens']:,} | {usd(r['cost_usd'])} | "
        f"{r['lines']} | {r['bytes']:,} | {'; '.join(viol) if viol else 'none'} | "
        f"{'yes' if r['hit_token_ceiling'] else 'no'} | {'yes' if r['hit_stream_budget'] else 'no'} | "
        f"{'yes' if r['deferred_listed'] else 'no'} |"
    )


def _final_row(r: dict[str, Any], ceil: dict[str, int]) -> str:
    """One final-check table row; output tokens carry their share of the
    worker's ceiling when it has one."""
    viol = r["validator"]
    cap = ceil.get(r["worker"])
    share = f" / {cap:,} ({100 * r['output_tokens'] / cap:.0f}%)" if cap else ""
    return (
        f"| {r['label']} | {r['served_model'] or 'not recorded'} | {r['effort'] or '—'} | "
        f"{r['first_token_s']:.2f} s | {r['wall_s']:.1f} s | {r['output_tokens']:,}{share} | {usd(r['cost_usd'])} | "
        f"{r['lines']} | {r['bytes']:,} | {r['chars_per_line']} | {'; '.join(viol) if viol else 'pass (no violations)'} | "
        f"{'yes' if r['hit_token_ceiling'] else 'no'} | {'yes' if r['hit_stream_budget'] else 'no'} |"
    )


def _code_length_section(a: dict[str, Any], keep: dict[str, str]) -> list[str]:
    """The complex code.gen re-measure after the length fix: each run, then the summaries."""
    L = [
        "## Complex code.gen after the length fix",
        "",
        "cpx-001 (barbershop booking system) handed to the real workers as a complex-tier step, after code.gen and "
        f"code.critic moved to a 250–450-line target, a {CODE_CEILING_TOKENS:,}-token ceiling, low effort and a "
        f"{STREAM_BUDGET_S:.0f} s stream budget. Lines and bytes are measured on the saved HTML; validator violations "
        "are as found when this re-measure ran (the validator has changed since; see the final check).",
        "",
        "| run | served model | effort | first token | wall time | output tokens (incl. thinking) | cost | lines | bytes | validator violations | hit 9,000-token ceiling | hit 100 s budget | deferred features in summary |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in a["code_length"]:
        if "skipped" in r:
            L.append(f"| {r['label']} | not run — {r['skipped']} | | | | | | | | | | | |")
            continue
        L.append(_code_length_row(r))
    L += ["", "Summaries as returned:", ""]
    for r in a["code_length"]:
        if "skipped" not in r:
            L.append(f"- **{r['label']}** (`r3-code-length/{r['html']}`): {_cell(r['summary'])}")
    L += ["", _block("code_length_notes", keep), ""]
    return L


def _final_section(a: dict[str, Any], keep: dict[str, str]) -> list[str]:
    """The final check: each run against the ceilings as they stand now, then the summaries."""
    ceil = a["final_ceilings"]
    L = [
        "### Final check",
        "",
        "The same cpx-001 complex step after the follow-up fixes: ceilings raised to the measured "
        f"{ceil['code.gen']:,} (code.gen) and {ceil['code.critic']:,} (code.critic) tokens, readably formatted "
        "source asked for, a depth floor met by readable lines or source size, and deferred features named in "
        "the summary. Validator results are the current validator run on the saved HTML.",
        "",
        "| run | served model | effort | first token | wall time | output tokens (incl. thinking) / ceiling | cost | lines | bytes | chars per line | validator (new floor) | hit ceiling | hit 100 s budget |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in a["final"]:
        if "skipped" in r:
            L.append(f"| {r['label']} | not run — {r['skipped']} | | | | | | | | | | | |")
            continue
        L.append(_final_row(r, ceil))
    L += ["", "Summaries as returned, and the deferred list:", ""]
    for r in a["final"]:
        if "skipped" in r:
            continue
        quote = f"“{r['deferred_quote']}”" if r["deferred_quote"] else "no “Deferred: …” list"
        L.append(f"- **{r['label']}** (`r4-final/{r['html']}`): {_cell(r['summary'])} — {quote}")
    L += ["", _block("final_notes", keep), ""]
    return L


def _spend_section(a: dict[str, Any], keep: dict[str, str]) -> list[str]:
    """The spend table: measured, estimated, and each separately capped re-measure."""
    sp = a["spend"]
    return [
        "## Spend",
        "",
        _block("spend", keep),
        "",
        "| item | amount |",
        "|---|---|",
        f"| jev guard, 168 calls (from recorded tokens) | {usd(sp['guard_usd'])} |",
        f"| workers, as reported by the API | {usd(sp['workers_reported_usd'])} |",
        f"| **measured total** | **{usd(sp['measured_total_usd'])}** |",
        *[
            f"| cut-off stream, {k} (estimated, unreported) | ≈ {usd(v)} |"
            for k, v in sp["cut_off_estimated_usd"].items()
        ],
        f"| **total including estimates** | **{usd(sp['total_with_estimates_usd'])}** |",
        *(
            [f"| complex code.gen re-measure (separate $0.20 cap) | {usd(sp['code_length_usd'])} |"]
            if a.get("code_length")
            else []
        ),
        *([f"| final check (separate $0.25 cap) | {usd(sp['final_usd'])} |"] if a.get("final") else []),
        "",
    ]


def render(a: dict[str, Any], keep: dict[str, str]) -> str:
    old, pool, new = a["campaign_rep0"], a["campaign_pooled"], a["recheck"]
    L = [
        "# Release re-check — 2026-10-06",
        "",
        "A fresh live jev pass over the 168 in-repo intents under the policy that shipped after the campaign "
        '(injection block line 0.40, an unsure "low" rounded up to "moderate" but never anything onto "complex", '
        "complexity tiers anchored with concrete examples), set beside the campaign's stage 1; and the workers whose "
        "prompts or schemas changed, re-run on the campaign's inputs. Figures carry 95% Wilson intervals and n. "
        "The campaign report is `../2026-10-06/REPORT.md`.",
        "",
        "## Summary",
        "",
        _block("summary", keep),
        "",
        "## Guard: before and after",
        "",
        "| metric | campaign stage 1, rep 0 (n=168) | campaign stage 1, 3 reps (n=504) | re-check, new policy (n=168) |",
        "|---|---|---|---|",
    ]
    for label, key in ROWS:
        L.append(f"| {label} | {old[key].fmt()} | {pool[key].fmt()} | {new[key].fmt()} |")
    L += ["", f"Failed attempts in the re-check: {a['errors']}.", "", "### Tier confusion (legit cases)", ""]
    for title, g in (("campaign stage 1, rep 0", old), ("re-check", new)):
        L += [
            f"{title}:",
            "",
            "| expected \\ routed | " + " | ".join(TIER_COLUMNS) + " |",
            "|---" * (len(TIER_COLUMNS) + 1) + "|",
        ]
        for exp in TIERS:
            L.append(f"| {exp} | " + " | ".join(str(g["tier_confusion"][exp][c]) for c in TIER_COLUMNS) + " |")
        L.append("")
    r = a["routes"]
    L += [
        "### Where allowed requests route",
        "",
        "| | " + " | ".join(f"{t} → {TIER_MODEL[t]}" for t in TIERS) + " |",
        "|---|---|---|---|",
    ]
    for label, key in (
        ("campaign, legit requests", "campaign_legit"),
        ("re-check, legit requests", "recheck_legit"),
        ("campaign, every allowed request", "campaign_all_allowed"),
        ("re-check, every allowed request", "recheck_all_allowed"),
    ):
        L.append(f"| {label} | " + " | ".join(str(r[key][t]) for t in TIERS) + " |")
    L += [
        "",
        "### Every wrong verdict in the re-check",
        "",
        "| case | expected → got | reasons | scores | intent |",
        "|---|---|---|---|---|",
    ]
    for m in a["misclassified"]:
        scores = ", ".join(f"{k} {v}" for k, v in m["scores"].items())
        L.append(
            f"| {m['id']} | {m['expected']} → {m['got']} | {', '.join(m['reasons']) or '—'} | {scores} | {_cell(m['intent'][:100])} |"
        )
    L += [
        "",
        "### Tier misses in the re-check",
        "",
        "| case | expected | raw choice | confidence | routed |",
        "|---|---|---|---|---|",
    ]
    for t in a["tier_misses"]:
        L.append(f"| {t['id']} | {t['expected']} | {t['raw']} | {t['confidence']} | {t['routed']} |")
    L += [
        "",
        _block("guard_notes", keep),
        "",
        "## Workers",
        "",
        "| worker | tier asked | model | latency | reported cost | outcome |",
        "|---|---|---|---|---|---|",
    ]
    for name, w in a["workers"].items():
        L.append(_worker_row(name, w))
    L += ["", _block("worker_notes", keep), ""]
    if a.get("code_length"):
        L += _code_length_section(a, keep)
    if a.get("final"):
        L += _final_section(a, keep)
    L += _spend_section(a, keep)
    return "\n".join(L) + "\n"

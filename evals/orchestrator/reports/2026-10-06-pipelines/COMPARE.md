# Planner composition: before and after

- before: `evals/orchestrator/reports/2026-10-06/s4-full`
- after: `evals/orchestrator/reports/2026-10-06-pipelines/after`
- cases planned by both: 47

## Before / after

| measure | before | after |
|---|---|---|
| plans | 47 | 47 |
| mean distinct specialists | 1.77 | 3.40 |
| single-step plans | 57.4% (27/47; 95% CI 43.3–70.5) | 17.0% (8/47; 95% CI 8.9–30.1) |
| mean recipe coverage | 65.6% | 95.1% |
| full recipe coverage | 40.4% (19/47; 95% CI 27.6–54.7) | 87.2% (41/47; 95% CI 74.8–94.0) |
| irrelevant-step rate | 1.2% (1/84; 95% CI 0.2–6.4) | 1.2% (2/160; 95% CI 0.3–4.4) |
| labelled agents in handoff order | 93.6% (44/47; 95% CI 82.8–97.8) | 89.4% (42/47; 95% CI 77.4–95.4) |
| plans with one code builder at most | 100.0% (47/47; 95% CI 92.4–100.0) | 100.0% (47/47; 95% CI 92.4–100.0) |
| coverage: contract | 100.0% | 100.0% |
| coverage: design | 100.0% | 100.0% |
| coverage: marketing | 70.0% | 93.3% |
| coverage: research | 70.0% | 80.0% |
| coverage: seo | 100.0% | 100.0% |
| coverage: short_copy | 75.0% | 100.0% |
| coverage: translation | 100.0% | 100.0% |
| coverage: webapp | 34.0% | 93.8% |
| coverage: website | 58.1% | 97.8% |
| distinct specialists: legit_complex | 3.27 | 5.18 |
| distinct specialists: legit_low | 1.00 | 1.86 |
| distinct specialists: legit_moderate | 1.53 | 3.93 |
| distinct specialists: legit_security | 1.43 | 2.57 |

## How these were made

- **Before:** the 2026-10-06 live campaign's stage 4 (`s4-full`): the earlier planner instructions ("prefer code.gen, often as a single-step plan"), no role cards, all 12 seeded agents offered. Rescored here from its stored raw plans against today's pipeline labels — no new calls.
- **After:** one live run on this branch at `b10f746` (guard → improve → re-check → plan; no workers), over the 47 allow cases that carry a pipeline label, at `--max-usd 1.50`. Measured spend **$0.7291** (before, the same 47 cases cost $0.4728); the plan stage cost $0.5389 (mean 404 output tokens, max 919, mean latency 5.3 s; before: 150 tokens). All 47 scored, none failed; plan validity 100%.
- The after run predates two later instruction edits (pick one code builder, never both; code.critic only after code.gen), so it measures the recipes and role cards, not those two sentences — both are also enforced in code (`orchestrator_svc._compose`).
- The handoff-order dip comes from five plans that put translate.42 before design and the build (translate the copy, then build in both languages) or copy before design — orderings the labels did not anticipate rather than broken handoffs. None of the 47 after-plans would lose a step to the composition rules.

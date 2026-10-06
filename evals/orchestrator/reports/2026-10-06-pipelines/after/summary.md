# Orchestrator guard + planner eval

- Pipeline: `app-live`; scored rows: 47; truncated (not scored): 0; failed attempts (errors.jsonl, not scored): 0 (none)
- Rows answered by a server-side fallback model: 0
- Measured spend: $0.7291 ($0.7291 on scored rows, $0.0000 on failed attempts)

## Guard

| metric | value |
|---|---|
| verdict accuracy | 100.0% (47/47; 95% CI 92.4–100.0) |
| injection recall | n/a (0 cases) |
| injection precision (blocked citing injection) | n/a (0 cases) |
| block recall (all should-block) | n/a (0 cases) |
| block precision | n/a (0 cases) |
| false-block rate (legitimate) | 0.0% (0/47; 95% CI 0.0–7.6) |
| false-block rate (borderline security slice) | 0.0% (0/7; 95% CI 0.0–35.4) |
| needs-detail recall | n/a (0 cases) |
| false needs-detail rate (legitimate) | 0.0% (0/47; 95% CI 0.0–7.6) |
| harmful recall (external benchmark) | n/a (0 cases) |
| tier accuracy (allowed legit cases) | 87.2% (41/47; 95% CI 74.8–94.0) |
| majority-class baseline (always `allow`) | 100.0% (47/47; 95% CI 92.4–100.0) |

## Tier confusion (expected rows x routed columns, legit cases)

| expected \ routed | low | moderate | complex | not_allowed |
|---|---|---|---|---|
| low | 14 | 2 | 0 | 0 |
| moderate | 1 | 18 | 0 | 0 |
| complex | 0 | 3 | 9 | 0 |

- under tier rate: 8.5% (4/47; 95% CI 3.4–19.9)
- over tier rate: 4.3% (2/47; 95% CI 1.2–14.2)

## Plan validity (raw planner output, before the clamp)

| metric | value |
|---|---|
| plan valid | 100.0% (47/47; 95% CI 92.4–100.0) |
| plan allowlisted | 100.0% (47/47; 95% CI 92.4–100.0) |
| plan schema | 100.0% (47/47; 95% CI 92.4–100.0) |
| plan tiers | 100.0% (47/47; 95% CI 92.4–100.0) |
| plan refused | 0.0% (0/47; 95% CI 0.0–7.6) |

## Pipeline composition (raw planner output)

- Plans: 47; distinct specialists per plan: mean 3.40, distribution 1: 8, 2: 11, 3: 7, 4: 6, 5: 5, 6: 10
- Single-step plans: 17.0% (8/47; 95% CI 8.9–30.1)
- Plans that buy both code builders: 0.0% (0/47; 95% CI 0.0–7.6)
- Labelled cases: 47; mean recipe coverage 95.1%; full coverage 87.2% (41/47; 95% CI 74.8–94.0)
- Irrelevant-step rate (labelled cases): 1.2% (2/160; 95% CI 0.3–4.4)
- Labelled agents in handoff order: 89.4% (42/47; 95% CI 77.4–95.4)

| recipe | cases | mean coverage | full coverage | irrelevant steps |
|---|---|---|---|---|
| contract | 4 | 100.0% | 100.0% (4/4; 95% CI 51.0–100.0) | 0.0% (0/9; 95% CI 0.0–29.9) |
| design | 2 | 100.0% | 100.0% (2/2; 95% CI 34.2–100.0) | 0.0% (0/6; 95% CI 0.0–39.0) |
| marketing | 5 | 93.3% | 80.0% (4/5; 95% CI 37.6–96.4) | 0.0% (0/18; 95% CI 0.0–17.6) |
| research | 5 | 80.0% | 80.0% (4/5; 95% CI 37.6–96.4) | 16.7% (2/12; 95% CI 4.7–44.8) |
| seo | 1 | 100.0% | 100.0% (1/1; 95% CI 20.7–100.0) | 0.0% (0/1; 95% CI 0.0–79.3) |
| short_copy | 6 | 100.0% | 100.0% (6/6; 95% CI 61.0–100.0) | 0.0% (0/10; 95% CI 0.0–27.8) |
| translation | 3 | 100.0% | 100.0% (3/3; 95% CI 43.9–100.0) | 0.0% (0/3; 95% CI 0.0–56.1) |
| webapp | 12 | 93.8% | 75.0% (9/12; 95% CI 46.8–91.1) | 0.0% (0/48; 95% CI 0.0–7.4) |
| website | 9 | 97.8% | 88.9% (8/9; 95% CI 56.5–98.0) | 0.0% (0/53; 95% CI 0.0–6.8) |

| category | plans | mean distinct specialists |
|---|---|---|
| legit_complex | 11 | 5.18 |
| legit_low | 14 | 1.86 |
| legit_moderate | 15 | 3.93 |
| legit_security | 7 | 2.57 |

Irrelevant steps by agent: code.gen 1, code.critic 1

## Verdict accuracy by category

| slice | accuracy |
|---|---|
| legit_complex | 100.0% (11/11; 95% CI 74.1–100.0) |
| legit_low | 100.0% (14/14; 95% CI 78.5–100.0) |
| legit_moderate | 100.0% (15/15; 95% CI 79.6–100.0) |
| legit_security | 100.0% (7/7; 95% CI 64.6–100.0) |

## Verdict accuracy by language

| slice | accuracy |
|---|---|
| en | 100.0% (37/37; 95% CI 90.6–100.0) |
| es | 100.0% (2/2; 95% CI 34.2–100.0) |
| taglish | 100.0% (3/3; 95% CI 43.9–100.0) |
| tl | 100.0% (4/4; 95% CI 51.0–100.0) |
| zh | 100.0% (1/1; 95% CI 20.7–100.0) |

## Verdict accuracy by split

| slice | accuracy |
|---|---|
| test | 100.0% (19/19; 95% CI 83.2–100.0) |
| train | 100.0% (28/28; 95% CI 87.9–100.0) |

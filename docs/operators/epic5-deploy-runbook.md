# Epic 5 deploy runbook: merge, deploy, switch escrow, re-run the evidence (testnet)

One document, in order, for the lead engineer taking Epic 5 from open PRs to
submitted evidence across the four repositories. Every step has its exact
command or dashboard action, what you should see, how to verify it, and how to
undo it. The pieces already exist in other documents; this runbook puts them
in order and links each one.

**Testnet only.** Nothing here touches mainnet. `render.yaml` still says
`STELLAR_NETWORK: mainnet` and `autoDeploy: true`: both are stale. The Render
dashboard's environment overrides the file, and the backend does **not**
auto-deploy, because Render's GitHub App is not installed on the `Bl0cksmiths`
organization. Every backend deploy below is a **Manual Deploy**.

**Never invent a value.** Where a value is a secret, this runbook describes it
and never gives it. Where no source settles something, it says **confirm**;
[the confirm list](#confirm-list) at the end collects every one of them.

## Progress (2026-09-30)

Where each step stands. The live checks were read on 2026-09-30 (Asia/Manila;
2026-09-29 17:35 UTC), with GETs only.

| Step | State | What is done | What remains |
|---|---|---|---|
| 1. Preconditions | open | 1.1: the lead confirmed that the Chapter Lead approved the escrow v2 testnet deploy on 2026-09-30 (C1). | The rest of step 1 is not tracked here. |
| 2. Merge order | **done** 2026-09-29 | All with merge commits: contracts #5 (`4e04e67`, 12:03 UTC), backend #94 (`44c3411`, 12:01 UTC), frontend #97 (`d90b37e`, 12:00 UTC). The evidence index followed in frontend #98 (`be3e826`, 16:27 UTC) and #99 (`b14d4cb`, 17:01 UTC). | nothing |
| 3. Render deploy | **done** 2026-09-29 | A Manual Deploy of backend `main` at `44c3411`. The 3.2 checks, live: see below. | nothing |
| 4. Escrow v2 | **done** 2026-09-30 | `make deploy-escrow-v2` deployed PaymentEscrow v2 as `CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4`. The script read back `version=2`, settler `GDB4N25…CDHP` (`$SETTLER`) and admin `GA7AI5…5OQV`. The address book went to contracts `main` as a PR, #6 (merged). Render's `STELLAR_PAYMENT_ESCROW` now names v2, and `/readiness` reports `escrow.version` `2`. | nothing. v1 (`CBJPTMAP…25PI`) stays valid and stays in the evidence as history. |
| 5. The v2 pin | **done** 2026-09-30 | Frontend #101 (merged) sets the `testnet` pin to v2. | nothing |
| 6. Refunds on | **done** 2026-09-30 | `DISPUTE_REFUNDS_ENABLED` and `REFUND_RECONCILE_ENABLED` are both `true`. `/readiness` reports `disputes.store` `postgres`, `disputes.reconcile.enabled` `true` and the sweep running. | nothing |
| 7. Operators | in progress | Two outside wallets have registered agents: OP-1 one trial agent, OP-2 five. The live adoption report counts 6 external agents from 2 wallets, 0 settled. | Neither has a working bound agent. OP-1's one agent is bound to a parked web page (F-033, F-034); none of OP-2's five is bound. Step 4 no longer blocks them: F-019 is fixed. The checkpoint record (7.5) is still open. |
| 8. Lifecycle | **partly done** 2026-09-30 | Disclosed team runs on v2, with team wallets as buyers and the team's own agents: 3 settled workflows (settles `f0674419…`, `19f3420d…`, `785428bf…`), each sealed and rated, every harness check passing. The faulty agent `faulty_test_v2` (team key owner `GB4K6…YKAYKK`) took three failure ratings to lower bound 5443 and is excluded live. Dispute `dsp_15acee279ac02852a5877ac1696ec4b5` is open, on run 3's settled step. The evidence sheet re-verified 19 of 19 hashes `SUCCESS`. The evidence is committed in [`docs/evidence/5.01/v2-team-runs/`](../evidence/5.01/v2-team-runs/): the sheet, `evidence.json` and `description.txt` under `evidence/`, and each run's `lifecycle.md` and `lifecycle.jsonl` under `h1`, `h2b`, `h3` (settled runs) and `f1` to `f3` (the faulty agent's). No `state.json` is committed. | The uphold, which waits for the adjudicator key, and then its refund and dispute rating. AC4 (the restart between seal and dispute) was skipped: Render cannot be restarted from the runner. |
| 9. Demo | ready to record | Steps 4 and 6 are done, and the below-floor agent that 9.1 builds exists (`faulty_test_v2`, 3 ratings, lower bound 5443). | Choose the operator on camera, an outside one or a disclosed team one (`--allow-team-operator`), then 9.2 onward. |
| 10. Evidence re-run | not started | The index was refreshed to the post-deploy state in frontend #98 and #99. | The full re-run after steps 7 to 9, with `--withhold-external` (the default) until each outside operator's consent is recorded. |
| 11. Closing | not started | — | BLO-134 and BLO-136 are due Fri 2026-10-02; the check-in (BLO-138) is today. |

**Step 3 checks, live.** Every 3.2 check passes:

- `GET $SITE/api/health` answered `{"status":"ok"}`.
- `GET $BE_HOST/readiness` answered `"status": "ready"` with both blocks:
  `disputes.store` is `"postgres"`, `disputes.reconcile.enabled` is `false`,
  `escrow.contract` is `$ESCROW_V1` (`CBJPTMAP…25PI`) and `escrow.version` is
  `1`. `ratings.signer` and `ratings.scorer` are `$SETTLER`, and
  `ratings.writer` is `scorer`.
- `GET $SITE/api/ecosystem/adoption` answered `200`: 6 external agents from 2
  operator wallets, 0 settled external workflows, `window_days` 7, not
  degraded. Only one of the six is bound.
- `GET $SITE/api/agents/any_id/readiness` answered `200`, with `registered`
  at `todo`.
- `GET $SITE/api/stellar/network` names `testnet`, with `$ESCROW_V1` as
  `contracts.payment_escrow`.

**Confirm list, re-checked.** The deploys closed C10: the frontend's
after-deploy checklist merged with #97. The escrow v2 deploy on 2026-09-30
closed C1, C4 and C6 (see [the confirm list](#confirm-list)). C2, C3, C7, C8,
C11 and C12 stay open.

**Team hygiene.** The team agent `3D_Artbot`, owned by the admin key
`GA7AI5…5OQV`, is bound to `https://arbot.com`. That endpoint does not answer:
its readiness shows `reachable` failed ("did not answer within 5 s"), and a
plain `GET` timed out after 15 s. It is still routable, at 5677 bps against the
5500 floor. So the planner can put it in a buyer's plan, and each run that
reaches it fails that step (F-034). The admin key holder should unbind it, or
rebind it to an agent that answers, before any wallet-authorized run.

**Team hygiene, 2026-09-30.** For the step 8 runs, the admin key rebound
`calculatorai` and `keyboardai` from their placeholder hosts to a reference
agent, and unbound both afterwards. Two admin-owned agents are **still bound
to a placeholder or dead URL and still routable**: `algorex` (`https://testing.com`)
and `3D_Artbot` (`https://arbot.com`, above). Neither can deliver a step, so
each run the planner routes to one fails that step (F-002, F-034). Unbind both,
or rebind them to an agent that answers, before the demo or any operator
session.

## The four repositories

| Short name | GitHub | Local checkout (Dan's machine) |
|---|---|---|
| contracts | `Bl0cksmiths/Orizon-Agents-Smart-Contract-Stellar` | `/home/dan/Contracts-2026/orizon-agents-Smart-Contract-Stellar` |
| backend | `Bl0cksmiths/Orizon-Agents-BE-Stellar` | `/home/dan/Websites-Services-2026/orizon-agents-BE-Stellar` |
| frontend | `Bl0cksmiths/Orizon-Agents-FE-Stellar` | `/home/dan/Websites-2026/orizon-agents-FE-Stellar` |
| example agent | `Bl0cksmiths/Orizon-Agents-Example-Agent-Stellar` | (fork per operator; see the onboarding runbook) |

The commands below use these shell variables. Set them once per terminal:

```sh
export CONTRACTS=/home/dan/Contracts-2026/orizon-agents-Smart-Contract-Stellar
export BE=/home/dan/Websites-Services-2026/orizon-agents-BE-Stellar
export FE=/home/dan/Websites-2026/orizon-agents-FE-Stellar
export BE_HOST=https://orizon-agents-be-stellar.onrender.com   # root-level /readiness lives here
export SITE=https://orizons.xyz                                # the frontend, which proxies /api
export SETTLER=GDB4N25UYM3YNTTAWX7LSGI2P7OR62QZQXRNQWAGF5TFVENDKCTTCDHP
export ESCROW_V1=CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI
export ESCROW_V2=CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4   # live since 2026-09-30 (step 4)
```

`SETTLER` is the public half of the production signing key (the one Render's
`STELLAR_SIGNING_KEY` holds), per the frontend's `docs/escrow-v2-switch.md`.
`ESCROW_V1` is `payment_escrow` in the contracts repo's `addresses.json`
(testnet), and `ESCROW_V2` is its `payment_escrow_v2`. `orizons.xyz` does not
proxy `/readiness`, so it is always read from `$BE_HOST`.

If a local checkout is hosting another lane's branch, run the steps from a
separate `git worktree` of `origin/main` rather than switching branches in it.

## Source documents

| Step | Source |
|---|---|
| Escrow v2 switch | frontend `docs/escrow-v2-switch.md` (on `feat/5.06-integration` and `main`); contracts `Makefile` `deploy-escrow-v2`, `scripts/deploy_escrow_v2.sh`; backend ADR 0010 |
| Backend environment | backend `.env.example`, README "Deploy — Render" |
| Operators (5.02) | [onboarding-session-runbook.md](onboarding-session-runbook.md), [friction-log.md](friction-log.md), [readiness.md](readiness.md), ADR 0012 |
| Lifecycle (5.01) | [lifecycle-harness.md](lifecycle-harness.md) |
| Demo (5.04) | [demo-recording.md](demo-recording.md); frontend `content/demo/script.md`, `shot-list.md` |
| Evidence index (5.05) | [sow-metrics.md](sow-metrics.md); frontend `content/evidence/index.json`, `docs/evidence-index-after-deploy.md` (on `main` since frontend #97) |
| Gates | Linear: "Sprint Risk Register & Decision Log" (R5, R9), BLO-36 |
| Task read tokens (no step; `TASK_AUTH_REQUIRED` stays `false`) | [task-token-auth.md](task-token-auth.md), the 2026-09-30 audit: what breaks if it is turned on today, the safe path and the staging |

---

## 1. Preconditions and gates

Nothing in steps 2–12 starts until every box here is ticked.

1. **The Chapter Lead has approved the escrow v2 testnet deploy.**
   - Why: risk **R9** ("Contract redeploy would invalidate published evidence")
     says *no redeploy without the Chapter Lead's agreement*, and ADR 0002
     rejected a new escrow entrypoint in Week 1 for the same reason:
     re-publishing contract ids that SOW §6.1 lists as submitted evidence is
     barred without it. Escrow v2 is a **new** contract id beside v1, and v1's
     id stays valid and stays in the evidence as history, but the id the live
     service settles through changes.
   - Record it: as a dated decision entry in the Linear document "Sprint Risk
     Register & Decision Log" under R9, quoting the Chapter Lead's words and
     the date, with a link to where it was given (the check-in issue, BLO-138,
     if it is given at the Wed 2026-09-30 check-in). *(C1, closed 2026-09-30:
     the lead confirmed the Chapter Lead approved on 2026-09-30.)*
   - Verify: the risk register shows the entry, and R9's status names it.
   - If it is refused: stop. Do steps 2, 3 and 7 only; the lifecycle (8), the
     demo's settlement scenes (9) and the refund metric cannot be produced on
     v1 (D-039), and the evidence index (10) states that plainly.
2. **The Week-2 operator checkpoint status is known and recorded truthfully.**
   - The gate (BLO-36 and risk **R5**): by the end of Week 2 (Fri
     2026-09-18), two named, committed external operators, **or** an
     escalation to the Chapter Lead raised that same day, with the chapter
     fallback triggered.
   - Status when this runbook was written (2026-09-29): **not recorded.**
     BLO-36 has no comments and no checkpoint entry; R5 in the risk register
     is still `open` and was last updated 2026-09-21. **Confirm** what actually
     happened at the checkpoint before step 7, and record it (step 7.5).
   - Do not backfill a checkpoint that did not happen. If no escalation was
     raised on 2026-09-18, the record says so and says when it was raised.
3. **Who does what.** From D-003 in the risk register and the Linear
   assignees:

   | Person | Owns in this runbook |
   |---|---|
   | **Dan** (Danielle Bagaforo Meer, lead) | steps 1–10: the merges, every deploy and dashboard change, the escrow v2 deploy, the operator sessions (5.02), the lifecycle runs (5.01), the demo recording (5.04), the metrics run (5.05) |
   | **Rie** (Rieselle Saure, PM + QA) | the 6.04 re-verification in step 10 (BLO-43), and step 11: the Week-4 bundle (BLO-134), the X post (BLO-136), the check-in record (BLO-138) |
   | **The Chapter Lead** | the R9 approval in 1.1, and the §6.2 sign-off in step 10 |

   **Confirm** whether Rie also facilitates or observes the operator
   sessions: no source assigns them to her.
4. **Access.** Before starting, check you have each of these. None of the
   secrets is written down anywhere in the repositories.
   - Render dashboard access to the backend service (environment and Manual
     Deploy).
   - Vercel access to the frontend project (deployments and promote).
   - The stellar-cli identity `admin` on the machine that deploys the
     contract. `stellar keys address admin` should print the address book's
     `admin`, `GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV`.
     The deploy script defaults the escrow admin to this identity's address.
     *(C4, closed 2026-09-30: the v2 deploy read back admin `GA7AI5…5OQV`,
     so the identity is the address book's `admin`.)*
   - Merge rights on all four repositories, and `gh` authenticated.
   - The deployment's operator `API_KEY` (a secret, from the Render dashboard)
     for the lifecycle harness's `--adjudicator-key-env`.
   - Two funded testnet buyer wallets whose seeds you hold (step 8), and a
     third team wallet for the faulty demo agent (step 9).
5. **The live state before you start.** Read it and keep the output; it is
   the baseline every rollback returns to.

   ```sh
   curl -s "$BE_HOST/readiness" | jq '{status, escrow, ratings, disputes}'
   curl -s "$SITE/api/stellar/network" | jq '{network, contracts}'
   gh pr list -R Bl0cksmiths/Orizon-Agents-BE-Stellar --state merged --limit 3
   ```

   Also note, from the Render dashboard, the commit the backend is currently
   running, and from Vercel, the current production deployment. Those two are
   what step 12 rolls back to.

---

## 2. Merge order

**Merge commits only. Never squash, never rebase-merge.** Each repo has one
PR for Epic 5, and each carries its stories as merged branches. A merge commit
keeps every story's commits in `main` with their original ids, which the
evidence index and the litepaper cite; a squash would rewrite them into one.

Open PRs on 2026-09-29 (`gh pr list`, all MERGEABLE):

| Repo | PR | Branch | What it is | Stacked on |
|---|---|---|---|---|
| contracts | #5 | `chore/mit-license` | the MIT licence | — |
| backend | #94 | `fix/5-audit-integration` | 5.03 friction coverage, 5.04 demo tools, 5.05 `sow_metrics`, the MIT licence, and the audit fixes (`window_days`, the team register, this runbook, ADR amendments) | — |
| frontend | #97 | `fix/5-audit-fe-integration` | 5.03 guide, 5.04 demo script and `/demo`, 5.05 `/evidence`, 5.06 litepaper, the MIT licence, and the audit fixes (the escrow-aware custody copy, the Ecosystem window) | — |

Backend #90, #91, #92 and #93 and frontend #91 to #96 are closed as
consolidated: every one of their commits is in #94 or #97.

Already merged and deployed: backend #88 (5.01) and #89 (5.02); frontend #89
(5.01, the v2 console) and #90 (5.02). The example agent has no open PR and
GitHub already detects its MIT licence.

Most of backend #94 is scripts, docs and tests. In `app/` it adds
`window_days` to `GET /api/ecosystem/adoption`, one entry to
`app/data/team_wallets.json`, and a reworded refund refusal message. The rest
of the behaviour step 3 deploys is 5.01 + 5.02 (#88, #89), which are already
on `main` but, per the demo script's live check on 2026-09-29, not yet on
Render.

### The order

For each PR: wait for its checks to go green, then merge it.

```sh
# contracts
gh pr merge 5  -R Bl0cksmiths/Orizon-Agents-Smart-Contract-Stellar --merge

# backend
gh pr merge 94 -R Bl0cksmiths/Orizon-Agents-BE-Stellar --merge

# frontend: first, because production's custody copy is wrong until it ships
gh pr merge 97 -R Bl0cksmiths/Orizon-Agents-FE-Stellar --merge
```

**The guide and the Register labels ship together.** They agree only
together: the guide quotes "price per step (XLM)", and #97 is also where the
Register label (and receipts, the dispute dialog, the trace and the
dashboard) stops hard-coding "USDC" and follows the network asset, XLM on
testnet (friction F-022). One PR means one Vercel deployment carries both.

After each merge:

1. **Expected:** GitHub shows the PR as merged, not squashed: `gh pr view
   <n> --json mergeCommit,commits` lists every commit, and `main` holds them
   with their original ids.
2. **Verify:** `git -C "$BE" fetch origin && git -C "$BE" log --oneline --merges
   -1 origin/main` (and the same for `$FE`) shows the PR's merge commit.
3. **Rollback:** a merged PR is undone with a revert PR of its merge commit
   (`git revert -m 1 <merge sha>` on a new branch, PR to `main`). Never force-push
   `main`.

The frontend's Vercel deploy of these merges is fine to let happen now: the
pages it adds (`/guide/list-your-agent`, `/demo`, `/evidence`, `/litepaper`)
are static and do not depend on escrow v2. The escrow pin stays `null` until
step 5, so nothing is paused.

**The frontend already speaks v2.** `docs/escrow-v2-switch.md` assumes the
frontend merges last, after the contract and backend switch. That no longer
holds: frontend #89 (the v2 console) is merged and deployed, so today the
plan card tells buyers that signing Authorize moves the cap into escrow while
production still settles through v1, which cannot do that (D-039). This
runbook does not create that gap; it closes it. Keep steps 3 to 5 in one
sitting so it closes as soon as possible. *(Closed 2026-09-30: steps 4 and 5
are done, and production settles through v2.)*

---

## 3. Render: deploy the backend `main`

Everything in this step happens in the Render dashboard, on the backend
service. The dashboard's environment is the one in force: it overrides
`render.yaml`, which is stale (mainnet values and `autoDeploy: true`). Change
nothing in `render.yaml` as part of this runbook.

### 3.1 Check the environment

Open the service → **Environment**. Check, without copying any value out of
the dashboard:

| Variable | Must be | Why |
|---|---|---|
| `STELLAR_NETWORK` | `testnet` | testnet only for the sprint |
| `STELLAR_RPC_URL`, `STELLAR_NETWORK_PASSPHRASE` | the testnet values in `.env.example` | |
| `STELLAR_PAYMENT_ESCROW` | `$ESCROW_V1` for now | step 4 changes it |
| `DATABASE_URL` | **set** (a secret: the Postgres connection string, Neon) | without it settlements, disputes and bindings live in memory and are lost on the next deploy or sleep |
| `API_KEY` | **set** (a secret: the operator key; 8+ printable ASCII characters per the demo script) | mandatory as soon as `DISPUTE_REFUNDS_ENABLED=true`; the process refuses to boot without it |
| `STELLAR_SIGNING_KEY` | **set** (a secret: the `S…` whose public half is `$SETTLER`) | settles, rates, seals and pays credits |
| `REGISTRY_BOOT_SYNC_TIMEOUT_SECONDS` | `5` (add it if missing) | how long boot waits for the first registry sync before pre-warming reputation; 0–60, and the config refuses anything else |
| `DISPUTE_REFUNDS_ENABLED` | `false` | stays off until step 6 |
| `REFUND_RECONCILE_ENABLED` | `false` | stays off until step 6 |
| `MAX_REFUND_USDC` | note its value | step 6 funds the signer against it; no public route reports it (default `1.0`) |
| `TASK_AUTH_REQUIRED` | `false` | leave it off: turning it on today hides receipts and breaks shared links ([task-token-auth.md](task-token-auth.md)) |

`5` is also the code default (`app/config.py`); setting it explicitly records
the choice in the dashboard, which is the environment of record.

### 3.2 Manual Deploy

1. **Action:** service → **Manual Deploy** → **Deploy latest commit** (branch
   `main`).
2. **Expected:** the deploy log ends live; the Events tab names the merge
   commit of the last backend PR from step 2.
3. **Verify** (the first request can take a minute on the free tier):

   ```sh
   curl -s "$SITE/api/health"
   curl -s "$BE_HOST/readiness" | jq '{status, escrow, disputes, ratings}'
   curl -s -o /dev/null -w '%{http_code}\n' "$SITE/api/ecosystem/adoption"
   curl -s -o /dev/null -w '%{http_code}\n' "$SITE/api/agents/any_id/readiness"
   ```

   - `/readiness` answers `"status": "ready"` and carries **both** a
     `disputes` block (`store`, `reconcile`) and an `escrow` block
     (`contract`, `version`). A response without them is an older build.
   - `disputes.store` is `"postgres"`. `"memory"` means `DATABASE_URL` is not
     in force: stop and fix it before anything else.
   - `disputes.reconcile.enabled` is `false`.
   - `escrow.contract` is `$ESCROW_V1`. `escrow.version` is `1`, or `null`
     on the first probe after boot (the probe starts a background read;
     ask again after a few seconds).
   - `ratings.signer` is `$SETTLER` and `ratings.writer` is `scorer`
     (`ratings.signer` can be `null` until the rating writer's chain read
     lands; ask again).
   - `/api/ecosystem/adoption` answers `200`.
   - `/api/agents/{id}/readiness` answers `200` (an unregistered id still gets
     200 with `registered: todo`).
4. **Rollback:** service → **Manual Deploy** → **Deploy a specific commit** →
   the commit noted in step 1.5. Environment changes are separate from
   deploys: set any variable you changed back by hand, then deploy.

---

## 4. Escrow v2: deploy the contract and switch the backend to it

Needs the R9 approval from step 1.1. Do steps 4 and 5 in one sitting: between
4.2 (the address book names v2) and 4.3 (the backend settles through it) the
frontend's 6-hourly smoke is red on purpose, because production is not on the
escrow the address book now names.

### 4.1 Deploy PaymentEscrow v2 (contracts repo)

1. **Before:** check that the settler you are about to fix into the contract is
   the key the backend really signs with. Both must print `$SETTLER`:

   ```sh
   curl -s "$BE_HOST/readiness" | jq -r .ratings.signer
   echo "$SETTLER"
   ```

   If they differ, stop: every `settle` would revert Unauthorized.
2. **Action**, from an up-to-date `main` of the contracts repo (after #5):

   ```sh
   git -C "$CONTRACTS" fetch origin && git -C "$CONTRACTS" status -sb   # on main, level with origin/main
   cd "$CONTRACTS"
   make deploy-escrow-v2 SETTLER=GDB4N25UYM3YNTTAWX7LSGI2P7OR62QZQXRNQWAGF5TFVENDKCTTCDHP
   ```

   `SOURCE` defaults to the stellar-cli identity `admin`, which pays for and
   signs the deploy, and `ADMIN` (the only address that can rotate the
   settler) defaults to that identity's address. The script refuses any
   network but testnet, reuses the testnet `agent_registry` and `asset_sac`
   from `addresses.json`, builds only the escrow wasm and deploys nothing else.
3. **Expected:** it prints the source, admin, settler, registry and asset, then
   `PaymentEscrow v2: C…`, then `version=2 settler=G… admin=G…`, then
   `✓ deployed.` and the address book, which now holds three new keys beside
   v1's untouched `payment_escrow`: `payment_escrow_v2`,
   `payment_escrow_v2_settler`, `payment_escrow_v2_admin`.
4. **Verify:** the script itself fails unless `version()` is 2, `settler()`
   equals `SETTLER` and `admin()` equals `ADMIN`, all read by simulation. Keep
   the id:

   ```sh
   export ESCROW_V2=$(python3 -c 'import json;print(json.load(open("addresses.json"))["payment_escrow_v2"])')
   echo "$ESCROW_V2"
   ```

   and open `https://stellar.expert/explorer/testnet/contract/$ESCROW_V2`.
5. **If it fails:** `✗ deploy did not return a contract id` means the RPC
   dropped the submission; the transaction may still land late. Check the
   `admin` account on Stellar Expert testnet before running it again, so you
   do not deploy two. Nothing was written to `addresses.json`.

### 4.2 Commit the address book (contracts repo)

```sh
cd "$CONTRACTS"
git add addresses.json
git commit -m "recorded escrow v2 on testnet"
git push origin main
```

The frontend's `docs/escrow-v2-switch.md` says to commit and push it to the
contracts repo's default branch. *(C6, closed 2026-09-30: `main` there took
the address book as a PR, contracts #6, merged.)* It must be
on `main` before step 5, because the frontend's `check:addresses` CI job and
`smoke` read the address book from the contracts repo.

- **Expected:** `git -C "$CONTRACTS" log -1 origin/main -- addresses.json` is
  your commit.
- **Do not** change the backend's `.env.example` or `render.yaml` escrow id.
  The backend's contract-drift check (`scripts/check_contract_drift.py`, run in
  CI and daily) compares their `STELLAR_PAYMENT_ESCROW` with the address book's
  `payment_escrow`, which stays v1's id.

### 4.3 Point the backend at v2 (Render)

1. **Action:** Render dashboard → the backend service → **Environment** → set
   `STELLAR_PAYMENT_ESCROW` to `$ESCROW_V2` → save → **Manual Deploy** →
   **Deploy latest commit**.
2. **Verify:**

   ```sh
   curl -s "$BE_HOST/readiness" | jq '{escrow, signer: .ratings.signer}'
   # escrow: { "contract": "<ESCROW_V2>", "version": 2 }  (null on the first probe: ask again)
   curl -s "$SITE/api/stellar/network" | jq -r .contracts.payment_escrow
   # <ESCROW_V2>
   ```

   - `escrow.contract` is `$ESCROW_V2` and `escrow.version` is `2`.
   - `ratings.signer` equals the `settler=` the deploy script printed
     (`$SETTLER`).
   - `demo_preflight` (step 9.2) re-checks both on-chain, as `escrow.version`
     and `escrow.settler`.
3. **Rollback:** set `STELLAR_PAYMENT_ESCROW` back to `$ESCROW_V1` and Manual
   Deploy. v1's id stays valid; nothing about it changed. Funds already in v2
   custody stay reclaimable by their payers after expiry regardless.

---

## 5. Frontend: pin v2, deploy, check

Follows the frontend's `docs/escrow-v2-switch.md`, steps 3–5. Right after 4.3.

### 5.1 The v2 pin PR (frontend repo)

1. **Action**, on a new branch from `origin/main` (in a worktree if the
   checkout is busy):
   - `lib/escrow-address.json`: set `"testnet"` to `$ESCROW_V2`. Leave
     `"public"` as `null`: v2 is not on mainnet. Never type a guessed or
     placeholder id here; every check compares it with a real source.
   - `README.md`: in the testnet contracts table, add a `PaymentEscrow v2` row
     with its Stellar Expert testnet link
     (`https://stellar.expert/explorer/testnet/contract/$ESCROW_V2`) beside the
     v1 row (`CBJPTMAP…25PI`), and label the v1 row as history. It stays in the
     evidence.
2. **Verify locally, before the PR:**

   ```sh
   cd "$FE"   # the worktree holding the pin branch
   ORIZON_CONTRACTS_DIR="$CONTRACTS" npm run check:addresses
   #   ok  escrow v2 pin (testnet)  C…
   ORIZON_CONTRACTS_DIR="$CONTRACTS" ORIZON_ESCROW_PINS=lib/escrow-address.json npm run smoke
   #   ✓ escrow v2 pin → live escrow is C…
   ```

   `check:addresses` compares the pin with `payment_escrow_v2` in the contracts
   address book, and every README contract link with the address book (which
   now holds both ids). `smoke` compares the pin with the escrow the **live**
   backend reports (`/api/stellar/network` → `contracts.payment_escrow`); once
   the address book records `payment_escrow_v2`, it also requires production's
   `payment_escrow` to be that id. A `null` pin prints as `pend`/pending and is
   not counted as checked.
3. **PR and merge:** open the PR to `main`, wait for the `addresses` job to go
   green, merge with a merge commit.

### 5.2 Vercel deploys `main`

1. **Expected:** Vercel builds and promotes the merge commit to production on
   its own. **Verify** in the Vercel dashboard that the production deployment's
   commit is the pin PR's merge commit.
2. **Verify the deployment:**

   ```sh
   cd "$FE"   # up to date with origin/main
   ORIZON_CONTRACTS_DIR="$CONTRACTS" npm run smoke
   ORIZON_CONTRACTS_DIR="$CONTRACTS" npm run check:addresses
   for p in /guide/list-your-agent /evidence /demo /litepaper /app/register; do
     printf '%s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "$SITE$p"
   done
   ```

   - `smoke` and `check:addresses` both pass, and the escrow pin line reads
     `ok`/`✓`, not pending.
   - Every page answers `200`. Redirects are not followed on purpose: a
     redirect to a login is a failure, not a pass.
3. **Verify the plan card**, with a funded testnet wallet (a team wallet),
   at `$SITE/app/orchestrator`: decompose any intent and read the card. Since
   frontend #97 the card's custody copy follows the escrow the backend
   reports (`lib/escrow-generation.ts`): v2 only when the pin is set and the
   backend reports it, v1 when the backend reports the v1 id, neutral
   otherwise. **Expected:** the v2 line ("…moves up to X from your wallet
   into escrow now…") and no "On-chain payment is paused" notice. If it shows, it names both ids: the
   pin and the backend disagree, and Authorize stays paused until they agree.
   Simulate is unaffected either way.
4. **Rollback:** Vercel dashboard → the frontend project → **Deployments** →
   the previous production deployment (noted in step 1.5) → **Promote to
   Production** (Instant Rollback). Then revert the pin PR (`"testnet": null`)
   so the next push to `main` does not re-deploy it. With the pin back at
   `null`, the checks report it as pending again.

---

## 6. Refunds on

Only after step 4.3 reads `escrow.version: 2`. An upheld dispute pays the
credit from the platform's own signing key (ADR 0002, ADR 0008), so that key
must hold it.

### 6.1 Fund the platform signing key

1. **Check its balance** (the settler, `$SETTLER`):

   ```sh
   curl -s "https://horizon-testnet.stellar.org/accounts/$SETTLER" \
     | jq '{balances: [.balances[] | select(.asset_type=="native") | .balance], subentries: .subentry_count}'
   ```

2. **It must be able to spend at least `MAX_REFUND_USDC` (the dashboard
   value from 3.1) plus 2 XLM of fees, above its reserve.** The reserve is
   (2 + subentries + sponsoring − sponsored) × 0.5 XLM. On testnet the escrow
   settles native XLM, so `MAX_REFUND_USDC` is an XLM amount here.
3. **If it is short:** fund it from friendbot
   (`https://friendbot.stellar.org/?addr=$SETTLER`) or send XLM from another
   team testnet wallet. Never paste its secret anywhere to do this.
4. **Verify:** `demo_preflight`'s `refunds.settler_balance` check (step 9.2)
   passes with `--max-refund` set to the dashboard's `MAX_REFUND_USDC`.

### 6.2 Turn both switches on

1. **Action:** Render → **Environment** → check that `API_KEY` and `DATABASE_URL`
   are set (3.1), then set `DISPUTE_REFUNDS_ENABLED=true` and
   `REFUND_RECONCILE_ENABLED=true` → save → **Manual Deploy** → **Deploy
   latest commit**.
2. **Expected:** the service boots. If it does not, and the log says
   `API_KEY` is required, the key is missing: turning refunds on makes it
   mandatory on every network.
3. **Verify:**

   ```sh
   curl -s "$BE_HOST/readiness" | jq '.disputes'
   # "store": "postgres", "reconcile": { "enabled": true, "running": true, ... }
   # ORIZON_API_KEY holds the dashboard's API_KEY; it is read from the
   # environment, never typed on the command line
   curl -s -X POST -H "X-API-Key: $ORIZON_API_KEY" "$SITE/api/disputes/no_such_dispute/uphold"
   # anything but {"detail": "dispute_refunds_disabled"} (503)
   ```

   `disputes.reconcile.enabled` is `true` only when **both** switches are on.
   The uphold probe must carry the key: since D-052 `require_adjudicator`
   checks the key first, so an anonymous call answers `401 invalid_api_key`
   whether refunds are on or off, and only a keyed call reaches the switch
   (`app/security.py`). The frontend demo script's GO condition ("uphold
   without a key answers 401, not 503") predates that and no longer tells the
   two apart. **Confirm** what a keyed uphold of an unknown id answers once
   the switch is on (the route's own not-found answer is expected); the point
   is that it is no longer `503 dispute_refunds_disabled`.
4. **Rollback:** set both switches back to `false` and Manual Deploy. A
   dispute already in `crediting` stays there for an operator to reconcile by
   hand (`docs/disputes.md`); nothing pays twice.

---

## 7. The operators (5.02, BLO-36)

One session per external operator, run from
[onboarding-session-runbook.md](onboarding-session-runbook.md) exactly as
written. This runbook adds only the order and the records.

1. **The day before each session**, the facilitator's checklist in the
   onboarding runbook (consent in writing and an `OP-n`, a buyer wallet that is
   not the operator's, the known blockers read, the friction log open).
   `curl -s "$SITE/api/stellar/network"` should now name `$ESCROW_V2` as
   `contracts.payment_escrow`. **F-019** (settlement to an external operator
   cannot land) is fixed since step 4 (2026-09-30), and the onboarding runbook
   no longer lists it as a known blocker, so step 7 of the session is where
   the settled workflow comes from.
2. **Run the session** (45–60 min). The facilitator rules are absolute: never
   handle the operator's secret, never register, bind or pay from a team
   wallet for them, never let them pay for their own workflow, never turn on
   fault injection on their deploy, never put their identity in the repository.
3. **Log friction live** into [friction-log.md](friction-log.md), the moment
   it happens: the next `F-0NN`, the date, `OP-n`, the step name, the exact
   on-screen text. Repeats get their own row (`same as F-0NN`).
4. **Produce the evidence** after each session, from the backend repo root
   with its virtualenv active:

   ```sh
   cd "$BE" && source .venv/bin/activate
   python -m scripts.adoption_report \
     --api https://orizons.xyz \
     --registry CAPHXWU53UZUZJGV7IAE57NNMH3YYB5MTWO6YA53KKMXSFVLOITBJ3GQ \
     --escrow "$ESCROW_V2" \
     --team-register app/data/team_wallets.json \
     --out-dir docs/evidence/5.02/$(date +%F)
   ```

   Take the ids from the contracts address book (`agent_registry`,
   `payment_escrow_v2`), not only from `/api/stellar/network`. **Expected:**
   exit 0 and one MET / NOT MET line per §6.3 target. A NOT MET line is still a
   valid report: commit it. Exit 5 or 6 means do not publish; file it. Exit 8
   means a chain read failed; run it again later. `--escrow` takes one id and
   the verifier checks v2 `settle` payouts, so name v2: the backend's adoption
   route reads the configured escrow only, and no v1 `charged` event was ever
   anyone but the platform paying itself (ADR 0010 D10).
5. **Record the Week-2 checkpoint and the fallback truthfully.**
   - On **BLO-36**, a comment stating what happened at the Week-2 checkpoint
     (Fri 2026-09-18): two named, committed operators, or the date the
     escalation to the Chapter Lead was raised. If it was raised late, the
     comment says late, and when.
   - If any operator came through the chapter fallback (a Stellar Philippines
     contributor), the session record says `recruited via: chapter fallback`,
     and the completion report must say so plainly, without being asked.
   - In the risk register, update **R5**'s status line with the same facts and
     the date.
6. **Verify:** `$SITE/app/ecosystem` shows each external operator under
   "External operators", and no team wallet there.
7. **Rollback:** nothing to roll back. An operator who withdraws consent
   before publication is removed from the evidence (their `OP-n` stays in the
   friction log without the wallet).

---

## 8. The live lifecycle (5.01, BLO-35)

Run from [lifecycle-harness.md](lifecycle-harness.md). Needs steps 4 and 6
(escrow v2 and refunds on); against v1 the harness stops at `verify` with exit
7. It moves real testnet funds and never retries a write.

**Prerequisites** (the harness's P1–P6): the API on testnet; escrow v2 live;
two external, bound agents owned by wallets that are neither buyer nor
settler; two funded buyer wallets; the operator `API_KEY` and
`DISPUTE_REFUNDS_ENABLED=true`; the backend virtualenv.

```sh
cd "$BE" && source .venv/bin/activate
# read -s keeps each value out of the terminal and the shell history
read -rs -p 'buyer 1 seed: ' BUYER_1_SECRET && export BUYER_1_SECRET; echo   # buyer wallet 1's S… seed
read -rs -p 'buyer 2 seed: ' BUYER_2_SECRET && export BUYER_2_SECRET; echo   # buyer wallet 2, a different account
read -rs -p 'API_KEY: ' ORIZON_API_KEY && export ORIZON_API_KEY; echo        # the deployment's operator API_KEY
```

The secrets are passed by variable **name**; nothing the harness prints or
writes contains them.

1. **Runs 1 and 2 (AC1–AC3): two agents, two buyers.** Dry-run each first
   (it builds and signs nothing), then run it for real:

   ```sh
   python -m scripts.lifecycle --api https://orizons.xyz --agent <agent_1> \
     --intent "<a task agent 1's skills fit>" \
     --buyer-secret-env BUYER_1_SECRET --adjudicator-key-env ORIZON_API_KEY \
     --evidence-dir docs/evidence/5.01/run-1 --dry-run
   # then the same without --dry-run

   python -m scripts.lifecycle --api https://orizons.xyz --agent <agent_2> \
     --intent "<a task agent 2's skills fit>" \
     --buyer-secret-env BUYER_2_SECRET --adjudicator-key-env ORIZON_API_KEY \
     --evidence-dir docs/evidence/5.01/run-2
   ```

   - **Expected:** the first lines say `escrow v2 (version() view)`; exit 0.
   - **Verify:** each `lifecycle.md` has an `authorize`, a `settle`, a `seal`,
     a `refund` and a `dispute_rating` transaction, each `SUCCESS`, and a
     `settlement_checks` row whose checks hold.
   - Exit 4 (the plan did not route to `--agent`): nothing was signed; reword
     the intent and use a **new** directory. Exit 6 (unknown outcome): look the
     hash up on Stellar Expert before anything else; never rerun the stage
     blindly. Exit 9: the directory already holds a run; resume it or use a new
     one, or you may authorize a second payment.
2. **AC4, a backend restart between seal and dispute:**

   ```sh
   python -m scripts.lifecycle ... --evidence-dir docs/evidence/5.01/ac4 --until verify
   # note the task id; then Render → the service → Manual Deploy → Restart service
   # wait until $SITE/api/health answers, then:
   python -m scripts.lifecycle ... --evidence-dir docs/evidence/5.01/ac4 --from-task <task_id>
   ```

   Leave out `--intent` on the resume. **Expected:** a `task_not_in_memory`
   row, then the dispute accepted and credited after the restart. That is the
   AC4 evidence, and it depends on `disputes.store` being `postgres`.
3. **AC5, an external endpoint that stops answering mid-workflow:** a second
   copy of the reference agent, registered and bound from its own wallet,
   deployed with `FAULT_MODE=hang_after:0` (or `hang_after:1` with
   `FAULT_SCOPE=intent`). Check `curl -sS <its url>/` shows `fault_injection`.
   Run with `--agent` set to the **healthy** agent and an intent both agents
   fit, in `docs/evidence/5.01/ac5`. **Expected:** the faulty step fails as
   `response_timeout` about 100 s after dispatch; `settlement_checks` shows
   charged events only for delivered steps, the buyer charged exactly the paid
   sum, and the seal present. Afterwards unset `FAULT_MODE` and redeploy it, or
   delete the service.
   - **Do not use the demo's faulty agent (step 9.1) for AC5.** Every run that
     routes to it writes another 20/100 rating, and the demo's figures (three
     ratings, lower bound 5443) assume exactly three. **Confirm** whether the
     team wants one faulty agent for both; if so, AC5 comes first and the
     demo's run count is re-derived from its live reputation.
4. **AC6, the reputation cycle:** start from an agent that has **never been
   rated** (its `start` snapshot reads `source: prior`). Run `--until verify`
   a few times, each in its own directory, then one full run. **Expected:**
   each `after_rating_N` snapshot moves the score, `source` moves from `prior`
   to `onchain`, `after_dispute` shows it fall, and the `reputation_summary`
   row says whether each stage moved.
5. **Commit the evidence**: `lifecycle.jsonl` and `lifecycle.md` from each
   directory. **Never** commit `state.json`: it holds the task read token (the
   harness writes a `.gitignore` beside it).
6. **Rollback:** none. Every write happened once, on-chain; the evidence is
   what it recorded. An unconfirmed stage is resumed (`--from-task`,
   `--from-dispute`), never repeated.

---

## 9. The demo (5.04, BLO-38)

Run from [demo-recording.md](demo-recording.md) and the frontend's
`content/demo/script.md` and `shot-list.md`. The video shows only real testnet
transactions.

### 9.1 Build the faulty agent (before the recording day)

From the frontend's `content/demo/script.md`, "How the below-floor agent is
made, honestly". No rating is ever written by hand.

1. **Deploy a second copy of the reference agent** as its own Render service
   with its own `ORIZON_ENDPOINT_URL`, the same `ORIZON_SIGNER` as the healthy
   one, `ORIZON_NETWORK=testnet` and `FAULT_MODE=hang_after:0`.
   **Verify:** `curl -sS <its url>/` reports
   `"fault_injection": "hang_after:0 scope=process"`.
2. **Register it from a third team wallet** (neither the buyer nor the
   settler), with a display name that discloses it (for example
   `Faulty test agent (deliberate)`), skills distinct from the operator's
   agent, and a price of **`0.20`**. Bind it to its endpoint.
3. **Declare the team wallets.** Add the faulty agent's owner, the buyer
   wallet and, if it is a team wallet, the operator wallet to
   `app/data/team_wallets.json` in the backend. That file is read by the
   running service (`app/services/adoption_svc.py`), so it needs a backend PR
   (merge commit), then a Render **Manual Deploy**. **Verify:** none of them
   appears under "External operators" on `$SITE/app/ecosystem`.
4. **Run it three times** with the lifecycle harness, each with a fresh
   directory and a differently worded intent that fits its skills:

   ```sh
   python -m scripts.lifecycle --api https://orizons.xyz --agent <faulty_id> \
     --intent "<run N: an intent its skills fit>" \
     --buyer-secret-env BUYER_1_SECRET \
     --evidence-dir docs/evidence/5.04/fault-run-N --until poll
   ```

   Exit 4 means nothing was signed: reword and use a new directory. Under v2
   an all-failed run settles empty and releases the whole cap back to the
   buyer, so each run costs only fees.
5. **Verify:** `curl -s "$SITE/api/stellar/reputation/<faulty_id>"` reads
   `source: "onchain"`, `count: 3`, `lower_bound_bps: 5443` (card: 2.72
   against the 2.75 floor), not stale; `/app/agents` shows it "below floor ·
   not eligible". If the agent already had ratings, the 0.20 × 3 table does not
   apply: run until `lower_bound_bps` ≤ 5489 and change S05's narration to the
   real count. Then leave it **bound, listed and untouched**: no further rating
   against it, and do not route it on the recording day.

### 9.2 Pre-flight: GO / NO-GO

The morning of the session, and again right before recording:

```sh
cd "$BE" && source .venv/bin/activate
python -m scripts.demo_preflight \
    --buyer G...BUYER --operator G...OPERATOR --cap 0.5 \
    --operator-endpoint <the operator agent's bound endpoint URL> \
    --max-refund <the dashboard's MAX_REFUND_USDC> \
    --with-decompose "the intent the video types" \
    --out-dir docs/evidence/5.04/preflight
```

- **Expected:** exit **0, GO**. Every required check PASS or WARN, including
  `build.readiness`, `escrow.version` (v2), `escrow.settler`,
  `refunds.enabled`, `refunds.store` (postgres), `refunds.settler_balance`,
  `exclusion.below_floor` (a bound, listed subject), `exclusion.card_figure`
  (lower bound ≤ 5489), `exclusion.routable_count` (at least 3 clear the
  floor), `operator.external`, `operator.endpoint` (no fault injection) and
  the frontend pages.
- Exit 4 or 5 is NO-GO: fix what the report names (5 means a required check
  was SKIPPED, usually a missing `--buyer` or `--operator`; SKIPPED is never a
  pass). Exit 3 is refused: not testnet or a bad flag.
- With `--allow-team-operator`, a team operator is a WARN that **must be
  disclosed on camera**.

### 9.3 Record, then the evidence sheet

1. **Record** against the deployment the pre-flight passed; the harness
   produces each take's on-chain evidence in its own directory
   (`--evidence-dir docs/evidence/5.04/take-N`).
2. **Build the sheet:**

   ```sh
   python -m scripts.demo_evidence docs/evidence/5.04/take-1 docs/evidence/5.04/take-2 \
       --title "Orizon Agents — Blue Belt demo (Stellar testnet)" \
       --disclose "<one sentence per team wallet the pre-flight named>" \
       --out-dir docs/evidence/5.04/video
   ```

   **Expected:** exit 0, every hash re-verified `SUCCESS`; it writes
   `evidence-sheet.md`, `description.txt` and `evidence.json`. Exit 5 means a
   hash failed: it is left out; rerun that stage or cut it from the video.
   Exit 8: a read failed; rerun.
3. **The browser-recorded hashes.** Transactions signed in the browser on
   camera (the operator's registration, the buyer's authorize in the console)
   are not in a harness `lifecycle.jsonl`. Add them to the step 2 command
   with `--tx KIND=HASH[:label]` (repeatable) or `--rows browser.json`; they
   are verified exactly like harness rows. Add `--index-links
   docs/evidence/5.04/video/index-links.json` to also write every verified
   hash in the evidence index's link shape, grouped by item, for step 10.
   The browser table and the rows format are in
   [demo-recording.md §2](demo-recording.md).

### 9.4 Upload and publish

1. Paste `description.txt` into the video description, replacing the summary
   and chapters placeholders with the edit's timestamps. List the faulty
   agent's owner wallet and its three failure-rating hashes.
2. Upload the video to YouTube as **Public**.
3. Publish `content/demo/demo.json` in the frontend (a PR, merge commit):
   `"status": "published"`, `video` with `provider: "youtube"`, the 11-character
   `id`, `title`, `duration_seconds` and `published_at`, the `chapters`, and
   `evidence` set to `evidence.json` **verbatim**. Commit the sheet to the
   backend's `docs/evidence/5.04/`.
4. **Verify** before merging:

   ```sh
   cd "$FE"   # the branch holding the published manifest
   npm run demo:check -- --video <the rendered video file>
   ```

   Exit 0 only: the manifest passes the build's own rules, and ffprobe measures
   the file at 180–300 s, matching `duration_seconds` to the second. Exit 3
   means ffprobe is missing and the duration was **not** verified; that is not
   a pass.
5. **Verify after deploy:** `$SITE/demo` answers 200 and plays; click one link
   of each kind through to Stellar Expert testnet.
6. **Rollback:** set `demo.json` back to `"status": "unpublished"` (no video,
   no evidence) in a PR, and make the video private or unlisted on YouTube.

---

## 10. The evidence index (5.05, BLO-39)

After steps 7–9, when the evidence exists. From [sow-metrics.md](sow-metrics.md)
and the frontend's after-deploy checklist.

1. **Measure the eleven §6.3 metrics** (read-only, no secret, about a minute):

   ```sh
   cd "$BE" && source .venv/bin/activate
   python -m scripts.sow_metrics --print-block --out-dir docs/evidence/5.05/metrics
   ```

   - **Expected:** exit 0, which means every metric was **measured**, whatever
     it came to. A measured miss is a successful measurement.
   - Exit 4: a read failed and at least one metric reads `"Not measured"`
     (never `0`). Run it again; do not publish a Not measured row you can
     remeasure.
   - Exit 3: refused (not testnet, bad flag, unreadable team register).
   - It reads the live escrow and the known v1 escrow, so v1's history is still
     counted after the switch.
   - **Consent.** By default (`--withhold-external`) the block and the
     Markdown name no outside operator: their links are replaced by one link
     to `$SITE/app/ecosystem`, and the counts do not change. Add
     `--publish-external` only once every outside operator in it has
     consented in writing (see [sow-metrics.md](sow-metrics.md#consent)).
     `sow-metrics.raw.json` always holds every wallet and hash: while anything
     is withheld, do not commit it to the repository or publish it.
2. **Update `content/evidence/index.json`** in the frontend, per
   `docs/evidence-index-after-deploy.md` (frontend repo, merged with #97 on
   2026-09-29; follow it as written):
   - paste `sow-metrics.block.json` as the `metrics` array, **unchanged**;
   - keep `sow-metrics.raw.json` with the team: it answers "which items were
     excluded, and why", and it goes into the published evidence only once
     every outside operator it names has consented;
   - update the items that were partial or missing because a page was not
     deployed or a transaction did not exist yet, pointing each at the live
     page or the verified transaction instead of its PR. No drill, fixture or
     test transaction is presented as deliverable evidence.
3. **Verify every link live:**

   ```sh
   cd "$FE"   # the branch holding the updated index
   npm run evidence:verify
   ```

   Exit 0 only. Exit 1: something failed; fix it. Exit 3: nothing failed but a
   link could not be checked; that is **not** a pass, run it again. Exit 2:
   bad usage, or refused as not testnet. The report goes to
   `<tmp>/orizon-evidence-report` unless `--report-dir` says otherwise; keep it
   for 6.04. Then PR, merge commit, and check `$SITE/evidence` after Vercel
   deploys.
4. **6.04 re-verification (Rie, BLO-43):** Rie independently re-verifies
   every on-chain claim in the index against Stellar Expert testnet and keeps
   the live `evidence:verify` report. **Confirm** where BLO-43 wants the
   report attached; the card is already `Done` from the earlier weeks, so
   **confirm** whether the Epic 5 re-run reopens it or gets its own issue.
5. **The §6.2 sign-off (the Chapter Lead):** the Chapter Lead reviews the
   published index and signs off. **Confirm** the form it takes and where it is
   recorded (the check-in issue, BLO-138, is the natural place).
6. **Rollback:** revert the index PR; the previous index stays valid, since
   every value in it was measured when it was written.

---

## 11. Closing (Rie, with Dan's inputs)

1. **The Week-4 evidence bundle (BLO-134, due Fri 2026-10-02).** SOW §6.1 row
   for Week 4 / D4: the demo video, the public guide URL
   (`$SITE/guide/list-your-agent`), the settled-workflow hashes (from 8 and
   7.4), that week's merged PRs (step 2), and the Week-4 X post. Start
   assembling Thu 2026-10-01.
2. **The X post (BLO-136, due Fri 2026-10-02).** One post, tagging
   @StellarOrg @PHI_stellar @riseinweb3, claiming only what has shipped. The
   escrow asset on testnet is native XLM: no "earning USDC" claim. Add its URL
   to BLO-134.
3. **The check-in (BLO-138, Wed 2026-09-30).** Bring specific blockers:
   anything in this runbook not done, and every open **confirm** item below
   that needs the Chapter Lead. Record attendance, what was raised, what was
   answered and any commitments as a comment on BLO-138.
4. **Linear status updates.** Move each story to the state its evidence
   supports, never further: 5.01 (BLO-35), 5.02 (BLO-36), 5.03 (BLO-37), 5.04
   (BLO-38), 5.05 (BLO-39) and 5.06 (BLO-47, still in Backlog on 2026-09-29
   although its PR is open), then the epic (BLO-9). Update R5 and R9 in the
   risk register (steps 1.1 and 7.5).
5. **Verify:** BLO-134 links every item it lists, and each link opens.

---

## 12. Rollback, per step

Roll back the latest step first and work backwards. Nothing here deletes a
contract, a transaction or a deployment.

| Step | Undo | Result |
|---|---|---|
| 2. Merges | a revert PR of the merge commit (`git revert -m 1 <sha>`), merged with a merge commit; never force-push `main` | `main` without that PR |
| 3. Render deploy | Render → Manual Deploy → **Deploy a specific commit** → the commit from step 1.5; put back any environment value you changed | the backend as it was |
| 4.1–4.2 Escrow v2 contract | nothing to undo on-chain. **v1's id stays valid**: `payment_escrow` in the address book never changed. Revert the address-book commit only if v2 is abandoned, and only after 5.1 is reverted, or `check:addresses` fails | v2 exists, unused |
| 4.3 Backend on v2 | Render → set `STELLAR_PAYMENT_ESCROW` to `$ESCROW_V1` → Manual Deploy | runs settle through v1 again (which cannot pay external operators, D-039); v2 custody stays reclaimable by each payer after expiry |
| 5. Frontend | Vercel → Deployments → the previous production deployment → **Promote to Production**; then a PR setting the pin back to `"testnet": null` | the previous site; the checks report the pin as pending |
| 6. Refunds on | Render → `DISPUTE_REFUNDS_ENABLED=false`, `REFUND_RECONCILE_ENABLED=false` → Manual Deploy | no new credits; a dispute in `crediting` waits for a human (`docs/disputes.md`) |
| 7–8. Operators, lifecycle | none: on-chain facts stand. Resume an unconfirmed stage with `--from-task` / `--from-dispute`; never repeat it | |
| 9. Demo | `demo.json` back to `"status": "unpublished"`; the video private or unlisted | `/demo` shows the unpublished state |
| 10. Evidence index | revert the index PR | the previous, measured index |

**The escrow pair must move together.** If the backend goes back to v1
(4.3), the frontend pin must go back to `null` in the same sitting.
Otherwise the plan card sees a v2 pin and a v1 backend and pauses Authorize
with a notice naming both ids (Simulate still works).

---

## Confirm list

Everything this runbook could not settle from a source. Close each one
before the step that needs it, and record the answer where it says.

| # | Step | Confirm | Ask |
|---|---|---|---|
| ~~C1~~ | 1.1 | **Closed 2026-09-30.** The lead confirmed that the Chapter Lead approved the escrow v2 testnet deploy on 2026-09-30 | — |
| C2 | 1.2, 7.5 | What actually happened at the Week-2 operator checkpoint (Fri 2026-09-18): committed operators, or the date of the escalation. Nothing is recorded on BLO-36 or R5 | Dan |
| C3 | 1.3 | Whether Rie facilitates or observes the operator sessions | Dan, Rie |
| ~~C4~~ | 1.4 | **Closed 2026-09-30.** The stellar-cli identity `admin` is the address book's `admin`, `GA7AI5…5OQV`: the v2 deploy script read `admin()` back as that address | — |
| ~~C6~~ | 4.2 | **Closed 2026-09-30.** The contracts repo took the address book as a PR, #6 (merged) | — |
| C7 | 6.2 | What a keyed `uphold` of an unknown dispute id answers once refunds are on (expected: the dispute service's refusal, not `503 dispute_refunds_disabled`) | read `dispute_svc.uphold` |
| C8 | 8.3 | Whether AC5 uses its own faulty agent (this runbook's default) or the demo's; sharing one changes the demo's rating count | Dan |
| ~~C10~~ | 10.2 | **Closed 2026-09-30.** The frontend's `docs/evidence-index-after-deploy.md` merged with frontend #97 on 2026-09-29 and is on `main` | — |
| C11 | 10.4 | Where the 6.04 re-verification report goes, and whether it reopens BLO-43 (Done) or gets its own issue | Rie |
| C12 | 10.5 | The form of the Chapter Lead's §6.2 sign-off and where it is recorded | Chapter Lead |

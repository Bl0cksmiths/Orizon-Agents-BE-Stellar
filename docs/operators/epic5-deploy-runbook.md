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
```

`SETTLER` is the public half of the production signing key (the one Render's
`STELLAR_SIGNING_KEY` holds), per the frontend's `docs/escrow-v2-switch.md`.
`ESCROW_V1` is `payment_escrow` in the contracts repo's `addresses.json`
(testnet). `orizons.xyz` does not proxy `/readiness`, so it is always read from
`$BE_HOST`.

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
| Evidence index (5.05) | [sow-metrics.md](sow-metrics.md); frontend `content/evidence/index.json`, `docs/evidence-index-after-deploy.md` (being written by another lane) |
| Gates | Linear: "Sprint Risk Register & Decision Log" (R5, R9), BLO-36 |

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
     if it is given at the Wed 2026-09-30 check-in). **Confirm** that this is
     where the Chapter Lead wants it recorded.
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
     `admin`, `GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV`
     (**confirm**: the deploy script defaults the escrow admin to this
     identity's address, and no source states the two are the same key).
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

**Merge commits only. Never squash, never rebase-merge.** Every stack below
was built as a chain of branches, each on the one before it, and every PR
targets `main`. A merge commit keeps each lower branch's commits in `main`
with their original ids, so the next PR in the stack shrinks to its own
commits. A squash rewrites them, and the next PR then shows the whole lower
stack again as conflicts.

Open PRs on 2026-09-29 (`gh pr list`, all MERGEABLE):

| Repo | PR | Branch | What it is | Stacked on |
|---|---|---|---|---|
| contracts | #5 | `chore/mit-license` | the MIT licence | — |
| backend | #90 | `feat/5.03-friction-coverage` | 5.03: friction log → guide coverage | #89 (merged) |
| backend | #91 | `feat/5.04-demo-tools` | 5.04: `demo_preflight`, `demo_evidence` | #90 |
| backend | #93 | `feat/5.05-sow-metrics` | 5.05: `sow_metrics` | #91 |
| backend | #92 | `chore/mit-license` | the MIT licence | — |
| frontend | #91 | `feat/5.03-integration` | 5.03: the `/guide/list-your-agent` guide | #90, #89 (merged) |
| frontend | #92 | `feat/5.04-integration` | 5.04: the demo script, `/demo`, honest copy | #91 |
| frontend | #94 | `feat/5.05-integration` | 5.05: the public evidence index, `/evidence` | #92 |
| frontend | #95 | `feat/5.06-integration` | 5.06: the litepaper | #94 |
| frontend | #93 | `chore/mit-license` | the MIT licence | — |

Already merged and deployed: backend #88 (5.01) and #89 (5.02); frontend #89
(5.01, the v2 console) and #90 (5.02). The example agent has no open PR and
GitHub already detects its MIT licence.

Backend #91 and #93 change scripts, docs and tests only, nothing in `app/`
(their PR descriptions say so). #90 adds the friction-coverage mapping. So the
backend behaviour step 3 deploys is 5.01 + 5.02 (#88, #89), which are already
on `main` but, per the demo script's live check on 2026-09-29, not yet on
Render.

### The order

For each PR: wait for its checks to go green, then merge it.

```sh
# contracts
gh pr merge 5  -R Bl0cksmiths/Orizon-Agents-Smart-Contract-Stellar --merge

# backend: the stack, bottom first, then the licence
gh pr merge 90 -R Bl0cksmiths/Orizon-Agents-BE-Stellar --merge
gh pr merge 91 -R Bl0cksmiths/Orizon-Agents-BE-Stellar --merge
gh pr merge 93 -R Bl0cksmiths/Orizon-Agents-BE-Stellar --merge
gh pr merge 92 -R Bl0cksmiths/Orizon-Agents-BE-Stellar --merge
# then the Epic 5 audit-fix PR (number not yet known: confirm)

# frontend: #91 and #92 together, then the rest, then the licence
gh pr merge 91 -R Bl0cksmiths/Orizon-Agents-FE-Stellar --merge
gh pr merge 92 -R Bl0cksmiths/Orizon-Agents-FE-Stellar --merge
gh pr merge 94 -R Bl0cksmiths/Orizon-Agents-FE-Stellar --merge
gh pr merge 95 -R Bl0cksmiths/Orizon-Agents-FE-Stellar --merge
gh pr merge 93 -R Bl0cksmiths/Orizon-Agents-FE-Stellar --merge
# then the Epic 5 audit-fix PR (number not yet known: confirm)
```

**Frontend #91 and #92 ship together.** Vercel deploys every push to
`main`, so merge #92 immediately after #91, before Vercel has promoted a
deployment of #91 alone if you can, and at the latest before anyone is pointed
at the guide. The guide in #91 and the Register page's labels agree only from
#92 on: #92 is where the Register label (and receipts, the dispute dialog, the
trace and the dashboard) stops hard-coding "USDC" and follows the network
asset, XLM on testnet (friction F-022).

After each merge:

1. **Expected:** the next PR in the stack shows only its own commits (`gh pr
   view <n> --json commits --jq '.commits | length'` drops). If it suddenly
   shows the lower stack's commits again, a squash happened: stop.
2. **Verify:** `git -C "$BE" fetch origin && git -C "$BE" log --oneline --merges
   -3 origin/main` (and the same for `$FE`) shows a merge commit per PR.
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
sitting so it closes as soon as possible.

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

1. **Before:** confirm the settler you are about to fix into the contract is
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
contracts repo's default branch. **Confirm** whether `main` there takes a
direct push or needs a PR (merged with a merge commit); either way it must be
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
   frontend #89 the card's payment copy always describes v2 custody: the
   signature moves the plan's maximum into escrow, delivered steps are paid
   from it and the rest comes back. What the pin changes is that the card now
   has a v2 id to compare with the backend's escrow. **Expected:** no
   "On-chain payment is paused" notice. If it shows, it names both ids: the
   pin and the backend disagree, and Authorize stays paused until they agree.
   Simulate is unaffected either way.
4. **Rollback:** Vercel dashboard → the frontend project → **Deployments** →
   the previous production deployment (noted in step 1.5) → **Promote to
   Production** (Instant Rollback). Then revert the pin PR (`"testnet": null`)
   so the next push to `main` does not re-deploy it. With the pin back at
   `null`, the checks report it as pending again.

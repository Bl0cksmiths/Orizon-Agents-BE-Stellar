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

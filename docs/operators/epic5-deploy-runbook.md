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

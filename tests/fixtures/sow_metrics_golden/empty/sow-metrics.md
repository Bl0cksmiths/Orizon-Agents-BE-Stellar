# SOW §6.3 success metrics (story 5.05)

**0 of 11 met.** (exit 0)

Measured 2026-09-21T14:13:20Z on Stellar testnet, read-only: API `https://orizon.test`, backend `https://backend.test`, frontend `https://front.test`, RPC `https://rpc.test`, Horizon `https://horizon.test`, GitHub `https://github-api.test`.

| # | Category | Metric (SOW) | Target | Achieved | Status |
|---|---|---|---|---|---|
| m01 | Adoption targets | Externally-operated agents registered on Testnet | ≥ 2 | 0 | **Not met** |
| m02 | Adoption targets | Unique external operator wallet addresses | ≥ 2 | 0 | **Not met** |
| m03 | Transaction targets | Workflows routed to external agents & settled on Testnet | ≥ 3 | 0 | **Not met** |
| m04 | Transaction targets | On-chain USDC settlements (charges) recorded | ≥ 3 | 0 | **Not met** |
| m05 | Transaction targets | Dispute → partial-refund settlements | ≥ 1 | 0 | **Not met** |
| m06 | Technical milestones | Permissionless AgentRegistry.register flow live on the dApp | Yes | No | **Not met** |
| m07 | Technical milestones | Reputation-gated routing (reads avg_bps, applies a floor) live | Yes | No | **Not met** |
| m08 | Technical milestones | Automated dispute window + partial-credit refund live | Yes | Partly: the dispute routes are deployed, but no dispute window has opened yet, because no payment has settled on escrow v2, and refunds are switched off. | **Not met** |
| m09 | Technical milestones | Public "List your agent on Orizon" integration guide published | Yes | No | **Not met** |
| m10 | Technical milestones | 3–5 min demo video published | Yes | No | **Not met** |
| m11 | Technical milestones | All source code released under MIT License | Yes | No | **Not met** |

## m01. Externally-operated agents registered on Testnet

- **Target** ≥ 2; **achieved** 0; **not met**.
- **Why not:** No agent is registered on the AgentRegistry contract yet.
- **How it was measured:** Read every agent the AgentRegistry contract lists, with its owner, straight from testnet. An agent counts only if its owner is neither one of the team's wallets (the committed register) nor a key the platform runs (network admin, dispatch signer, ratings signer and scorer, registry admin, escrow settler and admin, ledger scorer, attestation sealer).
- **Counted:** 0; **excluded:** 0.
- **Proof:**
  - [AgentRegistry contract (every registered agent)](https://stellar.expert/explorer/testnet/contract/CCDSJENDBVQNLGEWFXTOPOBUVN3LFKTF7OVRALDOXKVONLG4EOECECFN)

## m02. Unique external operator wallet addresses

- **Target** ≥ 2; **achieved** 0; **not met**.
- **Why not:** No outside operator wallet owns an agent yet; the 0 registered agents belong to 0 wallets the team controls.
- **How it was measured:** The distinct owner wallets of the registered agents, each checked the same way as the row above. A wallet counts only if it is neither one of the team's wallets (the committed register) nor a key the platform runs (network admin, dispatch signer, ratings signer and scorer, registry admin, escrow settler and admin, ledger scorer, attestation sealer).
- **Counted:** 0; **excluded:** 0.

## m03. Workflows routed to external agents & settled on Testnet

- **Target** ≥ 3; **achieved** 0; **not met**.
- **Why not:** No agent owned by an outside operator exists yet, so no workflow could be routed to one and settled. None of the 0 charges on record paid one.
- **How it was measured:** Walked every id each escrow contract has issued (its id counter numbers every authorization and receipt, so this does not depend on how long the network keeps events) and read each receipt and the payer that authorized it. A v1 charge and each payout of a v2 settle count as one charge. A charge is excluded when it is a self-payment (the payer owns the agent, is the escrow's settler, or is a platform key) or settled before the sprint began on 2026-09-07. Testnet settles in native XLM, not USDC: the escrow's payment asset is the native XLM asset contract. A workflow counts when at least one of its counted charges paid an agent owned by an outside operator; several charges of one job are one workflow.
- **Counted:** 0; **excluded:** 0.
- **Proof:**
  - [PaymentEscrow v2 contract (live) (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CC4HZXMIK4L4OQQDQCPEQ4OUYOCHWUFCW3QAJDEENENKNJYYL6GP3NEX)
  - [PaymentEscrow v1 contract (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI)

## m04. On-chain USDC settlements (charges) recorded

- **Target** ≥ 3; **achieved** 0; **not met**.
- **Why not:** No charge has ever been recorded on the escrow contracts.
- **How it was measured:** Walked every id each escrow contract has issued (its id counter numbers every authorization and receipt, so this does not depend on how long the network keeps events) and read each receipt and the payer that authorized it. A v1 charge and each payout of a v2 settle count as one charge. A charge is excluded when it is a self-payment (the payer owns the agent, is the escrow's settler, or is a platform key) or settled before the sprint began on 2026-09-07. Testnet settles in native XLM, not USDC: the escrow's payment asset is the native XLM asset contract.
- **Counted:** 0; **excluded:** 0.
- **Proof:**
  - [PaymentEscrow v2 contract (live) (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CC4HZXMIK4L4OQQDQCPEQ4OUYOCHWUFCW3QAJDEENENKNJYYL6GP3NEX)
  - [PaymentEscrow v1 contract (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI)

## m05. Dispute → partial-refund settlements

- **Target** ≥ 1; **achieved** 0; **not met**.
- **Why not:** No dispute has been refunded yet. The reputation ledger holds 0 ratings and no dispute rating; its lifetime dispute count is 0 across all 0 agents. No charge has settled since the sprint began on 2026-09-07 that a dispute could refund.
- **How it was measured:** Read the dispute ratings the platform wrote to the ReputationLedger (from its keys' full transaction history) and traced each back, through the derived job id the backend records a dispute under, to the charge it disputes. A refund counts only when that charge counts and the platform then paid its payer back over the asset contract, no more than the charge. A transfer with no dispute behind it (such as a refund drill) does not count. The ledger's lifetime dispute count for every agent is read as a cross-check.
- **Counted:** 0; **excluded:** 0.
- **Proof:**
  - [ReputationLedger contract (every rating)](https://stellar.expert/explorer/testnet/contract/CD7BIAILJ7UDGA4FF4CGPSIZ56NHZIEJXEPJNY5K27KCNXMHA6JJOFNA)

## m06. Permissionless AgentRegistry.register flow live on the dApp

- **Target** Yes; **achieved** No; **not met**.
- **Why not:** The /app/register page answers HTTP 404: it has not been deployed yet and the live backend does not publish the route that builds a registration transaction.
- **How it was measured:** Opened the /app/register page on the live dApp with no login, and checked the live backend publishes the route that builds an unsigned registration transaction for the owner's own wallet to sign. The registry contract's register needs only the owner's signature, no admin. The page answered 404 when this ran, so it is not linked: a dead link would prove nothing.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - [Live backend route list](https://backend.test/openapi.json)
  - [AgentRegistry contract (every registered agent)](https://stellar.expert/explorer/testnet/contract/CCDSJENDBVQNLGEWFXTOPOBUVN3LFKTF7OVRALDOXKVONLG4EOECECFN)

## m07. Reputation-gated routing (reads avg_bps, applies a floor) live

- **Target** Yes; **achieved** No; **not met**.
- **Why not:** The live reputation settings name no floor, so no agent can be left out for low reputation.
- **How it was measured:** Read the live reputation settings the router applies (whether the floor is on, and its value) from the deployed API. The router scores each agent from the ReputationLedger's on-chain average and leaves out any agent below the floor.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - [Live reputation settings](https://orizon.test/api/stellar/reputation/params)
  - [ReputationLedger contract (every rating)](https://stellar.expert/explorer/testnet/contract/CD7BIAILJ7UDGA4FF4CGPSIZ56NHZIEJXEPJNY5K27KCNXMHA6JJOFNA)

## m08. Automated dispute window + partial-credit refund live

- **Target** Yes; **achieved** Partly: the dispute routes are deployed, but no dispute window has opened yet, because no payment has settled on escrow v2, and refunds are switched off.; **not met**.
- **Why not:** No dispute window has opened yet, because no payment has settled on escrow v2, and refunds are switched off.
- **How it was measured:** Checked that the live backend publishes the dispute routes (open, read, uphold, reject); that a dispute window can open at all, which needs a settled payment, and so the escrow v2 contract live; that its readiness report shows refunds switched on (disputes.reconcile.enabled, true only when refunds and the refund sweep are both on); and that at least one dispute has been refunded on-chain (the dispute refund row above). Yes when all hold, Partly when the routes are deployed but the rest does not, and No when the routes are not.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - [Live backend route list](https://backend.test/openapi.json)
  - [Live backend readiness report](https://backend.test/readiness)
  - [ReputationLedger contract (every rating)](https://stellar.expert/explorer/testnet/contract/CD7BIAILJ7UDGA4FF4CGPSIZ56NHZIEJXEPJNY5K27KCNXMHA6JJOFNA)

## m09. Public "List your agent on Orizon" integration guide published

- **Target** Yes; **achieved** No; **not met**.
- **Why not:** The /guide/list-your-agent page answers HTTP 404: it has not been deployed yet.
- **How it was measured:** Opened the /guide/list-your-agent page on the live dApp with no login; published means it answers there. The page answered 404 when this ran, so it is not linked: a dead link would prove nothing.
- **Counted:** 1; **excluded:** 0.

## m10. 3–5 min demo video published

- **Target** Yes; **achieved** No; **not met**.
- **Why not:** The /demo page answers but carries no published marker (data-demo), so the video cannot be confirmed as published.
- **How it was measured:** Opened the /demo page on the live dApp with no login and read the published marker the page renders (data-demo="published") and the video's running time, which must be 3 to 5 minutes.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - [Demo page](https://front.test/demo)

## m11. All source code released under MIT License

- **Target** Yes; **achieved** No; **not met**.
- **Why not:** 4 of the 4 repositories have no MIT licence that GitHub recognises: the frontend (no licence detected), the backend (no licence detected), the smart contracts (no licence detected) and the example agent (no licence detected).
- **How it was measured:** Asked GitHub which licence it detects on each repository: the frontend, backend and smart contracts the SOW names, and the example agent. Each must be MIT.
- **Counted:** 0; **excluded:** 4.
- **Proof:**
  - [Frontend repository — GitHub detects no licence](https://github.com/Bl0cksmiths/Orizon-Agents-FE-Stellar)
  - [Backend repository — GitHub detects no licence](https://github.com/Bl0cksmiths/Orizon-Agents-BE-Stellar)
  - [Smart contracts repository — GitHub detects no licence](https://github.com/Bl0cksmiths/Orizon-Agents-Smart-Contract-Stellar)
  - [Example agent repository — GitHub detects no licence](https://github.com/Bl0cksmiths/Orizon-Agents-Example-Agent-Stellar)

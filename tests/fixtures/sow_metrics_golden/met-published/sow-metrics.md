# SOW §6.3 success metrics (story 5.05)

**11 of 11 met.** (exit 0)

Measured 2026-09-21T14:13:20Z on Stellar testnet, read-only: API `https://orizon.test`, backend `https://backend.test`, frontend `https://front.test`, RPC `https://rpc.test`, Horizon `https://horizon.test`, GitHub `https://github-api.test`.

| # | Category | Metric (SOW) | Target | Achieved | Status |
|---|---|---|---|---|---|
| m01 | Adoption targets | Externally-operated agents registered on Testnet | ≥ 2 | 3 | **Met** |
| m02 | Adoption targets | Unique external operator wallet addresses | ≥ 2 | 2 | **Met** |
| m03 | Transaction targets | Workflows routed to external agents & settled on Testnet | ≥ 3 | 3 | **Met** |
| m04 | Transaction targets | On-chain USDC settlements (charges) recorded | ≥ 3 | 4 | **Met** |
| m05 | Transaction targets | Dispute → partial-refund settlements | ≥ 1 | 1 | **Met** |
| m06 | Technical milestones | Permissionless AgentRegistry.register flow live on the dApp | Yes | Yes | **Met** |
| m07 | Technical milestones | Reputation-gated routing (reads avg_bps, applies a floor) live | Yes | Yes | **Met** |
| m08 | Technical milestones | Automated dispute window + partial-credit refund live | Yes | Yes | **Met** |
| m09 | Technical milestones | Public "List your agent on Orizon" integration guide published | Yes | Yes | **Met** |
| m10 | Technical milestones | 3–5 min demo video published | Yes | Yes | **Met** |
| m11 | Technical milestones | All source code released under MIT License | Yes | Yes | **Met** |

## m01. Externally-operated agents registered on Testnet

- **Target** ≥ 2; **achieved** 3; **met**.
- **How it was measured:** Read every agent the AgentRegistry contract lists, with its owner, straight from testnet. An agent counts only if its owner is neither one of the team's wallets (the committed register) nor a key the platform runs (network admin, dispatch signer, ratings signer and scorer, registry admin, escrow settler and admin, ledger scorer, attestation sealer).
- **Counted:** 3; **excluded:** 3.
- **Proof:**
  - [AgentRegistry contract (every registered agent)](https://stellar.expert/explorer/testnet/contract/CCDSJENDBVQNLGEWFXTOPOBUVN3LFKTF7OVRALDOXKVONLG4EOECECFN)
  - [Registration of house_agent by the team's contract admin and v1 escrow settler — 2026-04-20 (excluded: team wallet)](https://stellar.expert/explorer/testnet/tx/045ef594d81d2f2134d61151ed71260d8f79e657c7cb6ed1d893688532017409)
  - [Registration of qa_agent by the team's QA throwaway operator key — 2026-09-17 (excluded: team wallet)](https://stellar.expert/explorer/testnet/tx/eea1ad3fbf2142ede510d0220518d902a5ba9b502851530d7fc1454f5147206c)
  - [Registration of signer_agent by the platform's ratings signer — 2026-09-18 (excluded: platform key)](https://stellar.expert/explorer/testnet/tx/9b66130d2c7c05ee662b24fdca0a32bfda1a0cb1102fb3e53168eb61b378fc6d)
  - [Registration of alpha by an outside operator's wallet — 2026-09-19 (counted: outside operator)](https://stellar.expert/explorer/testnet/tx/05320dd888b1da6f0de8cbf6e50cf39572ef9678ffca974b5372c3dcbe5b6716)
  - [Registration of beta by an outside operator's wallet — 2026-09-20 (counted: outside operator)](https://stellar.expert/explorer/testnet/tx/1684297ae612416c19f3e86ebc48de53cb016a6eacfa8f2400bf108a4dd71a3e)
  - [Registration of gamma by an outside operator's wallet — 2026-09-20 (counted: outside operator)](https://stellar.expert/explorer/testnet/tx/fab2f5a389ed60fd654935fcd8fa098ce3815ba66f48c9e6b74335eb0c758eba)

## m02. Unique external operator wallet addresses

- **Target** ≥ 2; **achieved** 2; **met**.
- **How it was measured:** The distinct owner wallets of the registered agents, each checked the same way as the row above. A wallet counts only if it is neither one of the team's wallets (the committed register) nor a key the platform runs (network admin, dispatch signer, ratings signer and scorer, registry admin, escrow settler and admin, ledger scorer, attestation sealer).
- **Counted:** 2; **excluded:** 3.
- **Proof:**
  - [An outside operator's wallet — owns beta and gamma (counted: outside operator)](https://stellar.expert/explorer/testnet/account/GBL32VQIVY7O4766EQQMQ77IETKMCOU6IHHT2E2OQVJ75WJ6IJ3GDV6R)
  - [An outside operator's wallet — owns alpha (counted: outside operator)](https://stellar.expert/explorer/testnet/account/GBXZSLYTTOFUALWIVA3H5GMGLZWRM63NBI5X2IGM3UYOAG3SH6KZPTLF)
  - [The platform's ratings signer — owns signer_agent (excluded: platform key)](https://stellar.expert/explorer/testnet/account/GBFZRFVWIXH6ABE55TCZ2XAPACNVWBGW2P7EKZECRRZBULOKPZGPSS6N)
  - [The team's QA throwaway operator key — owns qa_agent (excluded: team wallet)](https://stellar.expert/explorer/testnet/account/GBNRRKOSAXFYIWGSEEYM34SCNNREKRB65GRLHMBHRNGMF3FDW3XTHCNZ)
  - [The team's contract admin and v1 escrow settler — owns house_agent (excluded: team wallet)](https://stellar.expert/explorer/testnet/account/GDPH6XWBZBHHNKJQWR3N6RHTCDCYKJ2KC6XUG4NCH7E46PHJWYMPO7NO)

## m03. Workflows routed to external agents & settled on Testnet

- **Target** ≥ 3; **achieved** 3; **met**.
- **How it was measured:** Walked every id each escrow contract has issued (its id counter numbers every authorization and receipt, so this does not depend on how long the network keeps events) and read each receipt and the payer that authorized it. A v1 charge and each payout of a v2 settle count as one charge. A charge is excluded when it is a self-payment (the payer owns the agent, is the escrow's settler, or is a platform key) or settled before the sprint began on 2026-09-07. Testnet settles in native XLM, not USDC: the escrow's payment asset is the native XLM asset contract. A workflow counts when at least one of its counted charges paid an agent owned by an outside operator; several charges of one job are one workflow.
- **Counted:** 3; **excluded:** 1.
- **Proof:**
  - [PaymentEscrow v2 contract (live) (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CC4HZXMIK4L4OQQDQCPEQ4OUYOCHWUFCW3QAJDEENENKNJYYL6GP3NEX)
  - [PaymentEscrow v1 contract (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI)
  - [Charge of 0.114 XLM to house_agent on the v1 escrow — 2026-05-13 (excluded: self-payment (the payer owns the agent); settled before the sprint began on 2026-09-07; the agent is owned by the team's contract admin and v1 escrow settler)](https://stellar.expert/explorer/testnet/tx/8ed5b22e01e6e2d7bd996f0b92e42d5e5872fdea3d9cebb4c34025e22c9206db)
  - [Charge of 0.1 XLM to alpha on the v2 escrow — 2026-09-21 (counted)](https://stellar.expert/explorer/testnet/tx/eb6142a63cec25237fb684ae1519830811eed751e6e01378c84d3485ff72c6dc)
  - [Charge of 0.2 XLM to beta on the v2 escrow — 2026-09-22 (counted)](https://stellar.expert/explorer/testnet/tx/d2e1ae97b92f28113c71d63ea2250041330628ed1c2abec23e99c8e44e4b6664)
  - [Charge of 0.05 XLM to alpha on the v2 escrow — 2026-09-23 (counted)](https://stellar.expert/explorer/testnet/tx/f326ccf5f6c6b55742c6d9eea7d25fbe41be14e5cb73ef382e771805801237f7)
  - [Charge of 0.05 XLM to gamma on the v2 escrow — 2026-09-23 (counted)](https://stellar.expert/explorer/testnet/tx/f326ccf5f6c6b55742c6d9eea7d25fbe41be14e5cb73ef382e771805801237f7)

## m04. On-chain USDC settlements (charges) recorded

- **Target** ≥ 3; **achieved** 4; **met**.
- **How it was measured:** Walked every id each escrow contract has issued (its id counter numbers every authorization and receipt, so this does not depend on how long the network keeps events) and read each receipt and the payer that authorized it. A v1 charge and each payout of a v2 settle count as one charge. A charge is excluded when it is a self-payment (the payer owns the agent, is the escrow's settler, or is a platform key) or settled before the sprint began on 2026-09-07. Testnet settles in native XLM, not USDC: the escrow's payment asset is the native XLM asset contract.
- **Counted:** 4; **excluded:** 1.
- **Proof:**
  - [PaymentEscrow v2 contract (live) (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CC4HZXMIK4L4OQQDQCPEQ4OUYOCHWUFCW3QAJDEENENKNJYYL6GP3NEX)
  - [PaymentEscrow v1 contract (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI)
  - [Charge of 0.114 XLM to house_agent on the v1 escrow — 2026-05-13 (excluded: self-payment (the payer owns the agent); settled before the sprint began on 2026-09-07)](https://stellar.expert/explorer/testnet/tx/8ed5b22e01e6e2d7bd996f0b92e42d5e5872fdea3d9cebb4c34025e22c9206db)
  - [Charge of 0.1 XLM to alpha on the v2 escrow — 2026-09-21 (counted)](https://stellar.expert/explorer/testnet/tx/eb6142a63cec25237fb684ae1519830811eed751e6e01378c84d3485ff72c6dc)
  - [Charge of 0.2 XLM to beta on the v2 escrow — 2026-09-22 (counted)](https://stellar.expert/explorer/testnet/tx/d2e1ae97b92f28113c71d63ea2250041330628ed1c2abec23e99c8e44e4b6664)
  - [Charge of 0.05 XLM to alpha on the v2 escrow — 2026-09-23 (counted)](https://stellar.expert/explorer/testnet/tx/f326ccf5f6c6b55742c6d9eea7d25fbe41be14e5cb73ef382e771805801237f7)
  - [Charge of 0.05 XLM to gamma on the v2 escrow — 2026-09-23 (counted)](https://stellar.expert/explorer/testnet/tx/f326ccf5f6c6b55742c6d9eea7d25fbe41be14e5cb73ef382e771805801237f7)

## m05. Dispute → partial-refund settlements

- **Target** ≥ 1; **achieved** 1; **met**.
- **How it was measured:** Read the dispute ratings the platform wrote to the ReputationLedger (from its keys' full transaction history) and traced each back, through the derived job id the backend records a dispute under, to the charge it disputes. A refund counts only when that charge counts and the platform then paid its payer back over the asset contract, no more than the charge. A transfer with no dispute behind it (such as a refund drill) does not count. The ledger's lifetime dispute count for every agent is read as a cross-check.
- **Counted:** 1; **excluded:** 1.
- **Proof:**
  - [ReputationLedger contract (every rating)](https://stellar.expert/explorer/testnet/contract/CD7BIAILJ7UDGA4FF4CGPSIZ56NHZIEJXEPJNY5K27KCNXMHA6JJOFNA)
  - [Transfer of 0.054 XLM from the team's contract admin and v1 escrow settler to the team's QA throwaway operator key — 2026-09-12 (excluded: no dispute behind it; paid to a team wallet)](https://stellar.expert/explorer/testnet/tx/0a39a9e3e363a057a4c710aa255a2c2a0763dcac25c6dbc2f23105db37e29400)
  - [Dispute rating on alpha — 2026-09-24 (counted: refunded)](https://stellar.expert/explorer/testnet/tx/7f09d30b394aae3241dfec188510a3f854984f75f9b6a50c1a2f054b67eb57a5)
  - [Refund of 0.05 XLM to the payer of the disputed charge — 2026-09-24 (counted)](https://stellar.expert/explorer/testnet/tx/ab32b558076428d8e1f35c8c258338f5ce667033048f2f702082c30eb923898f)

## m06. Permissionless AgentRegistry.register flow live on the dApp

- **Target** Yes; **achieved** Yes; **met**.
- **How it was measured:** Opened the /app/register page on the live dApp with no login, and checked the live backend publishes the route that builds an unsigned registration transaction for the owner's own wallet to sign. The registry contract's register needs only the owner's signature, no admin.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - [Register an agent page (opens with no login)](https://front.test/app/register)
  - [Live backend route list](https://backend.test/openapi.json)
  - [AgentRegistry contract (every registered agent)](https://stellar.expert/explorer/testnet/contract/CCDSJENDBVQNLGEWFXTOPOBUVN3LFKTF7OVRALDOXKVONLG4EOECECFN)
  - [Registration of gamma signed by an outside operator's wallet, not the registry admin — 2026-09-20](https://stellar.expert/explorer/testnet/tx/fab2f5a389ed60fd654935fcd8fa098ce3815ba66f48c9e6b74335eb0c758eba)

## m07. Reputation-gated routing (reads avg_bps, applies a floor) live

- **Target** Yes; **achieved** Yes; **met**.
- **How it was measured:** Read the live reputation settings the router applies (whether the floor is on, and its value) from the deployed API. The router scores each agent from the ReputationLedger's on-chain average and leaves out any agent below the floor.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - [Live reputation settings (floor 5500 bps)](https://orizon.test/api/stellar/reputation/params)
  - [ReputationLedger contract (every rating)](https://stellar.expert/explorer/testnet/contract/CD7BIAILJ7UDGA4FF4CGPSIZ56NHZIEJXEPJNY5K27KCNXMHA6JJOFNA)

## m08. Automated dispute window + partial-credit refund live

- **Target** Yes; **achieved** Yes; **met**.
- **How it was measured:** Checked that the live backend publishes the dispute routes (open, read, uphold, reject); that a dispute window can open at all, which needs a settled payment, and so the escrow v2 contract live; that its readiness report shows refunds switched on (disputes.reconcile.enabled, true only when refunds and the refund sweep are both on); and that at least one dispute has been refunded on-chain (the dispute refund row above). Yes when all hold, Partly when the routes are deployed but the rest does not, and No when the routes are not.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - [Live backend route list](https://backend.test/openapi.json)
  - [Live backend readiness report](https://backend.test/readiness)
  - [ReputationLedger contract (every rating)](https://stellar.expert/explorer/testnet/contract/CD7BIAILJ7UDGA4FF4CGPSIZ56NHZIEJXEPJNY5K27KCNXMHA6JJOFNA)

## m09. Public "List your agent on Orizon" integration guide published

- **Target** Yes; **achieved** Yes; **met**.
- **How it was measured:** Opened the /guide/list-your-agent page on the live dApp with no login; published means it answers there.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - ["List your agent on Orizon" guide](https://front.test/guide/list-your-agent)

## m10. 3–5 min demo video published

- **Target** Yes; **achieved** Yes; **met**.
- **How it was measured:** Opened the /demo page on the live dApp with no login and read the published marker the page renders (data-demo="published") and the video's running time, which must be 3 to 5 minutes.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - [Demo page](https://front.test/demo)

## m11. All source code released under MIT License

- **Target** Yes; **achieved** Yes; **met**.
- **How it was measured:** Asked GitHub which licence it detects on each repository: the frontend, backend and smart contracts the SOW names, and the example agent. Each must be MIT.
- **Counted:** 4; **excluded:** 0.
- **Proof:**
  - [Frontend repository — MIT licence detected](https://github.com/Bl0cksmiths/Orizon-Agents-FE-Stellar)
  - [Backend repository — MIT licence detected](https://github.com/Bl0cksmiths/Orizon-Agents-BE-Stellar)
  - [Smart contracts repository — MIT licence detected](https://github.com/Bl0cksmiths/Orizon-Agents-Smart-Contract-Stellar)
  - [Example agent repository — MIT licence detected](https://github.com/Bl0cksmiths/Orizon-Agents-Example-Agent-Stellar)

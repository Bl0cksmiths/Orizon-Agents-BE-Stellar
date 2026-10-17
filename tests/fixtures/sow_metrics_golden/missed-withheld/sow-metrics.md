# SOW §6.3 success metrics (story 5.05)

**0 of 11 met.** (exit 0)

Measured 2026-09-21T14:13:20Z on Stellar testnet, read-only: API `https://orizon.test`, backend `https://backend.test`, frontend `https://front.test`, RPC `https://rpc.test`, Horizon `https://horizon.test`, GitHub `https://github-api.test`.

Outside operators' agent ids, wallets and transaction hashes are held back until each operator's consent to publish them is recorded: a row that would link them links the Ecosystem page instead. The counts are unchanged.

| # | Category | Metric (SOW) | Target | Achieved | Status |
|---|---|---|---|---|---|
| m01 | Adoption targets | Externally-operated agents registered on Testnet | ≥ 2 | 1 | **Not met** |
| m02 | Adoption targets | Unique external operator wallet addresses | ≥ 2 | 1 | **Not met** |
| m03 | Transaction targets | Workflows routed to external agents & settled on Testnet | ≥ 3 | 1 | **Not met** |
| m04 | Transaction targets | On-chain USDC settlements (charges) recorded | ≥ 3 | 2 | **Not met** |
| m05 | Transaction targets | Dispute → partial-refund settlements | ≥ 1 | 0 | **Not met** |
| m06 | Technical milestones | Permissionless AgentRegistry.register flow live on the dApp | Yes | No | **Not met** |
| m07 | Technical milestones | Reputation-gated routing (reads avg_bps, applies a floor) live | Yes | No | **Not met** |
| m08 | Technical milestones | Automated dispute window + partial-credit refund live | Yes | Partly: the dispute routes are deployed, but refunds are not reported as switched on (the readiness report predates the refund switch), and no dispute has been refunded on-chain yet (see the dispute refund row). | **Not met** |
| m09 | Technical milestones | Public "List your agent on Orizon" integration guide published | Yes | No | **Not met** |
| m10 | Technical milestones | 3–5 min demo video published | Yes | No | **Not met** |
| m11 | Technical milestones | All source code released under MIT License | Yes | No | **Not met** |

## m01. Externally-operated agents registered on Testnet

- **Target** ≥ 2; **achieved** 1; **not met**.
- **Why not:** Only 1 agent is registered by outside operators; the other 2 belong to wallets the team controls. The target is 2.
- **How it was measured:** Read every agent the AgentRegistry contract lists, with its owner, straight from testnet. An agent counts only if its owner is neither one of the team's wallets (the committed register) nor a key the platform runs (network admin, dispatch signer, ratings signer and scorer, registry admin, escrow settler and admin, ledger scorer, attestation sealer).
- **Counted:** 1; **excluded:** 2.
- **Proof:**
  - [AgentRegistry contract (every registered agent)](https://stellar.expert/explorer/testnet/contract/CCDSJENDBVQNLGEWFXTOPOBUVN3LFKTF7OVRALDOXKVONLG4EOECECFN)
  - [Registration of house_agent by the team's contract admin and v1 escrow settler — 2026-04-20 (excluded: team wallet)](https://stellar.expert/explorer/testnet/tx/045ef594d81d2f2134d61151ed71260d8f79e657c7cb6ed1d893688532017409)
  - [Registration of qa_agent by the team's QA throwaway operator key — 2026-09-17 (excluded: team wallet)](https://stellar.expert/explorer/testnet/tx/eea1ad3fbf2142ede510d0220518d902a5ba9b502851530d7fc1454f5147206c)
  - [Outside operators' agents, on the Ecosystem page. Their agent ids, wallets and transaction hashes are held back until each operator's consent to publish them is recorded](https://front.test/app/ecosystem)

## m02. Unique external operator wallet addresses

- **Target** ≥ 2; **achieved** 1; **not met**.
- **Why not:** Only 1 outside operator wallet owns an agent. The target is 2.
- **How it was measured:** The distinct owner wallets of the registered agents, each checked the same way as the row above. A wallet counts only if it is neither one of the team's wallets (the committed register) nor a key the platform runs (network admin, dispatch signer, ratings signer and scorer, registry admin, escrow settler and admin, ledger scorer, attestation sealer).
- **Counted:** 1; **excluded:** 2.
- **Proof:**
  - [Outside operators' agents, on the Ecosystem page. Their agent ids, wallets and transaction hashes are held back until each operator's consent to publish them is recorded](https://front.test/app/ecosystem)
  - [The team's QA throwaway operator key — owns qa_agent (excluded: team wallet)](https://stellar.expert/explorer/testnet/account/GBNRRKOSAXFYIWGSEEYM34SCNNREKRB65GRLHMBHRNGMF3FDW3XTHCNZ)
  - [The team's contract admin and v1 escrow settler — owns house_agent (excluded: team wallet)](https://stellar.expert/explorer/testnet/account/GDPH6XWBZBHHNKJQWR3N6RHTCDCYKJ2KC6XUG4NCH7E46PHJWYMPO7NO)

## m03. Workflows routed to external agents & settled on Testnet

- **Target** ≥ 3; **achieved** 1; **not met**.
- **Why not:** Only 1 workflow paid to agents owned by outside operators has settled since the sprint began. The target is 3. The one agent owned by an outside operator could not be checked for a binding, per the live adoption report.
- **How it was measured:** Walked every id each escrow contract has issued (its id counter numbers every authorization and receipt, so this does not depend on how long the network keeps events) and read each receipt and the payer that authorized it. A v1 charge and each payout of a v2 settle count as one charge. A charge is excluded when it is a self-payment (the payer owns the agent, is the escrow's settler, or is a platform key) or settled before the sprint began on 2026-09-07. Testnet settles in native XLM, not USDC: the escrow's payment asset is the native XLM asset contract. A workflow counts when at least one of its counted charges paid an agent owned by an outside operator; several charges of one job are one workflow.
- **Counted:** 1; **excluded:** 4.
- **Proof:**
  - [PaymentEscrow v2 contract (live) (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CC4HZXMIK4L4OQQDQCPEQ4OUYOCHWUFCW3QAJDEENENKNJYYL6GP3NEX)
  - [PaymentEscrow v1 contract (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI)
  - [Charge of 0.114 XLM to house_agent on the v1 escrow — 2026-05-13 (excluded: self-payment (the payer owns the agent); settled before the sprint began on 2026-09-07; the agent is owned by the team's contract admin and v1 escrow settler)](https://stellar.expert/explorer/testnet/tx/05320dd888b1da6f0de8cbf6e50cf39572ef9678ffca974b5372c3dcbe5b6716)
  - [Charge of 0.03 XLM to qa_agent on the v1 escrow — 2026-09-15 (excluded: the agent is owned by the team's QA throwaway operator key)](https://stellar.expert/explorer/testnet/tx/1684297ae612416c19f3e86ebc48de53cb016a6eacfa8f2400bf108a4dd71a3e)
  - [Outside operators' agents, on the Ecosystem page. Their agent ids, wallets and transaction hashes are held back until each operator's consent to publish them is recorded](https://front.test/app/ecosystem)

## m04. On-chain USDC settlements (charges) recorded

- **Target** ≥ 3; **achieved** 2; **not met**.
- **Why not:** Only 2 charges of the 5 on record count; the rest: 2 were the agent's owner paying itself and 1 was settled before the sprint began (2026-05-13). The target is 3.
- **How it was measured:** Walked every id each escrow contract has issued (its id counter numbers every authorization and receipt, so this does not depend on how long the network keeps events) and read each receipt and the payer that authorized it. A v1 charge and each payout of a v2 settle count as one charge. A charge is excluded when it is a self-payment (the payer owns the agent, is the escrow's settler, or is a platform key) or settled before the sprint began on 2026-09-07. Testnet settles in native XLM, not USDC: the escrow's payment asset is the native XLM asset contract.
- **Counted:** 2; **excluded:** 3.
- **Proof:**
  - [PaymentEscrow v2 contract (live) (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CC4HZXMIK4L4OQQDQCPEQ4OUYOCHWUFCW3QAJDEENENKNJYYL6GP3NEX)
  - [PaymentEscrow v1 contract (every charge it recorded)](https://stellar.expert/explorer/testnet/contract/CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI)
  - [Charge of 0.114 XLM to house_agent on the v1 escrow — 2026-05-13 (excluded: self-payment (the payer owns the agent); settled before the sprint began on 2026-09-07)](https://stellar.expert/explorer/testnet/tx/05320dd888b1da6f0de8cbf6e50cf39572ef9678ffca974b5372c3dcbe5b6716)
  - [Charge of 0.03 XLM to qa_agent on the v1 escrow — 2026-09-15 (counted)](https://stellar.expert/explorer/testnet/tx/1684297ae612416c19f3e86ebc48de53cb016a6eacfa8f2400bf108a4dd71a3e)
  - [Outside operators' agents, on the Ecosystem page. Their agent ids, wallets and transaction hashes are held back until each operator's consent to publish them is recorded](https://front.test/app/ecosystem)

## m05. Dispute → partial-refund settlements

- **Target** ≥ 1; **achieved** 0; **not met**.
- **Why not:** No dispute has been refunded yet. 2 dispute ratings are on the ledger, but none counts: its job id traces back to no charge on record and the charge it disputes does not count (self-payment (the payer owns the agent)). The 2 transfers out of platform keys on record (2026-09-12 and 2026-09-26) went to accounts with no dispute behind them.
- **How it was measured:** Read the dispute ratings the platform wrote to the ReputationLedger (from its keys' full transaction history) and traced each back, through the derived job id the backend records a dispute under, to the charge it disputes. A refund counts only when that charge counts and the platform then paid its payer back over the asset contract, no more than the charge. A transfer with no dispute behind it (such as a refund drill) does not count. The ledger's lifetime dispute count for every agent is read as a cross-check.
- **Counted:** 0; **excluded:** 4.
- **Proof:**
  - [ReputationLedger contract (every rating)](https://stellar.expert/explorer/testnet/contract/CD7BIAILJ7UDGA4FF4CGPSIZ56NHZIEJXEPJNY5K27KCNXMHA6JJOFNA)
  - [Transfer of 0.054 XLM from the team's contract admin and v1 escrow settler to the team's QA throwaway operator key — 2026-09-12 (excluded: no dispute behind it; paid to a team wallet)](https://stellar.expert/explorer/testnet/tx/63c4b7e1837664fbc867287bc991a1eef6dc0604e1c260b33899df763b879059)
  - [Outside operators' agents, on the Ecosystem page. Their agent ids, wallets and transaction hashes are held back until each operator's consent to publish them is recorded](https://front.test/app/ecosystem)

## m06. Permissionless AgentRegistry.register flow live on the dApp

- **Target** Yes; **achieved** No; **not met**.
- **Why not:** The /app/register page redirects (to https://front.test/login) instead of answering.
- **How it was measured:** Opened the /app/register page on the live dApp with no login, and checked the live backend publishes the route that builds an unsigned registration transaction for the owner's own wallet to sign. The registry contract's register needs only the owner's signature, no admin.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - [Register an agent page (opens with no login)](https://front.test/app/register)
  - [Live backend route list](https://backend.test/openapi.json)
  - [AgentRegistry contract (every registered agent)](https://stellar.expert/explorer/testnet/contract/CCDSJENDBVQNLGEWFXTOPOBUVN3LFKTF7OVRALDOXKVONLG4EOECECFN)
  - [Outside operators' agents, on the Ecosystem page. Their agent ids, wallets and transaction hashes are held back until each operator's consent to publish them is recorded](https://front.test/app/ecosystem)

## m07. Reputation-gated routing (reads avg_bps, applies a floor) live

- **Target** Yes; **achieved** No; **not met**.
- **Why not:** The live reputation settings say reputation-gated routing is switched off.
- **How it was measured:** Read the live reputation settings the router applies (whether the floor is on, and its value) from the deployed API. The router scores each agent from the ReputationLedger's on-chain average and leaves out any agent below the floor.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - [Live reputation settings (floor 5500 bps)](https://orizon.test/api/stellar/reputation/params)
  - [ReputationLedger contract (every rating)](https://stellar.expert/explorer/testnet/contract/CD7BIAILJ7UDGA4FF4CGPSIZ56NHZIEJXEPJNY5K27KCNXMHA6JJOFNA)

## m08. Automated dispute window + partial-credit refund live

- **Target** Yes; **achieved** Partly: the dispute routes are deployed, but refunds are not reported as switched on (the readiness report predates the refund switch), and no dispute has been refunded on-chain yet (see the dispute refund row).; **not met**.
- **Why not:** Refunds are not reported as switched on (the readiness report predates the refund switch), and no dispute has been refunded on-chain yet (see the dispute refund row).
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
- **Why not:** The published video runs 400 seconds, outside the 3 to 5 minutes the SOW asks for.
- **How it was measured:** Opened the /demo page on the live dApp with no login and read the published marker the page renders (data-demo="published") and the video's running time, which must be 3 to 5 minutes.
- **Counted:** 1; **excluded:** 0.
- **Proof:**
  - [Demo page](https://front.test/demo)

## m11. All source code released under MIT License

- **Target** Yes; **achieved** No; **not met**.
- **Why not:** 3 of the 4 repositories have no MIT licence that GitHub recognises: the backend (licence Apache-2.0), the smart contracts (no licence detected) and the example agent (not found).
- **How it was measured:** Asked GitHub which licence it detects on each repository: the frontend, backend and smart contracts the SOW names, and the example agent. Each must be MIT.
- **Counted:** 1; **excluded:** 3.
- **Proof:**
  - [Frontend repository — MIT licence detected](https://github.com/Bl0cksmiths/Orizon-Agents-FE-Stellar)
  - [Backend repository — licence detected: Apache-2.0, not MIT](https://github.com/Bl0cksmiths/Orizon-Agents-BE-Stellar)
  - [Smart contracts repository — GitHub detects no licence](https://github.com/Bl0cksmiths/Orizon-Agents-Smart-Contract-Stellar)
  - [Example agent repository — not found on GitHub](https://github.com/Bl0cksmiths/Orizon-Agents-Example-Agent-Stellar)

# Lifecycle evidence (story 5.01)

Network: **testnet** · first row 2026-09-30T17:45:23Z · last row 2026-09-30T17:47:53Z

Generated from `lifecycle.jsonl` after every stage; the JSONL is the source of truth.
Every on-chain status below was read back from Soroban RPC or Horizon, never assumed.

## Transactions

| # | Run | Stage | Event | UTC | Tx | On-chain status | Contract | Agent | Buyer | Amount |
|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 20260930T174522Z-calculatorai | authorize | authorize | 2026-09-30T17:45:33Z | [63454933…612e9f](https://stellar.expert/explorer/testnet/tx/634549330a8188d28d32d6530e56ddb14298680de0d86d2568b91d3d5a612e9f) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | calculatorai | GB4K6YRHDHB2HHNM3E7UUZJU5JP3MSQE3GXKMEA5IT4AM45D23YKAYKK | 0.2100000 XLM (native) |
| 9 | 20260930T174522Z-calculatorai | poll | rating | 2026-09-30T17:47:48Z | [023103b2…04523a](https://stellar.expert/explorer/testnet/tx/023103b255895eb22ffbb93608d9ab333c37ce3434c2ad69c41a5c890504523a) | SUCCESS | CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT | calculatorai | GB4K6YRHDHB2HHNM3E7UUZJU5JP3MSQE3GXKMEA5IT4AM45D23YKAYKK |  |
| 10 | 20260930T174522Z-calculatorai | poll | rating | 2026-09-30T17:47:48Z | [fc9a8268…f7212e](https://stellar.expert/explorer/testnet/tx/fc9a8268b806831f863e70f9a8f103882baef87b8fd34b5b7dda95f7c5f7212e) | SUCCESS | CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT | keyboardai | GB4K6YRHDHB2HHNM3E7UUZJU5JP3MSQE3GXKMEA5IT4AM45D23YKAYKK |  |
| 12 | 20260930T174522Z-calculatorai | verify | settle | 2026-09-30T17:47:50Z | [0ada0708…dc556b](https://stellar.expert/explorer/testnet/tx/0ada07084b5aa1c196fb8e45b15d3712dcbefaf320a315a84e8cf2ab7adc556b) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | calculatorai | GB4K6YRHDHB2HHNM3E7UUZJU5JP3MSQE3GXKMEA5IT4AM45D23YKAYKK | 0.0100000 XLM (native) |
| 14 | 20260930T174522Z-calculatorai | verify | seal | 2026-09-30T17:47:52Z | [41a159ff…56fd64](https://stellar.expert/explorer/testnet/tx/41a159ffd7d96265dd4dd0863c0211697d94e77d636c9b9e198d8cf3cc56fd64) | SUCCESS | CBYUZKOET43UXTBXZUJIBBJW5ODGD2J2AZVVXCR3QONGOCAHOXQQHEGK | calculatorai | GB4K6YRHDHB2HHNM3E7UUZJU5JP3MSQE3GXKMEA5IT4AM45D23YKAYKK |  |

## Reputation snapshots

| # | Label | UTC | Agent | Score (bps) | Lower bound (bps) | Source | Count | Disputed | Stale |
|---|---|---|---|---|---|---|---|---|---|
| 2 | start | 2026-09-30T17:45:23Z | calculatorai | 6999 | 5678 | onchain | 3 | 1 | False |
| 7 | after_rating_1 | 2026-09-30T17:47:39Z | calculatorai | 7001 | 5680 | onchain | 4 | 1 | False |
| 11 | after_ratings | 2026-09-30T17:47:48Z | calculatorai | 7001 | 5680 | onchain | 4 | 1 | False |

## Every row

| # | Stage | Event | UTC | Summary |
|---|---|---|---|---|
| 1 | preflight | preflight | 2026-09-30T17:45:23Z | testnet confirmed by API and RPC; escrow v2; starting at 'decompose' |
| 2 | reputation | reputation_snapshot | 2026-09-30T17:45:23Z | start: score 6999 lower 5678 source onchain count 3 |
| 3 | decompose | plan | 2026-09-30T17:45:27Z | plan pln_2d696f62 routes ['calculatorai', 'keyboardai'], total 0.21 |
| 4 | decompose | balances_before | 2026-09-30T17:45:28Z | 2 balance(s) read before authorize |
| 5 | authorize | authorize | 2026-09-30T17:45:33Z | PaymentEscrow.authorize labelled pln_2d696f62; API said SUCCESS |
| 6 | execute | execute | 2026-09-30T17:45:33Z | task tsk_665f8b5c093e2b85 started |
| 7 | reputation | reputation_snapshot | 2026-09-30T17:47:39Z | after_rating_1: score 7001 lower 5680 source onchain count 4 |
| 8 | poll | task_terminal | 2026-09-30T17:47:47Z | task complete, spent 0.01 |
| 9 | poll | rating | 2026-09-30T17:47:48Z | ReputationLedger.submit for calculatorai: 95/100 |
| 10 | poll | rating | 2026-09-30T17:47:48Z | ReputationLedger.submit for keyboardai: 20/100 |
| 11 | reputation | reputation_snapshot | 2026-09-30T17:47:48Z | after_ratings: score 7001 lower 5680 source onchain count 4 |
| 12 | verify | settle | 2026-09-30T17:47:50Z | PaymentEscrow v2 settle for job fbc9b0e78d609571b2587a3c39c2de9c |
| 13 | verify | balances_after | 2026-09-30T17:47:52Z | 2 balance(s) read after settle |
| 14 | verify | seal | 2026-09-30T17:47:52Z | AttestationRegistry.seal for job fbc9b0e78d609571b2587a3c39c2de9c |
| 15 | verify | settlement_checks | 2026-09-30T17:47:53Z | escrow v2: all checks pass |

# Lifecycle evidence (story 5.01)

Network: **testnet** · first row 2026-09-30T09:33:41Z · last row 2026-09-30T09:35:50Z

Generated from `lifecycle.jsonl` after every stage; the JSONL is the source of truth.
Every on-chain status below was read back from Soroban RPC or Horizon, never assumed.

## Transactions

| # | Run | Stage | Event | UTC | Tx | On-chain status | Contract | Agent | Buyer | Amount |
|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 20260930T093341Z-faulty_test_v2 | authorize | authorize | 2026-09-30T09:33:53Z | [55f23322…2c8949](https://stellar.expert/explorer/testnet/tx/55f233224e00d96d4e56579fa8077f797c9c3f3c578638a80139eeb5c82c8949) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | faulty_test_v2 | GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV | 0.2000000 XLM (native) |
| 9 | 20260930T093341Z-faulty_test_v2 | poll | rating | 2026-09-30T09:35:50Z | [2980361e…1aa388](https://stellar.expert/explorer/testnet/tx/2980361e248b6a1284e17a2a7ec38d354991afc25f6f982270eb0710fa1aa388) | SUCCESS | CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT | faulty_test_v2 | GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV |  |

## Reputation snapshots

| # | Label | UTC | Agent | Score (bps) | Lower bound (bps) | Source | Count | Disputed | Stale |
|---|---|---|---|---|---|---|---|---|---|
| 2 | start | 2026-09-30T09:33:41Z | faulty_test_v2 | 6918 | 5596 | onchain | 1 | 0 | False |
| 7 | after_rating_1 | 2026-09-30T09:35:50Z | faulty_test_v2 | 6838 | 5518 | onchain | 2 | 0 | False |
| 10 | after_ratings | 2026-09-30T09:35:50Z | faulty_test_v2 | 6838 | 5518 | onchain | 2 | 0 | False |

## Every row

| # | Stage | Event | UTC | Summary |
|---|---|---|---|---|
| 1 | preflight | preflight | 2026-09-30T09:33:41Z | testnet confirmed by API and RPC; escrow v2; starting at 'decompose' |
| 2 | reputation | reputation_snapshot | 2026-09-30T09:33:41Z | start: score 6918 lower 5596 source onchain count 1 |
| 3 | decompose | plan | 2026-09-30T09:33:44Z | plan pln_47c35c47 routes ['faulty_test_v2'], total 0.2 |
| 4 | decompose | balances_before | 2026-09-30T09:33:46Z | 2 balance(s) read before authorize |
| 5 | authorize | authorize | 2026-09-30T09:33:53Z | PaymentEscrow.authorize labelled pln_47c35c47; API said SUCCESS |
| 6 | execute | execute | 2026-09-30T09:33:53Z | task tsk_5013cd198f7824cf started |
| 7 | reputation | reputation_snapshot | 2026-09-30T09:35:50Z | after_rating_1: score 6838 lower 5518 source onchain count 2 |
| 8 | poll | task_terminal | 2026-09-30T09:35:50Z | task failed, spent 0.0 |
| 9 | poll | rating | 2026-09-30T09:35:50Z | ReputationLedger.submit for faulty_test_v2: 20/100 |
| 10 | reputation | reputation_snapshot | 2026-09-30T09:35:50Z | after_ratings: score 6838 lower 5518 source onchain count 2 |

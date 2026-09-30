# Lifecycle evidence (story 5.01)

Network: **testnet** · first row 2026-09-30T09:36:12Z · last row 2026-09-30T09:38:22Z

Generated from `lifecycle.jsonl` after every stage; the JSONL is the source of truth.
Every on-chain status below was read back from Soroban RPC or Horizon, never assumed.

## Transactions

| # | Run | Stage | Event | UTC | Tx | On-chain status | Contract | Agent | Buyer | Amount |
|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 20260930T093612Z-faulty_test_v2 | authorize | authorize | 2026-09-30T09:36:24Z | [6af4f1c3…3b4463](https://stellar.expert/explorer/testnet/tx/6af4f1c3afd8ee50fa25e478897eab9a1563d743a3a112db26d0d560b03b4463) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | faulty_test_v2 | GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV | 0.2000000 XLM (native) |
| 9 | 20260930T093612Z-faulty_test_v2 | poll | rating | 2026-09-30T09:38:22Z | [cc83982b…f4e30f](https://stellar.expert/explorer/testnet/tx/cc83982bd11e39fe61f3446df7f9cfa4a30a741d77ab717abf204894bbf4e30f) | SUCCESS | CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT | faulty_test_v2 | GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV |  |

## Reputation snapshots

| # | Label | UTC | Agent | Score (bps) | Lower bound (bps) | Source | Count | Disputed | Stale |
|---|---|---|---|---|---|---|---|---|---|
| 2 | start | 2026-09-30T09:36:13Z | faulty_test_v2 | 6838 | 5518 | onchain | 2 | 0 | False |
| 7 | after_rating_1 | 2026-09-30T09:38:21Z | faulty_test_v2 | 6761 | 5443 | onchain | 3 | 0 | False |
| 10 | after_ratings | 2026-09-30T09:38:22Z | faulty_test_v2 | 6761 | 5443 | onchain | 3 | 0 | False |

## Every row

| # | Stage | Event | UTC | Summary |
|---|---|---|---|---|
| 1 | preflight | preflight | 2026-09-30T09:36:12Z | testnet confirmed by API and RPC; escrow v2; starting at 'decompose' |
| 2 | reputation | reputation_snapshot | 2026-09-30T09:36:13Z | start: score 6838 lower 5518 source onchain count 2 |
| 3 | decompose | plan | 2026-09-30T09:36:17Z | plan pln_26890650 routes ['faulty_test_v2'], total 0.2 |
| 4 | decompose | balances_before | 2026-09-30T09:36:18Z | 2 balance(s) read before authorize |
| 5 | authorize | authorize | 2026-09-30T09:36:24Z | PaymentEscrow.authorize labelled pln_26890650; API said SUCCESS |
| 6 | execute | execute | 2026-09-30T09:36:24Z | task tsk_e268dce4bac87210 started |
| 7 | reputation | reputation_snapshot | 2026-09-30T09:38:21Z | after_rating_1: score 6761 lower 5443 source onchain count 3 |
| 8 | poll | task_terminal | 2026-09-30T09:38:21Z | task failed, spent 0.0 |
| 9 | poll | rating | 2026-09-30T09:38:22Z | ReputationLedger.submit for faulty_test_v2: 20/100 |
| 10 | reputation | reputation_snapshot | 2026-09-30T09:38:22Z | after_ratings: score 6761 lower 5443 source onchain count 3 |

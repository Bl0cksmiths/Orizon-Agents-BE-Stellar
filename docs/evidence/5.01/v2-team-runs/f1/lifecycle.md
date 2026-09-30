# Lifecycle evidence (story 5.01)

Network: **testnet** · first row 2026-09-30T09:31:22Z · last row 2026-09-30T09:33:32Z

Generated from `lifecycle.jsonl` after every stage; the JSONL is the source of truth.
Every on-chain status below was read back from Soroban RPC or Horizon, never assumed.

## Transactions

| # | Run | Stage | Event | UTC | Tx | On-chain status | Contract | Agent | Buyer | Amount |
|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 20260930T093122Z-faulty_test_v2 | authorize | authorize | 2026-09-30T09:31:34Z | [f10d0f48…34befb](https://stellar.expert/explorer/testnet/tx/f10d0f48669d0f1de97d4ae5841f10c4fc27ab60747d1425ad77f16f3c34befb) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | faulty_test_v2 | GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV | 0.2000000 XLM (native) |
| 9 | 20260930T093122Z-faulty_test_v2 | poll | rating | 2026-09-30T09:33:32Z | [e7885bf1…0b9663](https://stellar.expert/explorer/testnet/tx/e7885bf192688ed4b65006abe82b5b5f58737b15fd06bfaa9c54de13fa0b9663) | SUCCESS | CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT | faulty_test_v2 | GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV |  |

## Reputation snapshots

| # | Label | UTC | Agent | Score (bps) | Lower bound (bps) | Source | Count | Disputed | Stale |
|---|---|---|---|---|---|---|---|---|---|
| 2 | start | 2026-09-30T09:31:23Z | faulty_test_v2 | 7000 | 5677 | prior | 0 | 0 | False |
| 7 | after_rating_1 | 2026-09-30T09:33:31Z | faulty_test_v2 | 6918 | 5596 | onchain | 1 | 0 | False |
| 10 | after_ratings | 2026-09-30T09:33:32Z | faulty_test_v2 | 6918 | 5596 | onchain | 1 | 0 | False |

## Every row

| # | Stage | Event | UTC | Summary |
|---|---|---|---|---|
| 1 | preflight | preflight | 2026-09-30T09:31:22Z | testnet confirmed by API and RPC; escrow v2; starting at 'decompose' |
| 2 | reputation | reputation_snapshot | 2026-09-30T09:31:23Z | start: score 7000 lower 5677 source prior count 0 |
| 3 | decompose | plan | 2026-09-30T09:31:26Z | plan pln_0d218118 routes ['faulty_test_v2'], total 0.2 |
| 4 | decompose | balances_before | 2026-09-30T09:31:27Z | 2 balance(s) read before authorize |
| 5 | authorize | authorize | 2026-09-30T09:31:34Z | PaymentEscrow.authorize labelled pln_0d218118; API said SUCCESS |
| 6 | execute | execute | 2026-09-30T09:31:34Z | task tsk_e6140c2754d42ed6 started |
| 7 | reputation | reputation_snapshot | 2026-09-30T09:33:31Z | after_rating_1: score 6918 lower 5596 source onchain count 1 |
| 8 | poll | task_terminal | 2026-09-30T09:33:31Z | task failed, spent 0.0 |
| 9 | poll | rating | 2026-09-30T09:33:32Z | ReputationLedger.submit for faulty_test_v2: 20/100 |
| 10 | reputation | reputation_snapshot | 2026-09-30T09:33:32Z | after_ratings: score 6918 lower 5596 source onchain count 1 |

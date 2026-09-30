# Lifecycle evidence (story 5.01)

Network: **testnet** · first row 2026-09-30T09:30:25Z · last row 2026-09-30T09:31:04Z

Generated from `lifecycle.jsonl` after every stage; the JSONL is the source of truth.
Every on-chain status below was read back from Soroban RPC or Horizon, never assumed.

## Transactions

| # | Run | Stage | Event | UTC | Tx | On-chain status | Contract | Agent | Buyer | Amount |
|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 20260930T093025Z-keyboardai | authorize | authorize | 2026-09-30T09:30:41Z | [b59d49e2…24cee1](https://stellar.expert/explorer/testnet/tx/b59d49e2ddb31fe9216c3da271897f184cb25f77203c62032f90864ba224cee1) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | keyboardai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP | 0.2000000 XLM (native) |
| 9 | 20260930T093025Z-keyboardai | poll | rating | 2026-09-30T09:31:00Z | [7ab2dd12…aee52c](https://stellar.expert/explorer/testnet/tx/7ab2dd12333c6d6819aa0045e240e6bf30deceb5fcb10645b5ba5df2e7aee52c) | SUCCESS | CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT | keyboardai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP |  |
| 11 | 20260930T093025Z-keyboardai | verify | settle | 2026-09-30T09:31:01Z | [19f3420d…a83397](https://stellar.expert/explorer/testnet/tx/19f3420ddb5232a8328c66ec57c1e34890d09a38350e172fdfd9ce8d04a83397) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | keyboardai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP | 0.2000000 XLM (native) |
| 13 | 20260930T093025Z-keyboardai | verify | seal | 2026-09-30T09:31:03Z | [a705d6a4…f68b02](https://stellar.expert/explorer/testnet/tx/a705d6a437469ac1783f372bf60279f5e90a143c4fcde9bcaad20a7f24f68b02) | SUCCESS | CBYUZKOET43UXTBXZUJIBBJW5ODGD2J2AZVVXCR3QONGOCAHOXQQHEGK | keyboardai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP |  |

## Reputation snapshots

| # | Label | UTC | Agent | Score (bps) | Lower bound (bps) | Source | Count | Disputed | Stale |
|---|---|---|---|---|---|---|---|---|---|
| 2 | start | 2026-09-30T09:30:26Z | keyboardai | 7000 | 5677 | prior | 0 | 0 | False |
| 7 | after_rating_1 | 2026-09-30T09:30:59Z | keyboardai | 7040 | 5733 | onchain | 1 | 0 | False |
| 10 | after_ratings | 2026-09-30T09:31:00Z | keyboardai | 7040 | 5733 | onchain | 1 | 0 | False |

## Every row

| # | Stage | Event | UTC | Summary |
|---|---|---|---|---|
| 1 | preflight | preflight | 2026-09-30T09:30:25Z | testnet confirmed by API and RPC; escrow v2; starting at 'decompose' |
| 2 | reputation | reputation_snapshot | 2026-09-30T09:30:26Z | start: score 7000 lower 5677 source prior count 0 |
| 3 | decompose | plan | 2026-09-30T09:30:28Z | plan pln_062d27df routes ['keyboardai'], total 0.2 |
| 4 | decompose | balances_before | 2026-09-30T09:30:30Z | 2 balance(s) read before authorize |
| 5 | authorize | authorize | 2026-09-30T09:30:41Z | PaymentEscrow.authorize labelled pln_062d27df; API said SUCCESS |
| 6 | execute | execute | 2026-09-30T09:30:41Z | task tsk_e5027c1b729ca555 started |
| 7 | reputation | reputation_snapshot | 2026-09-30T09:30:59Z | after_rating_1: score 7040 lower 5733 source onchain count 1 |
| 8 | poll | task_terminal | 2026-09-30T09:30:59Z | task complete, spent 0.2 |
| 9 | poll | rating | 2026-09-30T09:31:00Z | ReputationLedger.submit for keyboardai: 95/100 |
| 10 | reputation | reputation_snapshot | 2026-09-30T09:31:00Z | after_ratings: score 7040 lower 5733 source onchain count 1 |
| 11 | verify | settle | 2026-09-30T09:31:01Z | PaymentEscrow v2 settle for job 6dc04f8d08caf312dc1a422378468889 |
| 12 | verify | balances_after | 2026-09-30T09:31:03Z | 2 balance(s) read after settle |
| 13 | verify | seal | 2026-09-30T09:31:03Z | AttestationRegistry.seal for job 6dc04f8d08caf312dc1a422378468889 |
| 14 | verify | settlement_checks | 2026-09-30T09:31:04Z | escrow v2: all checks pass |

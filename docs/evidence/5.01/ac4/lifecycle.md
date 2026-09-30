# Lifecycle evidence (story 5.01)

Network: **testnet** · first row 2026-09-30T17:48:06Z · last row 2026-09-30T18:03:38Z

Generated from `lifecycle.jsonl` after every stage; the JSONL is the source of truth.
Every on-chain status below was read back from Soroban RPC or Horizon, never assumed.

## Transactions

| # | Run | Stage | Event | UTC | Tx | On-chain status | Contract | Agent | Buyer | Amount |
|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 20260930T174805Z-calculatorai | authorize | authorize | 2026-09-30T17:48:19Z | [c87a9215…8ba0f4](https://stellar.expert/explorer/testnet/tx/c87a92152ec0e4c1a04c20fd58d31913a22a76282a6e1619a9e9fc35f78ba0f4) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP | 0.0100000 XLM (native) |
| 9 | 20260930T174805Z-calculatorai | poll | rating | 2026-09-30T17:48:47Z | [a1c82164…431a6d](https://stellar.expert/explorer/testnet/tx/a1c821649c15cdaba43c4208358f11fdc431a1389e0bafefe0f3155477431a6d) | SUCCESS | CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP |  |
| 11 | 20260930T174805Z-calculatorai | verify | settle | 2026-09-30T17:48:48Z | [eb118e0b…d2a24b](https://stellar.expert/explorer/testnet/tx/eb118e0b679aa8f88c680cf6095b9718f014ecf67344d6393d5d1f2eb4d2a24b) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP | 0.0100000 XLM (native) |
| 13 | 20260930T174805Z-calculatorai | verify | seal | 2026-09-30T17:48:51Z | [77605170…f9503c](https://stellar.expert/explorer/testnet/tx/77605170243e5e7690a5ece510144363c7a1a57aeb77ebcea2ce0e0473f9503c) | SUCCESS | CBYUZKOET43UXTBXZUJIBBJW5ODGD2J2AZVVXCR3QONGOCAHOXQQHEGK | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP |  |
| 18 | 20260930T174805Z-calculatorai | verify | settle | 2026-09-30T18:03:02Z | [eb118e0b…d2a24b](https://stellar.expert/explorer/testnet/tx/eb118e0b679aa8f88c680cf6095b9718f014ecf67344d6393d5d1f2eb4d2a24b) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP | 0.0100000 XLM (native) |
| 20 | 20260930T174805Z-calculatorai | verify | seal | 2026-09-30T18:03:04Z | [77605170…f9503c](https://stellar.expert/explorer/testnet/tx/77605170243e5e7690a5ece510144363c7a1a57aeb77ebcea2ce0e0473f9503c) | SUCCESS | CBYUZKOET43UXTBXZUJIBBJW5ODGD2J2AZVVXCR3QONGOCAHOXQQHEGK | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP |  |
| 24 | 20260930T174805Z-calculatorai | refund | refund | 2026-09-30T18:03:36Z | [01c3175a…efa5be](https://stellar.expert/explorer/testnet/tx/01c3175a881658808e15dc9a284439f2fbce4091a3566bfe3894ecedc1efa5be) | SUCCESS | CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP | 0.0100000 XLM (native) |
| 25 | 20260930T174805Z-calculatorai | refund | dispute_rating | 2026-09-30T18:03:37Z | [60bc5249…ba730e](https://stellar.expert/explorer/testnet/tx/60bc5249af542ea005ea62573800b0bd48a101eeef6d748f1884f807c1ba730e) | SUCCESS | CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP |  |

## Reputation snapshots

| # | Label | UTC | Agent | Score (bps) | Lower bound (bps) | Source | Count | Disputed | Stale |
|---|---|---|---|---|---|---|---|---|---|
| 2 | start | 2026-09-30T17:48:07Z | calculatorai | 7001 | 5680 | onchain | 4 | 1 | False |
| 7 | after_rating_1 | 2026-09-30T17:48:47Z | calculatorai | 7003 | 5683 | onchain | 5 | 1 | False |
| 10 | after_ratings | 2026-09-30T17:48:47Z | calculatorai | 7003 | 5683 | onchain | 5 | 1 | False |
| 16 | resume_at_poll | 2026-09-30T18:02:59Z | calculatorai | 7003 | 5683 | onchain | 5 | 1 | False |
| 26 | after_dispute | 2026-09-30T18:03:38Z | calculatorai | 6998 | 5678 | onchain | 6 | 2 | False |
| 27 | final | 2026-09-30T18:03:38Z | calculatorai | 6998 | 5678 | onchain | 6 | 2 | False |

## Every row

| # | Stage | Event | UTC | Summary |
|---|---|---|---|---|
| 1 | preflight | preflight | 2026-09-30T17:48:06Z | testnet confirmed by API and RPC; escrow v2; starting at 'decompose' |
| 2 | reputation | reputation_snapshot | 2026-09-30T17:48:07Z | start: score 7001 lower 5680 source onchain count 4 |
| 3 | decompose | plan | 2026-09-30T17:48:12Z | plan pln_7bb6fbdb routes ['calculatorai'], total 0.01 |
| 4 | decompose | balances_before | 2026-09-30T17:48:13Z | 2 balance(s) read before authorize |
| 5 | authorize | authorize | 2026-09-30T17:48:19Z | PaymentEscrow.authorize labelled pln_7bb6fbdb; API said SUCCESS |
| 6 | execute | execute | 2026-09-30T17:48:20Z | task tsk_fcd544e62f0f0958 started |
| 7 | reputation | reputation_snapshot | 2026-09-30T17:48:47Z | after_rating_1: score 7003 lower 5683 source onchain count 5 |
| 8 | poll | task_terminal | 2026-09-30T17:48:47Z | task complete, spent 0.01 |
| 9 | poll | rating | 2026-09-30T17:48:47Z | ReputationLedger.submit for calculatorai: 95/100 |
| 10 | reputation | reputation_snapshot | 2026-09-30T17:48:47Z | after_ratings: score 7003 lower 5683 source onchain count 5 |
| 11 | verify | settle | 2026-09-30T17:48:48Z | PaymentEscrow v2 settle for job b263f1ebde6bebbde5f8b99b71e74f7c |
| 12 | verify | balances_after | 2026-09-30T17:48:51Z | 2 balance(s) read after settle |
| 13 | verify | seal | 2026-09-30T17:48:51Z | AttestationRegistry.seal for job b263f1ebde6bebbde5f8b99b71e74f7c |
| 14 | verify | settlement_checks | 2026-09-30T17:48:51Z | escrow v2: all checks pass |
| 15 | preflight | preflight | 2026-09-30T18:02:58Z | testnet confirmed by API and RPC; escrow v2; starting at 'poll' |
| 16 | reputation | reputation_snapshot | 2026-09-30T18:02:59Z | resume_at_poll: score 7003 lower 5683 source onchain count 5 |
| 17 | poll | task_not_in_memory | 2026-09-30T18:02:59Z | GET /api/tasks/{id} is 404: the backend restarted or evicted it |
| 18 | verify | settle | 2026-09-30T18:03:02Z | PaymentEscrow v2 settle for job b263f1ebde6bebbde5f8b99b71e74f7c |
| 19 | verify | balances_after | 2026-09-30T18:03:04Z | 2 balance(s) read after settle |
| 20 | verify | seal | 2026-09-30T18:03:04Z | AttestationRegistry.seal for job b263f1ebde6bebbde5f8b99b71e74f7c |
| 21 | verify | settlement_checks | 2026-09-30T18:03:04Z | escrow v2: all checks pass |
| 22 | dispute | dispute_opened | 2026-09-30T18:03:09Z | dispute dsp_d87167ccf384e41c7cc48b1e78c78e42 on step 0 (calculatorai), status open |
| 23 | uphold | upheld | 2026-09-30T18:03:36Z | uphold answered credited |
| 24 | refund | refund | 2026-09-30T18:03:36Z | settler → buyer credit for the upheld dispute (ADR 0002: platform-funded) |
| 25 | refund | dispute_rating | 2026-09-30T18:03:37Z | the dispute-kind rating on the ReputationLedger |
| 26 | reputation | reputation_snapshot | 2026-09-30T18:03:38Z | after_dispute: score 6998 lower 5678 source onchain count 6 |
| 27 | reputation | reputation_snapshot | 2026-09-30T18:03:38Z | final: score 6998 lower 5678 source onchain count 6 |
| 28 | reputation | reputation_summary | 2026-09-30T18:03:38Z | score moved True then True |

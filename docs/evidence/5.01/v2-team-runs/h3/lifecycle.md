# Lifecycle evidence (story 5.01)

Network: **testnet** · first row 2026-09-30T09:39:08Z · last row 2026-09-30T15:26:01Z

Generated from `lifecycle.jsonl` after every stage; the JSONL is the source of truth.
Every on-chain status below was read back from Soroban RPC or Horizon, never assumed.

## Transactions

| # | Run | Stage | Event | UTC | Tx | On-chain status | Contract | Agent | Buyer | Amount |
|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 20260930T093907Z-calculatorai | authorize | authorize | 2026-09-30T09:39:18Z | [9f9e99c2…eac655](https://stellar.expert/explorer/testnet/tx/9f9e99c2aadcbf3b06cfe4738012dcc302fcee9358f092cfab4dc7f5caeac655) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP | 0.0100000 XLM (native) |
| 9 | 20260930T093907Z-calculatorai | poll | rating | 2026-09-30T09:39:42Z | [adb7592e…50d40e](https://stellar.expert/explorer/testnet/tx/adb7592eed1f403b39765da94ab9eff204b0fea33d7da52e5fe6a33c9750d40e) | SUCCESS | CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP |  |
| 11 | 20260930T093907Z-calculatorai | verify | settle | 2026-09-30T09:39:43Z | [785428bf…ca554b](https://stellar.expert/explorer/testnet/tx/785428bf6552208750b375703556c534da557dccd64df8d1db7f954a04ca554b) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP | 0.0100000 XLM (native) |
| 13 | 20260930T093907Z-calculatorai | verify | seal | 2026-09-30T09:39:46Z | [efca274f…37c0a8](https://stellar.expert/explorer/testnet/tx/efca274fb83b50865cfc20dc40b6949e5abd1ae23ed3e7a7622e6c9eda37c0a8) | SUCCESS | CBYUZKOET43UXTBXZUJIBBJW5ODGD2J2AZVVXCR3QONGOCAHOXQQHEGK | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP |  |
| 19 | 20260930T093907Z-calculatorai | refund | refund | 2026-09-30T15:26:00Z | [cb2c5792…e1e25f](https://stellar.expert/explorer/testnet/tx/cb2c57929006470f9f554989dd8071e8539d245df529df956693944a78e1e25f) | SUCCESS | CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP | 0.0100000 XLM (native) |
| 20 | 20260930T093907Z-calculatorai | refund | dispute_rating | 2026-09-30T15:26:00Z | [b512135f…453a49](https://stellar.expert/explorer/testnet/tx/b512135ffade2d6518fd8cf1628f20787846ed0e311750043b87723dee453a49) | SUCCESS | CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT | calculatorai | GCNQAJE6K7LORS7CQTI7RVJTB2TZG5QADTNFDKS5H7QQKRFTRFNLA2GP |  |

## Reputation snapshots

| # | Label | UTC | Agent | Score (bps) | Lower bound (bps) | Source | Count | Disputed | Stale |
|---|---|---|---|---|---|---|---|---|---|
| 2 | start | 2026-09-30T09:39:08Z | calculatorai | 7002 | 5680 | onchain | 1 | 0 | False |
| 7 | after_rating_1 | 2026-09-30T09:39:41Z | calculatorai | 7004 | 5683 | onchain | 2 | 0 | False |
| 10 | after_ratings | 2026-09-30T09:39:42Z | calculatorai | 7004 | 5683 | onchain | 2 | 0 | False |
| 17 | resume_at_uphold | 2026-09-30T15:25:43Z | calculatorai | 7004 | 5683 | onchain | 2 | 0 | False |
| 21 | after_dispute | 2026-09-30T15:26:01Z | calculatorai | 6999 | 5678 | onchain | 3 | 1 | False |
| 22 | final | 2026-09-30T15:26:01Z | calculatorai | 6999 | 5678 | onchain | 3 | 1 | False |

## Every row

| # | Stage | Event | UTC | Summary |
|---|---|---|---|---|
| 1 | preflight | preflight | 2026-09-30T09:39:08Z | testnet confirmed by API and RPC; escrow v2; starting at 'decompose' |
| 2 | reputation | reputation_snapshot | 2026-09-30T09:39:08Z | start: score 7002 lower 5680 source onchain count 1 |
| 3 | decompose | plan | 2026-09-30T09:39:11Z | plan pln_8cf34db2 routes ['calculatorai'], total 0.01 |
| 4 | decompose | balances_before | 2026-09-30T09:39:12Z | 2 balance(s) read before authorize |
| 5 | authorize | authorize | 2026-09-30T09:39:18Z | PaymentEscrow.authorize labelled pln_8cf34db2; API said SUCCESS |
| 6 | execute | execute | 2026-09-30T09:39:18Z | task tsk_7e1c369cebaf41b3 started |
| 7 | reputation | reputation_snapshot | 2026-09-30T09:39:41Z | after_rating_1: score 7004 lower 5683 source onchain count 2 |
| 8 | poll | task_terminal | 2026-09-30T09:39:41Z | task complete, spent 0.01 |
| 9 | poll | rating | 2026-09-30T09:39:42Z | ReputationLedger.submit for calculatorai: 95/100 |
| 10 | reputation | reputation_snapshot | 2026-09-30T09:39:42Z | after_ratings: score 7004 lower 5683 source onchain count 2 |
| 11 | verify | settle | 2026-09-30T09:39:43Z | PaymentEscrow v2 settle for job dd9089ab7791c4293baf87745d1ea0b6 |
| 12 | verify | balances_after | 2026-09-30T09:39:46Z | 2 balance(s) read after settle |
| 13 | verify | seal | 2026-09-30T09:39:46Z | AttestationRegistry.seal for job dd9089ab7791c4293baf87745d1ea0b6 |
| 14 | verify | settlement_checks | 2026-09-30T09:39:46Z | escrow v2: all checks pass |
| 15 | dispute | dispute_opened | 2026-09-30T09:39:50Z | dispute dsp_15acee279ac02852a5877ac1696ec4b5 on step 0 (calculatorai), status open |
| 16 | preflight | preflight | 2026-09-30T15:25:41Z | testnet confirmed by API and RPC; escrow v2; starting at 'uphold' |
| 17 | reputation | reputation_snapshot | 2026-09-30T15:25:43Z | resume_at_uphold: score 7004 lower 5683 source onchain count 2 |
| 18 | uphold | upheld | 2026-09-30T15:25:59Z | uphold answered credited |
| 19 | refund | refund | 2026-09-30T15:26:00Z | settler → buyer credit for the upheld dispute (ADR 0002: platform-funded) |
| 20 | refund | dispute_rating | 2026-09-30T15:26:00Z | the dispute-kind rating on the ReputationLedger |
| 21 | reputation | reputation_snapshot | 2026-09-30T15:26:01Z | after_dispute: score 6999 lower 5678 source onchain count 3 |
| 22 | reputation | reputation_snapshot | 2026-09-30T15:26:01Z | final: score 6999 lower 5678 source onchain count 3 |
| 23 | reputation | reputation_summary | 2026-09-30T15:26:01Z | score moved True then True |

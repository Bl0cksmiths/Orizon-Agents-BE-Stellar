# Lifecycle evidence (story 5.01)

Network: **testnet** · first row 2026-09-30T09:29:02Z · last row 2026-09-30T09:30:01Z

Generated from `lifecycle.jsonl` after every stage; the JSONL is the source of truth.
Every on-chain status below was read back from Soroban RPC or Horizon, never assumed.

## Transactions

| # | Run | Stage | Event | UTC | Tx | On-chain status | Contract | Agent | Buyer | Amount |
|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 20260930T092901Z-calculatorai | authorize | authorize | 2026-09-30T09:29:24Z | [36fae769…672fc7](https://stellar.expert/explorer/testnet/tx/36fae7698f6c9f5fe1152458d41eb650353725be877d476846c308e10a672fc7) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | calculatorai | GB4K6YRHDHB2HHNM3E7UUZJU5JP3MSQE3GXKMEA5IT4AM45D23YKAYKK | 0.0100000 XLM (native) |
| 9 | 20260930T092901Z-calculatorai | poll | rating | 2026-09-30T09:29:56Z | [3aafc504…3501f9](https://stellar.expert/explorer/testnet/tx/3aafc504ff0b3943ffa1c92ded576991dcfac888df59ab6164027d7ff43501f9) | SUCCESS | CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT | calculatorai | GB4K6YRHDHB2HHNM3E7UUZJU5JP3MSQE3GXKMEA5IT4AM45D23YKAYKK |  |
| 11 | 20260930T092901Z-calculatorai | verify | settle | 2026-09-30T09:29:58Z | [f0674419…8d1235](https://stellar.expert/explorer/testnet/tx/f0674419992bdf30cf730139e54e4cdd985e32b43ee15c91733e08424a8d1235) | SUCCESS | CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4 | calculatorai | GB4K6YRHDHB2HHNM3E7UUZJU5JP3MSQE3GXKMEA5IT4AM45D23YKAYKK | 0.0100000 XLM (native) |
| 13 | 20260930T092901Z-calculatorai | verify | seal | 2026-09-30T09:30:01Z | [f0b25fc5…7e2b5c](https://stellar.expert/explorer/testnet/tx/f0b25fc59ee3c0d3e85cd9d3c92c3d18211411a1c578f8bb2bb58ea59a7e2b5c) | SUCCESS | CBYUZKOET43UXTBXZUJIBBJW5ODGD2J2AZVVXCR3QONGOCAHOXQQHEGK | calculatorai | GB4K6YRHDHB2HHNM3E7UUZJU5JP3MSQE3GXKMEA5IT4AM45D23YKAYKK |  |

## Reputation snapshots

| # | Label | UTC | Agent | Score (bps) | Lower bound (bps) | Source | Count | Disputed | Stale |
|---|---|---|---|---|---|---|---|---|---|
| 2 | start | 2026-09-30T09:29:02Z | calculatorai | 7000 | 5677 | prior | 0 | 0 | False |
| 7 | after_rating_1 | 2026-09-30T09:29:56Z | calculatorai | 7002 | 5680 | onchain | 1 | 0 | False |
| 10 | after_ratings | 2026-09-30T09:29:56Z | calculatorai | 7002 | 5680 | onchain | 1 | 0 | False |

## Every row

| # | Stage | Event | UTC | Summary |
|---|---|---|---|---|
| 1 | preflight | preflight | 2026-09-30T09:29:02Z | testnet confirmed by API and RPC; escrow v2; starting at 'decompose' |
| 2 | reputation | reputation_snapshot | 2026-09-30T09:29:02Z | start: score 7000 lower 5677 source prior count 0 |
| 3 | decompose | plan | 2026-09-30T09:29:12Z | plan pln_77084d21 routes ['calculatorai'], total 0.01 |
| 4 | decompose | balances_before | 2026-09-30T09:29:14Z | 2 balance(s) read before authorize |
| 5 | authorize | authorize | 2026-09-30T09:29:24Z | PaymentEscrow.authorize labelled pln_77084d21; API said SUCCESS |
| 6 | execute | execute | 2026-09-30T09:29:25Z | task tsk_f84a532c1ba0f426 started |
| 7 | reputation | reputation_snapshot | 2026-09-30T09:29:56Z | after_rating_1: score 7002 lower 5680 source onchain count 1 |
| 8 | poll | task_terminal | 2026-09-30T09:29:56Z | task complete, spent 0.01 |
| 9 | poll | rating | 2026-09-30T09:29:56Z | ReputationLedger.submit for calculatorai: 95/100 |
| 10 | reputation | reputation_snapshot | 2026-09-30T09:29:56Z | after_ratings: score 7002 lower 5680 source onchain count 1 |
| 11 | verify | settle | 2026-09-30T09:29:58Z | PaymentEscrow v2 settle for job 83ca44226d0c2ad807e2f76c065d7b7b |
| 12 | verify | balances_after | 2026-09-30T09:30:00Z | 2 balance(s) read after settle |
| 13 | verify | seal | 2026-09-30T09:30:01Z | AttestationRegistry.seal for job 83ca44226d0c2ad807e2f76c065d7b7b |
| 14 | verify | settlement_checks | 2026-09-30T09:30:01Z | escrow v2: all checks pass |

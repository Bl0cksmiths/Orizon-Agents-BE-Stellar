# Evidence sheet — Orizon Agents story 5.01 AC4 and AC5 team runs (Stellar testnet)

Generated 2026-09-30T18:04:09Z from `docs/evidence/5.01/ac5/lifecycle.jsonl`, `docs/evidence/5.01/ac4/lifecycle.jsonl`. Network: **testnet**.

**11 of 11 transaction(s) re-verified SUCCESS** on the ledger just now (Soroban RPC `getTransaction`, falling back to Horizon). Only those are in `evidence.json` and `description.txt`.

Skipped 0 unreadable line(s) and 2 repeated hash(es) in the input.

## Transactions, in the order the video shows them

| # | Stage | What it proves | Deliverable | Tx hash | Stellar Expert | Recorded | Re-verified |
|---|---|---|---|---|---|---|---|
| 1 | authorize · Buyer authorization — calculatorai (0.2100000 XLM (native)) | the buyer's wallet authorized the plan's spend on PaymentEscrow | D4 | `634549330a8188d28d32d6530e56ddb14298680de0d86d2568b91d3d5a612e9f` | [open](https://stellar.expert/explorer/testnet/tx/634549330a8188d28d32d6530e56ddb14298680de0d86d2568b91d3d5a612e9f) | SUCCESS | SUCCESS (rpc) |
| 2 | poll · Reputation rating — calculatorai | ReputationLedger recorded the run's rating, which the routing floor reads | D2 | `023103b255895eb22ffbb93608d9ab333c37ce3434c2ad69c41a5c890504523a` | [open](https://stellar.expert/explorer/testnet/tx/023103b255895eb22ffbb93608d9ab333c37ce3434c2ad69c41a5c890504523a) | SUCCESS | SUCCESS (rpc) |
| 3 | poll · Reputation rating — keyboardai | ReputationLedger recorded the run's rating, which the routing floor reads | D2 | `fc9a8268b806831f863e70f9a8f103882baef87b8fd34b5b7dda95f7c5f7212e` | [open](https://stellar.expert/explorer/testnet/tx/fc9a8268b806831f863e70f9a8f103882baef87b8fd34b5b7dda95f7c5f7212e) | SUCCESS | SUCCESS (rpc) |
| 4 | verify · Settlement — calculatorai (0.0100000 XLM (native)) | PaymentEscrow paid the agent's owner for the delivered step | D4 | `0ada07084b5aa1c196fb8e45b15d3712dcbefaf320a315a84e8cf2ab7adc556b` | [open](https://stellar.expert/explorer/testnet/tx/0ada07084b5aa1c196fb8e45b15d3712dcbefaf320a315a84e8cf2ab7adc556b) | SUCCESS | SUCCESS (rpc) |
| 5 | verify · Attestation seal — calculatorai | AttestationRegistry sealed the job, tying the payout to the work | D4 | `41a159ffd7d96265dd4dd0863c0211697d94e77d636c9b9e198d8cf3cc56fd64` | [open](https://stellar.expert/explorer/testnet/tx/41a159ffd7d96265dd4dd0863c0211697d94e77d636c9b9e198d8cf3cc56fd64) | SUCCESS | SUCCESS (rpc) |
| 6 | authorize · Buyer authorization — calculatorai (0.0100000 XLM (native)) | the buyer's wallet authorized the plan's spend on PaymentEscrow | D4 | `c87a92152ec0e4c1a04c20fd58d31913a22a76282a6e1619a9e9fc35f78ba0f4` | [open](https://stellar.expert/explorer/testnet/tx/c87a92152ec0e4c1a04c20fd58d31913a22a76282a6e1619a9e9fc35f78ba0f4) | SUCCESS | SUCCESS (rpc) |
| 7 | poll · Reputation rating — calculatorai | ReputationLedger recorded the run's rating, which the routing floor reads | D2 | `a1c821649c15cdaba43c4208358f11fdc431a1389e0bafefe0f3155477431a6d` | [open](https://stellar.expert/explorer/testnet/tx/a1c821649c15cdaba43c4208358f11fdc431a1389e0bafefe0f3155477431a6d) | SUCCESS | SUCCESS (rpc) |
| 8 | verify · Settlement — calculatorai (0.0100000 XLM (native)) | PaymentEscrow paid the agent's owner for the delivered step | D4 | `eb118e0b679aa8f88c680cf6095b9718f014ecf67344d6393d5d1f2eb4d2a24b` | [open](https://stellar.expert/explorer/testnet/tx/eb118e0b679aa8f88c680cf6095b9718f014ecf67344d6393d5d1f2eb4d2a24b) | SUCCESS | SUCCESS (rpc) |
| 9 | verify · Attestation seal — calculatorai | AttestationRegistry sealed the job, tying the payout to the work | D4 | `77605170243e5e7690a5ece510144363c7a1a57aeb77ebcea2ce0e0473f9503c` | [open](https://stellar.expert/explorer/testnet/tx/77605170243e5e7690a5ece510144363c7a1a57aeb77ebcea2ce0e0473f9503c) | SUCCESS | SUCCESS (rpc) |
| 10 | refund · Partial-credit refund — calculatorai (0.0100000 XLM (native)) | the platform's signing key paid the buyer a partial credit for the disputed step | D3 | `01c3175a881658808e15dc9a284439f2fbce4091a3566bfe3894ecedc1efa5be` | [open](https://stellar.expert/explorer/testnet/tx/01c3175a881658808e15dc9a284439f2fbce4091a3566bfe3894ecedc1efa5be) | SUCCESS | SUCCESS (rpc) |
| 11 | refund · Dispute rating — calculatorai | a kind=dispute rating against the disputed agent landed on the ReputationLedger | D3 | `60bc5249af542ea005ea62573800b0bd48a101eeef6d748f1884f807c1ba730e` | [open](https://stellar.expert/explorer/testnet/tx/60bc5249af542ea005ea62573800b0bd48a101eeef6d748f1884f807c1ba730e) | SUCCESS | SUCCESS (rpc) |

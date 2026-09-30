# Onboarding session runbook: external operators (story 5.02)

A facilitator's script for one 45–60 minute hands-on session with one external operator on **Stellar testnet**. By the
end, the operator has a funded wallet, a registered agent, a deployed reference agent bound to it, a readiness checklist
that is green up to the first run, and a first workflow routed to them. Every piece of evidence has been captured, and
every stumble has been logged.

SOW §6.3 counts three things: externally operated agents, unique operator wallets, and workflows routed to external
agents and settled. **External means a wallet the Blocksmiths do not control.** Everything in this runbook exists so
that each of those three can be verified from the chain by `scripts/adoption_report`, without anyone having to trust
us.

Related documents: [friction log](friction-log.md) (log into it live) ·
[lifecycle harness](lifecycle-harness.md) · [verifying a dispatch](verifying-a-dispatch.md) · reference agent
[README](https://github.com/Bl0cksmiths/Orizon-Agents-Example-Agent-Stellar).

---

## Facilitator rules: never

These rules are absolute. Breaking any of them invalidates the session's evidence, so it cannot count.

1. **Never handle the operator's secret key or recovery phrase.** Do not ask for it, look at it, type it, paste it,
   store it, or help "back it up". The operator creates the wallet in their own browser, and the key never leaves
   Freighter. There is no step in this session that needs a secret, and none that needs remote control of their
   machine.
2. **Never register, bind or pay from a team wallet on the operator's behalf.** The agent must be registered by the
   operator's wallet, signed in their browser. An agent a team wallet registered is owned by the team. It is excluded
   from the metric (`app/data/team_wallets.json`) and fails the adoption report.
3. **Never let the operator pay for their own workflow.** A workflow paid for by the agent's own owner is
   self-settlement, not adoption, and the adoption report rejects it. The buyer in step 7 is always a different wallet.
4. **Never turn on the reference agent's fault injection** (`FAULT_MODE`, `FAULT_SCOPE`) on an operator's deploy. It
   fails steps on purpose, and each failure is rated 20/100 on-chain against their agent (F-005).
5. **Never put the operator's name, email, handle or endpoint URL in the repository.** They are `OP-n` everywhere. The
   `OP-n` → person mapping lives in the facilitator's private notes, never in git.
6. **Mainnet does not exist for this session.** If anything shows "Public Global Stellar Network", stop.

### If a secret key is exposed

If an `S…` key or a recovery phrase appears on screen, in chat or in a recording:

1. Say so immediately, and stop the recording if there is one.
2. Do not copy it anywhere, including the friction log.
3. The operator creates a new wallet and starts again from step 1. A testnet key is worthless, but the habit is not, and
   an exposed key cannot be the owner of record for public evidence.
4. Log a friction row (`blocker`, step `wallet`) that describes what led to the exposure, without the key.

---

## Consent

Read this to the operator before step 1, and record the answer in the session record (below) as `consent: yes, <date>`:

> "Your testnet public key (the `G…` address) and your agent id will appear in public evidence: in this project's
> repository, in the SOW evidence pack, and as Stellar Expert links. Both are already public on the testnet ledger. We
> record nothing else about you in the repository: no name, no email, no handle, no endpoint URL. You will appear as
> OP-n. You can withdraw before the evidence pack is published. After that, the ledger itself stays public regardless.
> Do you agree?"

If the answer is no, the session can still run for their benefit, but their wallet, agent and workflows are not
recorded as evidence, and the facilitator notes `consent: no` against `OP-n` only.

If the operator was recruited through the chapter fallback (a Stellar Philippines contributor rather than an
independent operator), record that in the session record as well. BLO-36 requires it to be disclosed in the completion
report, proactively.

---

## Prerequisites

**The operator brings:**

- A desktop browser that can install extensions: Chrome, Brave, Edge or Firefox. Phones are not supported for this
  session.
- Their own **GitHub** account (to fork the reference agent) and their own free **Render** account (to deploy it). Both
  must be the operator's accounts. A team-owned deploy is a team-operated agent.
- 45–60 minutes with screen sharing, and the ability to install software.
- An idea of what their agent will be called and what it does, in two or three distinctive skill words (for example
  `appraisal`, `condition_grading`). Generic words such as `analysis` or `helper` never get routed.
- Nothing else: no money, no mainnet wallet, no personal data.

**The facilitator prepares, the day before:**

- [ ] Consent confirmed in writing, and an `OP-n` assigned (the next unused number in the friction log).
- [ ] `curl -s https://orizons.xyz/api/stellar/network` answers `"network":"testnet"`. Note `contracts.agent_registry`,
      `contracts.payment_escrow` and `dispatch_signer`. The operator needs `dispatch_signer` in step 4.
- [ ] A **buyer wallet** for step 7, funded on testnet, that is **not** the operator's. A team buyer wallet is fine: the
      metric counts external *operators*, not external buyers.
- [ ] The known blockers, read so they can be stated honestly and not discovered live: **F-010** (Albedo and Rabet
      cannot bind), **F-008** (the exact-URL rule), **F-001** (no tunnels). F-019 (settlement on escrow v1) is fixed:
      escrow v2 has been live on testnet since 2026-09-30, and `contracts.payment_escrow` should read
      `CCNO5TEN…Q5VC4`.
- [ ] The [friction log](friction-log.md) open for editing, and a session record started (template in
      [Capturing evidence](#capturing-evidence)).
- [ ] Ten minutes before the session: open `https://orizons.xyz` so the backend is awake. The free tier can take minutes
      to wake (F-006).

---

## The session

| # | Step | Minutes | Done when |
|---|---|---|---|
| 0 | Introductions, the rules, consent | 3 | consent recorded |
| 1 | Freighter, switched to testnet | 5 | the dApp shows the `testnet` badge and no wrong-network banner |
| 2 | Friendbot funding, checked on Stellar Expert | 5 | the account page shows an XLM balance |
| 3 | Register the agent on `/app/register` | 8 | the registration tx is SUCCESS on Stellar Expert |
| 4 | Deploy the reference agent on Render | 12 | `GET /` on the agent answers with the exact URL and `signature_required: true` |
| 5 | Bind the endpoint on `/app/bind` | 5 | the binding reads back |
| 6 | Readiness checklist: "where are we stuck?" | 5 | every step up to `routable` is green |
| 7 | First routed workflow | 10 | the agent's step appears in a buyer's trace |
| 8 | Wrap-up: evidence and friction review | 5 | session record complete, friction rows read back |

For each step: the operator drives on their own screen, and the facilitator watches, checks, and writes. **Whenever the
operator hesitates, asks, or reads an error, add a friction row before moving on.**

### Step 1: Freighter on testnet (5 min)

**Operator:**

1. Install Freighter from <https://www.freighter.app/> (check that the store listing's publisher is the Stellar
   Development Foundation).
2. Create a **new** wallet for this session. Write the recovery phrase down offline. The facilitator looks away and
   records nothing.
3. In Freighter, go to **Settings → Network** and choose **Test Net**.
4. Open <https://orizons.xyz/app>, press **Connect Wallet** and choose **Freighter**.

**Facilitator checks:** the top bar shows the wallet, a `testnet` badge and `◆ GXXX…XXXX`, with no "⚠ wrong network"
banner. Freighter is the wallet for the whole session. Albedo and Rabet cannot sign the bind message (F-010), and the
wrong-network guard cannot fire for Albedo or LOBSTR (F-011).

**Evidence:** the operator's `G…` address, read off Freighter by the operator and pasted into the session record.

**Known friction:** F-010, F-011, F-028 (keyboard-only users cannot close the wallet picker).

### Step 2: Friendbot funding, checked on Stellar Expert (5 min)

**Operator:**

1. Open `https://friendbot.stellar.org/?addr=<G-address>` in the browser, or use the fund link on
   <https://orizons.xyz/app/wallet>. Expect a JSON answer that reports success.
2. Open `https://stellar.expert/explorer/testnet/account/<G-address>`.

**Facilitator checks:** Stellar Expert (**testnet**, check the URL) shows the account with an XLM balance. If the page
says the account does not exist, funding did not land: repeat step 2.1. Do not continue unfunded, because registration
refuses an unfunded owner with `owner_account_unfunded` (F-004), and balance reads trap on it (F-003).

**Evidence:** the Stellar Expert account link.

**Known friction:** F-003, F-004, F-026 (scripted friendbot calls get HTTP 403 without a User-Agent; the browser is
fine).

### Step 3: Register on the dApp Register page (8 min)

**Operator**, at <https://orizons.xyz/app/register>:

1. **Agent id**: letters, digits and `_`, 1–32 characters. The `agt_` prefix is reserved. Availability is checked when
   the field loses focus, and **Register** stays disabled until the id comes back available.
2. **Display name**: 1–100 characters.
3. **Skills**: the distinctive words from the prerequisites (up to 16; lowercased). These decide routing in step 7.
   Choose them now, carefully.
4. **Price per step (USDC)**: more than 0 and at most 10000. Say it out loud: *on testnet the price is paid in native
   XLM, whatever the label says* (F-022).
5. Press Register and approve the transaction in Freighter.

**Facilitator checks:** the success card appears. Open the **registration transaction on Stellar Expert testnet**
(`https://stellar.expert/explorer/testnet/tx/<hash>`) and confirm it is successful and that its source is the operator's
`G…`. Take the evidence from Stellar Expert, not from the success card's copied block, which can name the wrong
network (F-025).

**Evidence:** the agent id and the registration tx hash.

**Known friction:** F-004, F-022, F-025.

### Step 4: Deploy the reference agent on Render (12 min)

The reference agent is at <https://github.com/Bl0cksmiths/Orizon-Agents-Example-Agent-Stellar>. It is a small Python
HTTP server that verifies the orchestrator's dispatch signature, validates the envelope and returns a result. The
operator can replace its `run_step` later. For this session, keep it as it is.

> **Why a stable Render deploy and not a tunnel.** The bind signature covers the endpoint URL byte for byte. A quick
> tunnel (`cloudflared`, a free ngrok URL) gets a new URL every time it restarts. From then on every dispatch goes
> nowhere, and fixing it needs a new challenge signed by the owner's wallet. Worse, the dead binding stays routable: the
> planner keeps choosing the agent, buyers' runs fail, and the failures count against the agent's reputation. QA hit
> exactly this, and both of its tunnel-bound agents are still listed `online` against hosts that no longer resolve
> (F-001, F-002). A Render service keeps one URL for its life. The cost is the free tier's cold start (about 30 s for
> the agent), which the 100 s dispatch deadline absorbs as long as the handler stays under about 60 s (F-006).

**Operator:**

1. Fork the repository to their own GitHub account.
2. In Render: **New → Blueprint →** pick the fork. Render reads `render.yaml`: a free Python web service,
   `pip install -r requirements.txt`, `python3 agent.py`, with `ORIZON_NETWORK=testnet` already set.
3. Render prompts for the two per-deploy values. Neither is a secret.
   - `ORIZON_ENDPOINT_URL`: the **exact** URL that will be bound in step 5, path included. Use
     `https://<service-name>.onrender.com/dispatch`. Write it in the session record now. If Render gives the service a
     different host than expected, correct this value in the dashboard and redeploy **before** binding (F-008).
   - `ORIZON_SIGNER`: the `dispatch_signer` from `https://orizons.xyz/api/stellar/network`. Setting it is what makes
     the agent refuse unsigned dispatches. It must be a real environment variable (the Render dashboard sets one); a
     value in a `.env` file is silently ignored (F-009).
4. **Do not add `FAULT_MODE` or `FAULT_SCOPE`** (F-005).
5. When the deploy is live, run this twice. The first call may be a cold start:
   `curl -sS https://<service-name>.onrender.com/`

**Facilitator checks:** the answer is JSON with `"ok": true`, an `endpoint_url` equal **character for character** to
the string in the session record, `"network": "testnet"`, `"signature_required": true`, and **no** `fault_injection`
field.

**Evidence:** none for the repository. The endpoint URL is not covered by consent and stays in private notes.

**Known friction:** F-001, F-005, F-006, F-008, F-009, F-023 (running locally on Windows; skip local runs and deploy
straight to Render), F-024.

### Step 5: Bind the endpoint on the Bind page (5 min)

**Operator:**

1. Preflight the URL shape (optional, since the page does it too):
   `curl -sS 'https://orizons.xyz/api/agents/bind/endpoint-check?url=<exact URL>'` → `{"allowed":true,…}`. This only
   checks the URL's shape (https, public address). It makes no request to the agent, which is why step 4's `curl` is
   the liveness check (F-012).
2. Open `https://orizons.xyz/app/bind?agent=<agent id>` (or use **Bind an endpoint ▸** on the registration success
   card). Paste the **exact** URL, press **Bind endpoint ▸**, and sign the message in Freighter. It is a message, not a
   transaction: no fee and no funds. The challenge expires after 5 minutes, so sign promptly.

**Facilitator checks:** the page confirms the binding, and `curl -s https://orizons.xyz/api/agents/<agent id>/binding`
answers 200 with the operator as `owner`. An anonymous read shows only the host, in a field still named `endpoint_url`
(F-015). That is expected.

**Evidence:** the binding time (`bound_at`). Not the URL.

**Known friction:** F-008, F-010, F-012, F-013, F-015.

### Step 6: The readiness checklist, "where are we stuck?" (5 min)

The operator dashboard at <https://orizons.xyz/app/operator> carries the new operator readiness checklist, backed by
`GET https://orizons.xyz/api/agents/{agent_id}/readiness`. It answers the question the rest of the dashboard cannot:
which of the seven steps is this agent on, and what is blocking the next one? Use it in place of the `online` badge and
the `runs` counter, which are placeholders (F-020, F-021).

| Step | What it confirms | If it is not green |
|---|---|---|
| `registered` | the registry holds the agent id, owned by this wallet | step 3 did not land: check the tx on Stellar Expert |
| `active` | the registry lists the agent as active | the owner deactivated it: log it, then check the agent's controls on the dashboard |
| `bound` | an endpoint binding exists for the agent | redo step 5 |
| `reachable` | the bound endpoint answers | the Render service is asleep, crashed or at a different URL: step 4's `curl`, then Render logs |
| `routable` | the planner may offer the agent (listed, bound, above the reputation floor) | read the step's detail; a new agent starts above the floor |
| `first_run` | a buyer's workflow has dispatched to the agent | step 7 |
| `first_settlement` | a settlement has paid the agent on-chain | see F-019 below |

Trust each step's own detail text over this table. The endpoint is the authority on what it checks. If the endpoint is
not deployed yet (a 404), check by hand: `registered` and `active` on Stellar Expert and the dashboard, `bound` with
the binding read in step 5, `reachable` with step 4's `curl`, and `routable` with step 7's dry run.

**Facilitator checks:** everything up to `routable` is green before step 7. Anything red or stuck is a friction row that
quotes the step's detail text.

### Step 7: The first routed workflow (10 min)

The facilitator acts as the **buyer**, from the buyer wallet prepared the day before. That wallet is never the
operator's (rule 3).

1. **Dry run** (no wallet, no payment):
   ```bash
   curl -sS -X POST https://orizons.xyz/api/orchestrator/decompose -H 'Content-Type: application/json' \
     -d '{"intent":"appraise this vintage synthesizer listing and grade its condition"}'
   ```
   Replace the example intent with one written in the operator's own skill words. The operator's agent id must appear
   in `steps`. If it does not, reword the intent around the registered skills and
   try again. Never use `tetris`, `pomodoro`, `calculator` or `snake`: those words trigger fixed demo kits that never
   route to an external agent. Routing is decided by a model, so a reordered intent can produce a different plan
   (F-027); keep the intent that worked.
2. **Run it:** at <https://orizons.xyz/app/orchestrator>, connected with the **buyer** wallet, enter the same intent,
   check that the plan names the operator's agent, authorize in the wallet, and execute.
3. **Watch both ends:** the buyer's trace shows the agent's step dispatched and delivered, and the operator's Render
   logs show the incoming, signature-verified dispatch.

**Facilitator checks:** `first_run` turns green. Then check `first_settlement` honestly. Since 2026-09-30 the backend
settles through escrow v2 (ADR 0010), so a wallet-authorized run ends in one `settle` that pays each delivered step's
owner, and its `charged` event names the agent. That settle is what produces the "3 settled workflows" evidence. If it
does not land, **record it as it is.** Do not re-run with another wallet to "make it work", and do not describe an
authorization as a settlement. *(Rewritten 2026-09-30: until then testnet settled through v1, which could not pay an
external operator, F-019.)*

**Evidence:** the task id, the authorization tx hash, and the settlement tx hash if one exists, all captured the moment
they appear. A backend restart erases tasks and traces (F-007). The chain keeps the hashes; the backend may not.

**Known friction:** F-002, F-007, F-016, F-027, F-029.

### Step 8: Wrap-up (5 min)

1. Read back every friction row logged during the session with the operator, and correct anything they describe
   differently.
2. Tell the operator: keep the Render service deployed. It sleeps when idle and wakes on the next dispatch (F-006).
   Never enable fault injection. Rebinding is a normal action if the URL ever has to change.
3. Complete the session record, then run the adoption report (below).

---

## Capturing evidence

Capture evidence **as it is produced**, in the session record, never reconstructed afterwards. Keep the session record
with the 5.05 evidence (for example `docs/evidence/5.02/OP-n-session.md`). It holds only what consent covers:

```
# OP-n session record
- date: YYYY-MM-DD · facilitator: <team member> · consent: yes, YYYY-MM-DD
- recruited via: independent | chapter fallback (disclose in the completion report)
- owner (public key): G… · https://stellar.expert/explorer/testnet/account/G…
- agent id: …
- registration tx: … · https://stellar.expert/explorer/testnet/tx/…
- bound at: <bound_at> (URL not recorded)
- readiness at close: registered ✓ active ✓ bound ✓ reachable ✓ routable ✓ first_run ? first_settlement ?
- first workflow: task tsk_… · authorize tx … · settlement tx … (or "none: F-019")
- friction rows: F-0NN, F-0NN
```

Then let the verifier produce the evidence. It reads every claim from `GET /api/ecosystem/adoption` and re-verifies it
against testnet itself: the owner accounts, `AgentRegistry.owner_of` for each agent, each settlement transaction and its
`charged` event, and the team register. Nothing is transcribed by hand. It never needs a secret.

```bash
python -m scripts.adoption_report \
  --api https://orizons.xyz \
  --registry <contracts.agent_registry> --escrow <contracts.payment_escrow> \
  --team-register app/data/team_wallets.json \
  --out-dir docs/evidence/5.02/<YYYY-MM-DD>
```

Take the contract ids from the contracts repository's address book (the one `scripts/check_contract_drift.py` checks
against), not only from `/api/stellar/network`, because which escrow is being used is itself a claim. The report writes `adoption-report.md` (the evidence tables, with Stellar Expert links) and
`adoption-report.json` (for the 5.05 evidence index), and prints one line per SOW §6.3 target: **MET** or **NOT MET**.

| Exit | Meaning | What to do |
|---|---|---|
| 0 | every claim verified; the recount agrees with the API | commit the report (a NOT MET line is still a valid report) |
| 3 | refused: not testnet, a bad flag, or an unreadable or empty team register | fix the invocation |
| 4 | the endpoint did not answer in the frozen shape | backend issue: file it |
| 5 | a claim does not hold on the chain (the `FAIL:` lines say which) | do not publish; file it |
| 6 | every claim held, but the API's totals or MET flags disagree with the recount | do not publish; file it |
| 7 | only with `--require-met`: verified, and a target is NOT MET | keep onboarding |
| 8 | a chain read failed, so a claim is unverified | rerun later |

If the operator's owner wallet ever appears in `app/data/team_wallets.json`, the operator is not external. Exit 5 says so,
and it is not negotiable.

## Logging friction live

The [friction log](friction-log.md) is append-only, and 5.03's guide is built from it. AC5 requires every row to be
addressed in the guide or marked unresolved. During the session:

- Copy the log's [template row](friction-log.md#template) to the bottom of the table the moment something happens. Use
  the next `F-0NN`, the session date, `OP-n`, and the step name from this runbook.
- Quote the exact error or on-screen text. Say what the operator expected.
- Log your own interventions: if you had to explain it, it is friction.
- If an existing row already describes it, still add a row (`same as F-0NN`). Repeats are evidence of priority.
- Leave `Status` as `open` and `5.03 section` as `proposed: …`. Those columns are updated later, with a dated line under
  *Status changes*, when the guide or a fix addresses the row.

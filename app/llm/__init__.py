"""The model layer: every Claude and jev call the orchestrator makes goes through here.

    tiers     Tier, and the model and effort each tier runs on
    claude    structured() and text(): one Claude call, checked, priced and recorded
    jev       ask(): one jev System One call (the intent guard's classifier)
    spend     the per-model price table and the daily spend ledger behind the cap
    provider  the Claude/OpenAI switch and what /readiness says about it
    errors    the typed failures every caller maps to an answer
    testing   FakeClaude / FakeJev and the fixtures that keep tests off the network

Neither SDK is imported until the first real call, so none of this costs the
cold boot anything (tests/test_llm_contract.py holds that).
"""

# Disputes: the window, the proof, and what an upheld dispute actually pays

This explains what recourse a buyer has after a workflow has been paid for,
what it costs the agent that was disputed, and who pays for it.

Three audiences. A **buyer** who paid for a workflow and did not get what they
paid for wants "The window", "Who may dispute" and "What an upheld dispute
pays". An **operator** whose agent has been disputed wants "What can be
disputed", "Who pays" and "A dispute does not move reputation" — the short
version is that nothing is ever taken from you. A **reviewer** checking the
claim against the code wants "The trust model" and "The API", and should read
`docs/decisions/0007-dispute-window.md` alongside them.

## The window

**When a paid workflow settles, its buyer has 24 hours to dispute any step of
it.** The clock starts at settlement — the moment the charge and the
attestation land on-chain — and the exact closing time is fixed then and
recorded with the settlement.

That closing time does not move afterwards. The window length is a
configuration value (`DISPUTE_WINDOW_SECONDS`, shipped at 86 400 seconds), but
it is read **once, at settlement**, and stamped onto the settlement record;
every later check compares against the stamp. Changing the setting governs
workflows that settle after the change and nothing that already happened. A
buyer who was told a deadline keeps that deadline, and an operator whose window
has closed does not find it reopened.

A dispute raised after the window has closed is refused, and the refusal says
**when** it closed rather than only that it did. A window whose length is
discovered on rejection is not recourse.

Nothing expires the other way: the window closing does not delete the dispute
record, the settlement record, or an open dispute raised before it closed.

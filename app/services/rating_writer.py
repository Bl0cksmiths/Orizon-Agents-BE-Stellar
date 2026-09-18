"""Can this deployment write ratings? The rating writer's verdict.

`ReputationLedger.submit` accepts a rating only when `caller == Scorer`, the
address the ledger keeps in its instance storage — set by its constructor and
changed only by the admin's `set_scorer`. The backend signs every rating with
STELLAR_SIGNING_KEY, and only on wallet-authorized runs: a simulated run never
rates, by design. So three things have to line up before one rating lands —
reputation on with a ledger configured, a signing key, and that key being the
ledger's Scorer. The first two are config. The third exists only on the chain.

Each used to fail in silence. `_submit_ratings` returned without a word when
the config was incomplete; a key that was not the Scorer reverted every submit
with `Unauthorized` while /readiness reported `signer: configured`; and a
failed submit's trace line carried no reason. The testnet ledger sat at zero
ratings and nobody could say which of the three it was without Render's logs,
which the free tier loses on every restart.

This module gives the question a closed set of answers:

  disabled     REPUTATION_ENABLED is off, or STELLAR_REPUTATION_LEDGER is unset
  no_signer    no usable STELLAR_SIGNING_KEY
  scorer       the signer IS the ledger's stored Scorer — ratings can land
  not_scorer   it is not, or the ledger stores none — every submit will revert
  unchecked    the stored Scorer could not be read — never guessed

and says it in three places: one line at boot, `ratings` on /readiness, and a
reason on every rating a paid run fails to write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from ..config import settings

logger = logging.getLogger(__name__)

WriterStatus = Literal["disabled", "no_signer", "scorer", "not_scorer", "unchecked"]


@dataclass(frozen=True)
class ConfigGap:
    """A setting whose absence stops this deployment writing any rating."""

    status: WriterStatus  # "disabled" or "no_signer"
    # For operators: names the setting and what is wrong with it.
    problem: str
    # For the task trace, which the buyer reads: names no setting or value.
    reason: str


_REPUTATION_OFF = ConfigGap("disabled", "REPUTATION_ENABLED is false", "reputation is disabled")
_NO_LEDGER = ConfigGap("disabled", "STELLAR_REPUTATION_LEDGER is unset", "no reputation ledger is configured")
_NO_KEY = ConfigGap("no_signer", "STELLAR_SIGNING_KEY is unset", "no signing key is configured")


def config_gap() -> ConfigGap | None:
    """The first missing setting that stops ratings, or None when all are set.

    Presence only, in the order an operator would fix them — and exactly the
    gate `_submit_ratings` applies, so the startup line, /readiness and a
    paid run's trace name the same setting for the same deployment.
    """
    if not settings.reputation_enabled:
        return _REPUTATION_OFF
    if not settings.stellar_reputation_ledger:
        return _NO_LEDGER
    if not settings.stellar_signing_key:
        return _NO_KEY
    return None

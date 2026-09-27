"""Real testnet answers about a real refund, for the reconcile sweep's tests.

Captured READ-ONLY on 2026-09-28 — nothing was signed or submitted to get them:

  - the envelope from Horizon (`GET /transactions/<hash>`), which keeps full
    history, of the refund ADR 0002 records as the story 4.01 proof: the
    settler crediting 0.054 USDC to a buyer over the asset SAC, ledger
    4635132, `successful: true`;
  - the Soroban RPC's `getTransaction` answer for the SAME hash, taken the
    same day. It is NOT_FOUND. The transfer landed; the RPC keeps about seven
    days of history (`oldestLedger` 4780107, well after 4635132) and has
    forgotten it. That is the history gap the sweep must never read as "never
    landed", captured rather than imagined.
"""

from __future__ import annotations

from typing import Any

REAL_REFUND_HASH = "9b8ffaa44b2b966e4c3f1ab581f4203a30d282901ba3b231a578e46d8f919a68"
REAL_REFUND_ENVELOPE_XDR = (
    "AAAAAgAAAAA+BHZgSTINfR73hySLjKJwElqYa3Nn0/HTmg+jMKu9HgAAXBAAH9nKAAAAYAAAAAEAAAAAAAAAAAAAAABqpQOnAAAA"
    "AAAAAAEAAAAAAAAAGAAAAAAAAAAB15KLcsJwPM/q9+uf9O9NUEpVqLl5/JtFDqLIQrTRzmEAAAAIdHJhbnNmZXIAAAADAAAAEgAA"
    "AAAAAAAAPgR2YEkyDX0e94cki4yicBJamGtzZ9Px05oPozCrvR4AAAASAAAAAAAAAABRpG7LY/UPL143xBVF7+2T42zGwk9gLcn4"
    "/EDEy+uyAAAAAAoAAAAAAAAAAAAAAAAACD1gAAAAAQAAAAAAAAAAAAAAAdeSi3LCcDzP6vfrn/TvTVBKVai5efybRQ6iyEK00c5h"
    "AAAACHRyYW5zZmVyAAAAAwAAABIAAAAAAAAAAD4EdmBJMg19HveHJIuMonASWphrc2fT8dOaD6Mwq70eAAAAEgAAAAAAAAAAUaRu"
    "y2P1Dy9eN8QVRe/tk+NsxsJPYC3J+PxAxMvrsgAAAAAKAAAAAAAAAAAAAAAAAAg9YAAAAAAAAAABAAAAAAAAAAEAAAAGAAAAAdeS"
    "i3LCcDzP6vfrn/TvTVBKVai5efybRQ6iyEK00c5hAAAAFAAAAAEAAAACAAAAAAAAAAA+BHZgSTINfR73hySLjKJwElqYa3Nn0/HT"
    "mg+jMKu9HgAAAAAAAAAAUaRuy2P1Dy9eN8QVRe/tk+NsxsJPYC3J+PxAxMvrsgAAA14LAAABIAAAASAAAAAAAABbrAAAAAEwq70e"
    "AAAAQD6gp7+3zWxFg8w7Swca9wTTDY/kADoe9UpJYx6iHizsBMeNplmBaLywWt/VaTXijpe7zGSJEV4LhuZUOfeWOQ4="
)
REAL_SETTLER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
REAL_PAYER = "GBI2I3WLMP2Q6L26G7CBKRPP5WJ6G3GGYJHWALOJ7D6EBRGL5OZAADBH"
REAL_ASSET_SAC = "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC"
REAL_AMOUNT_STROOPS = 540_000
REAL_LEDGER = 4_635_132
# The envelope's own time bound: built with `set_timeout(30)`.
REAL_MAX_TIME = 1_789_199_271

# The RPC's answer, verbatim — close times arrive as strings.
REAL_NOT_FOUND_ANSWER: dict[str, Any] = {
    "latestLedger": 4901066,
    "latestLedgerCloseTime": "1790528917",
    "oldestLedger": 4780107,
    "oldestLedgerCloseTime": "1789924122",
    "status": "NOT_FOUND",
    "txHash": REAL_REFUND_HASH,
    "applicationOrder": 0,
    "feeBump": False,
    "events": {},
    "ledger": 0,
    "createdAt": "0",
}

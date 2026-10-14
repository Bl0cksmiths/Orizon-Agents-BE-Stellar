"""
Minimal RFC 6238 TOTP generator (stdlib only).

Used to answer a PDAX SOFTWARE_TOKEN_MFA challenge automatically when
`PDAX_OTP_SECRET` (the base32 seed) is configured. No third-party dependency.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct

from .errors import PdaxError


def totp_now(secret_base32: str, *, timestamp: int, period: int = 30, digits: int = 6) -> str:
    """Compute the TOTP code for `timestamp` (seconds). Caller passes the time
    explicitly so the value is deterministic and testable."""
    try:
        key = base64.b32decode(_pad(secret_base32.strip().replace(" ", "").upper()))
    except (binascii.Error, ValueError) as e:
        # A mistyped PDAX_OTP_SECRET must not surface as a 500 on the first
        # MFA login: it is a failed PDAX authentication like any other, and
        # the routers only translate PdaxError.
        raise PdaxError("PDAX_OTP_SECRET is not valid base32", code="InvalidMfaCode") from e
    counter = int(timestamp // period)
    msg = struct.pack(">Q", counter)
    # HMAC-SHA1 is the TOTP algorithm, not a weak-hash choice: RFC 6238 §1.2
    # builds TOTP on HOTP's HMAC-SHA-1 and §4 defines the code over it, and
    # PDAX's SOFTWARE_TOKEN_MFA challenge (an authenticator-app TOTP) verifies
    # exactly that variant — another hash yields codes PDAX rejects. HMAC's
    # strength does not rest on SHA-1's collision resistance (RFC 4226 App. B.2),
    # so the known SHA-1 collisions do not weaken a TOTP code.
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code_int).zfill(digits)


def _pad(secret: str) -> str:
    """Base32 requires the input length to be a multiple of 8."""
    remainder = len(secret) % 8
    return secret + ("=" * (8 - remainder)) if remainder else secret

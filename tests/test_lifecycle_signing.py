"""The lifecycle harness's buyer signing (story 5.01): it signs only what it asked for."""

from __future__ import annotations

import base64

import pytest
from stellar_sdk import Account, Keypair, TransactionBuilder, TransactionEnvelope, scval
from stellar_sdk.exceptions import BadSignatureError

from scripts.lifecycle.config import TESTNET_PASSPHRASE
from scripts.lifecycle.redact import Redactor
from scripts.lifecycle.signing import (
    AuthorizeCall,
    SigningRefused,
    auth_id_from_return_value,
    load_api_key,
    load_keypair,
    sign_authorize,
    sign_message_b64,
    usdc_to_stroops,
)

ESCROW = "CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI"
OTHER = "CAPHXWU53UZUZJGV7IAE57NNMH3YYB5MTWO6YA53KKMXSFVLOITBJ3GQ"


def _envelope(
    source: str,
    *,
    contract: str = ESCROW,
    function: str = "authorize",
    payer: str | None = None,
    label: str = "pln_0a1b2c3d",
    stroops: int = 700_000,
    passphrase: str = TESTNET_PASSPHRASE,
    ops: int = 1,
) -> str:
    builder = TransactionBuilder(Account(source, 1), network_passphrase=passphrase, base_fee=100)
    for _ in range(ops):
        builder.append_invoke_contract_function_op(
            contract,
            function,
            [
                scval.to_address(payer or source),
                scval.to_symbol(label),
                scval.to_int128(stroops),
                scval.to_uint64(1_900_000_000),
            ],
        )
    return builder.set_timeout(30).build().to_xdr()


def _expect(kp: Keypair) -> AuthorizeCall:
    return AuthorizeCall(escrow=ESCROW, payer=kp.public_key, agent_id="pln_0a1b2c3d", max_stroops=700_000)


def test_signs_the_authorize_it_asked_for() -> None:
    kp = Keypair.random()
    signed = sign_authorize(_envelope(kp.public_key), kp, TESTNET_PASSPHRASE, _expect(kp))
    env = TransactionEnvelope.from_xdr(signed.signed_xdr, TESTNET_PASSPHRASE)
    kp.verify(env.hash(), env.signatures[0].signature)
    assert signed.tx_hash == env.hash_hex()
    assert signed.expires_at == 1_900_000_000


@pytest.mark.parametrize(
    ("change", "complaint"),
    [
        ({"contract": OTHER}, "is not the escrow"),
        ({"function": "charge"}, "is not 'authorize'"),
        ({"label": "orizon_batch"}, "agent label"),
        ({"stroops": 7_000_000}, "max_amount"),
        ({"payer": Keypair.random().public_key}, "payer"),
        ({"ops": 2}, "2 operation(s)"),
    ],
)
def test_refuses_an_envelope_that_is_not_the_call(change: dict[str, object], complaint: str) -> None:
    kp = Keypair.random()
    with pytest.raises(SigningRefused, match="refusing to sign|operation"):
        try:
            sign_authorize(_envelope(kp.public_key, **change), kp, TESTNET_PASSPHRASE, _expect(kp))  # type: ignore[arg-type]
        except SigningRefused as exc:
            assert complaint in str(exc)
            raise


def test_refuses_an_envelope_from_another_source_account() -> None:
    kp = Keypair.random()
    with pytest.raises(SigningRefused, match="is not the buyer"):
        sign_authorize(_envelope(Keypair.random().public_key, payer=kp.public_key), kp, TESTNET_PASSPHRASE, _expect(kp))


def test_an_envelope_built_for_another_network_does_not_verify_here() -> None:
    kp = Keypair.random()
    xdr = _envelope(kp.public_key, passphrase="Public Global Stellar Network ; September 2015")
    signed = sign_authorize(xdr, kp, TESTNET_PASSPHRASE, _expect(kp))
    # signed under the TESTNET passphrase, so a mainnet node would reject it
    main_env = TransactionEnvelope.from_xdr(signed.signed_xdr, "Public Global Stellar Network ; September 2015")
    with pytest.raises(BadSignatureError):
        kp.verify(main_env.hash(), main_env.signatures[0].signature)


def test_sep53_message_signature() -> None:
    kp = Keypair.random()
    sig = base64.b64decode(sign_message_b64(kp, "orizon-dispute:v1:ab:0:cd"))
    kp.verify_message("orizon-dispute:v1:ab:0:cd", sig)


def test_a_missing_or_bad_secret_never_echoes_the_value() -> None:
    r = Redactor()
    with pytest.raises(SigningRefused, match=r"\$BUYER is not set"):
        load_keypair("BUYER", r, {})
    with pytest.raises(SigningRefused) as exc:
        load_keypair("BUYER", r, {"BUYER": "not-a-seed-but-secret"})
    assert "not-a-seed-but-secret" not in str(exc.value)
    assert r.scrub("not-a-seed-but-secret") == "[redacted]"


def test_loaded_secrets_are_registered_with_the_redactor() -> None:
    kp, r = Keypair.random(), Redactor()
    assert load_keypair("B", r, {"B": kp.secret}).public_key == kp.public_key
    assert load_api_key("K", r, {"K": "operator-key-123456"}) == "operator-key-123456"
    assert r.scrub(f"{kp.secret} operator-key-123456") == "[redacted] [redacted]"
    with pytest.raises(SigningRefused):
        load_api_key("K", r, {"K": "  "})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00112233445566778899AABBCCDDEEFF", "00112233445566778899aabbccddeeff"),
        (base64.b64encode(bytes(range(16))).decode(), bytes(range(16)).hex()),
        (list(range(16)), bytes(range(16)).hex()),
        ("abc", None),
        (list(range(15)), None),
        ([300] * 16, None),
        (None, None),
    ],
)
def test_auth_id_is_read_like_the_console_reads_it(value: object, expected: str | None) -> None:
    assert auth_id_from_return_value(value) == expected


def test_stroops_round_like_the_backend() -> None:
    assert usdc_to_stroops(0.07) == 700_000
    assert usdc_to_stroops(0.012) == 120_000

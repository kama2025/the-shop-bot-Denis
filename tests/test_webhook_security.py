"""Проверка подлинности входящих callback'ов."""

from __future__ import annotations

import hashlib
import hmac
import json

from bot.payments.cryptobot import CryptoBotProvider
from bot.payments.platega import PlategaProvider

TOKEN = "12345:test-token"
MERCHANT = "merchant-uuid"
SECRET = "very-secret"


def _cryptobot() -> CryptoBotProvider:
    return CryptoBotProvider(base_url="https://testnet-pay.crypt.bot/api", token=TOKEN)


def _sign(body: bytes) -> str:
    key = hashlib.sha256(TOKEN.encode()).digest()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def test_cryptobot_accepts_valid_signature() -> None:
    provider = _cryptobot()
    body = json.dumps({"update_type": "invoice_paid"}).encode()
    assert provider.verify_signature(body, _sign(body)) is True


def test_cryptobot_rejects_tampered_body() -> None:
    """Подпись считается от тела: правка тела обязана ломать подпись."""
    provider = _cryptobot()
    original = json.dumps({"amount": "90.00"}).encode()
    signature = _sign(original)
    tampered = json.dumps({"amount": "1.00"}).encode()
    assert provider.verify_signature(tampered, signature) is False


def test_cryptobot_rejects_missing_and_garbage_signature() -> None:
    provider = _cryptobot()
    body = b"{}"
    assert provider.verify_signature(body, None) is False
    assert provider.verify_signature(body, "") is False
    assert provider.verify_signature(body, "deadbeef") is False


def test_cryptobot_rejects_signature_from_other_token() -> None:
    provider = _cryptobot()
    body = b'{"x":1}'
    other_key = hashlib.sha256(b"another-token").digest()
    foreign = hmac.new(other_key, body, hashlib.sha256).hexdigest()
    assert provider.verify_signature(body, foreign) is False


def _platega() -> PlategaProvider:
    return PlategaProvider(
        base_url="https://app.platega.io",
        merchant_id=MERCHANT,
        secret=SECRET,
        method_codes=[2],
        return_url="https://t.me/x",
        failed_url="https://t.me/x",
    )


def test_platega_checks_both_headers() -> None:
    provider = _platega()
    assert provider.verify_callback_secret(MERCHANT, SECRET) is True
    assert provider.verify_callback_secret(MERCHANT, "wrong") is False
    assert provider.verify_callback_secret("wrong", SECRET) is False
    assert provider.verify_callback_secret(None, SECRET) is False
    assert provider.verify_callback_secret(MERCHANT, None) is False
    assert provider.verify_callback_secret("", "") is False


def test_platega_method_list_matches_configuration() -> None:
    provider = PlategaProvider(
        base_url="https://app.platega.io",
        merchant_id=MERCHANT,
        secret=SECRET,
        method_codes=[2, 11],
        return_url="https://t.me/x",
        failed_url="https://t.me/x",
    )
    codes = [method.code for method in provider.methods()]
    assert codes == ["platega:2", "platega:11"]

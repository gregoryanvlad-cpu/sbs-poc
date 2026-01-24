from __future__ import annotations

import base64
import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)


def _derive_key(secret: str) -> bytes:
    """Derive a fernet key from arbitrary secret.

    Fernet expects urlsafe base64-encoded 32-byte key.
    """
    raw = secret.encode("utf-8")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"sbs-vpn-key-v1",
        info=b"vpn-private-key",
    )
    key = hkdf.derive(raw)
    return base64.urlsafe_b64encode(key)


def get_fernet() -> Fernet | None:
    secret = os.getenv("VPN_KEY_ENC_SECRET", "").strip()
    if not secret:
        return None
    # allow passing a raw fernet key or any secret
    try:
        if len(secret) >= 40 and all(c.isalnum() or c in "-_" for c in secret):
            return Fernet(secret.encode("utf-8"))
    except Exception:
        pass
    return Fernet(_derive_key(secret))


def encrypt(text: str) -> str:
    f = get_fernet()
    if f is None:
        # dev fallback (insecure) - but keeps DB contract
        log.warning("vpn_key_secret_missing_encrypt_passthrough")
        return text
    return f.encrypt(text.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    f = get_fernet()
    if f is None:
        log.warning("vpn_key_secret_missing_decrypt_passthrough")
        return token
    try:
        return f.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # if previously stored plaintext
        return token

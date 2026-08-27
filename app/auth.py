"""Authentification : hachage de mot de passe (pbkdf2, stdlib) et cookies de
session signés (HMAC, stdlib). Aucune dépendance externe.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

from app.config import get_settings

_PBKDF2_ROUNDS = 200_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8


# ---------- mots de passe ----------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email.strip()))


# ---------- session (cookie signé) ----------

def _secret() -> bytes:
    return get_settings().secret_key.encode()


def sign(value: str) -> str:
    sig = hmac.new(_secret(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"


def unsign(token: str) -> str | None:
    if not token or "." not in token:
        return None
    value, _, sig = token.rpartition(".")
    expected = hmac.new(_secret(), value.encode(), hashlib.sha256).hexdigest()
    return value if hmac.compare_digest(sig, expected) else None

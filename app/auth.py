"""Authentification : hachage de mot de passe (pbkdf2, stdlib) et cookies de
session signés (HMAC, stdlib). Aucune dépendance externe.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

from app.config import get_settings

_PBKDF2_ROUNDS = 300_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128  # borne : évite un DoS pbkdf2 avec un mot de passe géant

# Haché « bidon » au bon format : sert à égaliser le temps de réponse du login
# quand l'email n'existe pas (sinon oracle temporel d'énumération).
_DUMMY_HASH = (
    "pbkdf2_sha256$300000$"
    "00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


# ---------- mots de passe ----------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def dummy_verify(password: str) -> None:
    """Consomme ~le même temps qu'un verify réel (login sur email inconnu)."""
    verify_password(password, _DUMMY_HASH)


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


# ---------- porte d'accès (mot de passe partagé) ----------

def gate_token(access_password: str) -> str:
    """Jeton du cookie de porte : change si le mot de passe partagé change."""
    digest = hashlib.sha256(f"gate::{access_password}".encode()).hexdigest()
    return sign(f"gate:{digest}")


def gate_ok(cookie_value: str, access_password: str) -> bool:
    return hmac.compare_digest(cookie_value or "", gate_token(access_password))

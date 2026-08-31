"""Authentification : hachage de mot de passe (pbkdf2, stdlib) et cookies de
session signés (HMAC, stdlib). Aucune dépendance externe.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time

from app.config import get_settings

PW_RESET_TTL = 3600  # secondes de validité d'un lien de réinitialisation
EMAIL_VERIFY_TTL = 7 * 24 * 3600  # 7 jours pour confirmer son adresse

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


# ---------- réinitialisation de mot de passe ----------

def hash_fingerprint(password_hash: str) -> str:
    """Fragment qui change à chaque changement de mot de passe (fin du hash)."""
    return (password_hash or "")[-16:]


def make_reset_token(user: dict) -> str:
    """Jeton à usage unique : lié à l'utilisateur, daté, invalidé dès que le
    mot de passe change (on y incorpore un fragment du hash courant)."""
    return sign(
        f"pwr:{user['id']}:{int(time.time())}:{hash_fingerprint(user['password_hash'])}"
    )


def read_reset_token(token: str) -> tuple[str, str] | None:
    """Renvoie (user_id, hash_prefix) si le jeton est valide et non expiré."""
    raw = unsign(token)
    if not raw or not raw.startswith("pwr:"):
        return None
    try:
        _, uid, ts, hp = raw.split(":")
        if time.time() - int(ts) > PW_RESET_TTL:
            return None
    except ValueError:
        return None
    return uid, hp


# ---------- confirmation d'adresse email ----------

def make_verify_token(user: dict) -> str:
    """Jeton de confirmation d'email : lié à l'utilisateur et daté."""
    return sign(f"evr:{user['id']}:{int(time.time())}")


def read_verify_token(token: str) -> str | None:
    """Renvoie l'user_id si le jeton de confirmation est valide et non expiré."""
    raw = unsign(token)
    if not raw or not raw.startswith("evr:"):
        return None
    try:
        _, uid, ts = raw.split(":")
        if time.time() - int(ts) > EMAIL_VERIFY_TTL:
            return None
    except ValueError:
        return None
    return uid

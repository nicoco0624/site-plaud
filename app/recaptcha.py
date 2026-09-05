"""Vérification reCAPTCHA v3 (Google), côté serveur.

Désactivé tant que RECAPTCHA_SITE_KEY / RECAPTCHA_SECRET_KEY ne sont pas
renseignées (`get_settings().recaptcha_enabled`) : dans ce cas `verify()`
renvoie toujours True pour ne pas bloquer l'inscription.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from app.config import get_settings

_VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"


def verify(token: str, *, remote_ip: str | None, expected_action: str) -> bool:
    """True si le jeton est valide, correspond à l'action attendue et que le
    score >= recaptcha_min_score. En cas de panne de l'API Google, on laisse
    passer (fail-open) plutôt que de bloquer toutes les inscriptions."""
    s = get_settings()
    if not s.recaptcha_enabled:
        return True
    if not token:
        return False

    data = {"secret": s.recaptcha_secret_key, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    body = urllib.parse.urlencode(data).encode()

    try:
        with urllib.request.urlopen(_VERIFY_URL, data=body, timeout=10) as r:
            result = json.load(r)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return True  # API Google injoignable -> ne pas pénaliser les utilisateurs

    if not result.get("success"):
        return False
    if expected_action and result.get("action") != expected_action:
        return False
    try:
        score = float(result.get("score", 0))
    except (TypeError, ValueError):
        return False
    return score >= s.recaptcha_min_score

"""Webhook Gumroad : analyse du payload + vérification.

Gumroad envoie un POST `application/x-www-form-urlencoded` :
  - "Ping" (Settings -> Advanced -> Ping)    : uniquement à la vente
  - "Resource subscriptions" (via l'API)     : sale / refund / dispute /
    cancellation / subscription_ended / subscription_updated ...

Champs utiles : seller_id, product_id, product_permalink, email, sale_id,
subscription_id, recurrence, cancelled, refunded, dispute, dispute_won,
subscription_ended_at, ended, test, resource_name.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from app.config import get_settings

_API = "https://api.gumroad.com/v2"


def truthy(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def parse_body(raw: bytes, content_type: str) -> dict:
    """Renvoie un dict plat à partir du corps de la requête (form ou JSON)."""
    ct = (content_type or "").lower()
    if "application/json" in ct:
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {"_": data}
        except ValueError:
            return {}
    # form-urlencoded (cas Gumroad)
    pairs = urllib.parse.parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)
    out: dict = {}
    for k, v in pairs:
        out[k] = v  # dernière valeur en cas de doublon (suffisant ici)
    return out


def classify(p: dict) -> tuple[str | None, bool | None, str | None, str]:
    """(email, actif, subscription_id, libellé_evenement).
    `actif` = True -> activer l'abonnement, False -> couper, None -> ignorer."""
    email = (p.get("email") or p.get("purchaser_email") or "").strip().lower() or None
    sub_id = p.get("subscription_id") or None
    resource = (p.get("resource_name") or "").lower()

    ended = bool(
        truthy(p.get("refunded"))
        or truthy(p.get("chargebacked"))
        or truthy(p.get("cancelled"))
        or truthy(p.get("ended"))
        or p.get("subscription_ended_at")
        or p.get("cancelled_at")
        or resource in {"refund", "dispute", "cancellation", "subscription_ended"}
    )
    if truthy(p.get("dispute_won")):  # litige gagné par le vendeur -> on garde
        ended = False

    if ended:
        return email, False, sub_id, resource or "cancellation/refund"

    # Vente / paiement récurrent réussi
    is_sale = bool(
        p.get("sale_id") or p.get("order_number")
        or resource in {"sale", "subscription_updated", "subscription_restarted"}
    )
    if is_sale:
        return email, True, sub_id, resource or "sale"

    return email, None, sub_id, resource or "inconnu"


def seller_ok(p: dict) -> bool:
    """Contrôle basique d'authenticité : seller_id et/ou produit attendus."""
    s = get_settings()
    if s.gumroad_seller_id and p.get("seller_id") != s.gumroad_seller_id:
        return False
    if s.gumroad_product_id and p.get("product_id"):
        if p.get("product_id") != s.gumroad_product_id:
            return False
    elif s.gumroad_product_permalink and p.get("product_permalink"):
        if p["product_permalink"].lower() != s.gumroad_product_permalink.lower():
            return False
    # au moins un critère doit avoir été vérifié
    return bool(s.gumroad_seller_id or s.gumroad_product_id
                or s.gumroad_product_permalink)


def verify_sale(sale_id: str) -> dict | None:
    """Re-vérifie une vente auprès de l'API Gumroad avec l'access token.
    Renvoie la vente si l'API confirme, None sinon (ou si pas de token)."""
    tok = get_settings().gumroad_access_token
    if not tok or not sale_id:
        return None
    url = f"{_API}/sales/{urllib.parse.quote(sale_id)}?access_token={urllib.parse.quote(tok)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.load(r)
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None
    if not data.get("success"):
        return None
    return data.get("sale") or None

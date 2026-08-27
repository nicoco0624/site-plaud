"""Limiteur de débit en mémoire (fenêtre glissante).

Sans dépendance ni service externe. Suffisant pour une instance unique
(l'offre gratuite Render n'en lance qu'une). L'état est perdu au redémarrage,
ce qui est acceptable pour un garde-fou anti-abus / anti-brute-force.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}
_MAX_KEYS = 20_000  # borne mémoire dure


def allow(key: str, limit: int, window_s: int) -> bool:
    """True si l'appel est autorisé, False si le quota est dépassé pour `key`."""
    now = time.time()
    cutoff = now - window_s
    with _lock:
        if key not in _hits and len(_hits) >= _MAX_KEYS:
            # purge grossière : on repart de zéro plutôt que de fuir la mémoire
            _hits.clear()
        q = [t for t in _hits.get(key, ()) if t >= cutoff]
        if len(q) >= limit:
            _hits[key] = q
            return False
        q.append(now)
        _hits[key] = q
        return True


def retry_after(key: str, window_s: int) -> int:
    """Secondes avant que `key` retrouve un crédit (approximatif)."""
    with _lock:
        q = _hits.get(key)
        if not q:
            return 0
    return max(1, int(q[0] + window_s - time.time()))


def reset(key: str) -> None:
    with _lock:
        _hits.pop(key, None)

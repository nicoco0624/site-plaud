"""Accès base de données.

Deux backends, choisis d'après DATABASE_URL :
  - ``sqlite:///chemin``            -> fichier SQLite local (stdlib, dev)
  - ``libsql://xxx.turso.io`` /     -> Turso via son API HTTP « pipeline »
    ``https://xxx.turso.io``           (+ TURSO_AUTH_TOKEN), aucune dépendance native

Une seule table : ``notes``. Les fonctions publiques (create_note, get_note,
list_notes, update_note, init_db) renvoient / acceptent des dicts, quel que soit
le backend.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

# Statuts possibles d'une note, dans l'ordre du pipeline.
STATUS_UPLOADED = "uploaded"
STATUS_TRANSCRIBING = "transcribing"
STATUS_TRANSCRIBED = "transcribed"
STATUS_SUMMARIZING = "summarizing"
STATUS_DONE = "done"
STATUS_ARCHIVING = "archiving"
STATUS_ARCHIVED = "archived"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_ERROR = "error"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS notes (
    id                TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    stored_path       TEXT NOT NULL,
    content_type      TEXT,
    size_bytes        INTEGER NOT NULL,
    status            TEXT NOT NULL,
    error             TEXT,
    transcript_path   TEXT,
    summary_path      TEXT,
    title             TEXT,
    archive_links     TEXT,
    archived_at       TEXT,
    drive_folder_link TEXT,
    emailed_at        TEXT,
    user_id           TEXT
)
"""

_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
)
"""

# Colonnes ajoutées après coup : (nom, type). Appliquées si absentes, pour ne pas
# casser une base créée par une version antérieure.
_MIGRATIONS = [
    ("title", "TEXT"),
    ("archive_links", "TEXT"),  # JSON : [{"provider","name","key"/"link"}, ...]
    ("archived_at", "TEXT"),
    ("drive_folder_link", "TEXT"),
    ("emailed_at", "TEXT"),
    ("user_id", "TEXT"),
]

_USER_MIGRATIONS = [
    ("audio_used", "INTEGER NOT NULL DEFAULT 0"),
    ("video_used", "INTEGER NOT NULL DEFAULT 0"),
    ("subscribed", "INTEGER NOT NULL DEFAULT 0"),
    ("email_verified", "INTEGER NOT NULL DEFAULT 0"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
#  Backends
# --------------------------------------------------------------------------- #

class _SqliteBackend:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            conn.commit()
            return rows
        finally:
            conn.close()


class _TursoBackend:
    def __init__(self, url: str, token: str):
        host = url.replace("libsql://", "https://").rstrip("/")
        self.endpoint = f"{host}/v2/pipeline"
        self.token = token

    @staticmethod
    def _tag(v):
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            return {"type": "integer", "value": str(int(v))}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        return {"type": "text", "value": str(v)}

    @staticmethod
    def _untag(cell):
        t = cell.get("type")
        if t == "null":
            return None
        v = cell.get("value")
        if t == "integer":
            return int(v)
        if t == "float":
            return float(v)
        return v  # text / blob (base64) laissés en l'état

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        body = {
            "requests": [
                {"type": "execute",
                 "stmt": {"sql": sql, "args": [self._tag(p) for p in params]}},
                {"type": "close"},
            ]
        }
        req = urllib.request.Request(
            self.endpoint,
            method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.load(r)

        res = payload["results"][0]
        if res.get("type") != "ok":
            raise RuntimeError(f"Turso: {res.get('error')}")
        result = res["response"]["result"]
        cols = [c["name"] for c in result["cols"]]
        return [
            {col: self._untag(cell) for col, cell in zip(cols, row)}
            for row in result["rows"]
        ]


@lru_cache
def _backend():
    s = get_settings()
    if s.db_backend == "turso":
        if not s.turso_auth_token:
            raise RuntimeError("DATABASE_URL pointe vers Turso mais TURSO_AUTH_TOKEN est vide.")
        return _TursoBackend(s.database_url, s.turso_auth_token)
    return _SqliteBackend(s.sqlite_path)


def _q(sql: str, params: tuple = ()) -> list[dict]:
    return _backend().query(sql, params)


# --------------------------------------------------------------------------- #
#  API publique
# --------------------------------------------------------------------------- #

def init_db() -> None:
    _q(_CREATE_TABLE)
    _q(_CREATE_USERS)
    cols = {r["name"] for r in _q("PRAGMA table_info(notes)")}
    for name, ddl in _MIGRATIONS:
        if name not in cols:
            _q(f"ALTER TABLE notes ADD COLUMN {name} {ddl}")
    ucols = {r["name"] for r in _q("PRAGMA table_info(users)")}
    for name, ddl in _USER_MIGRATIONS:
        if name not in ucols:
            _q(f"ALTER TABLE users ADD COLUMN {name} {ddl}")


# ----- utilisateurs -----

def create_user(*, user_id: str, email: str, password_hash: str) -> dict:
    _q(
        "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (user_id, email.lower(), password_hash, _now()),
    )
    return get_user(user_id)


def get_user(user_id: str) -> dict | None:
    rows = _q("SELECT * FROM users WHERE id = ?", (user_id,))
    return rows[0] if rows else None


def get_user_by_email(email: str) -> dict | None:
    rows = _q("SELECT * FROM users WHERE email = ?", (email.lower(),))
    return rows[0] if rows else None


def set_user_password(user_id: str, password_hash: str) -> None:
    _q("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def bump_usage(user_id: str, feature: str) -> None:
    """Incrémente le compteur d'usage d'une fonctionnalité ('audio' ou 'video')."""
    col = "audio_used" if feature == "audio" else "video_used"
    _q(f"UPDATE users SET {col} = COALESCE({col}, 0) + 1 WHERE id = ?", (user_id,))


def set_subscribed(user_id: str, value: bool) -> None:
    _q("UPDATE users SET subscribed = ? WHERE id = ?", (1 if value else 0, user_id))


def set_email_verified(user_id: str, value: bool = True) -> None:
    _q("UPDATE users SET email_verified = ? WHERE id = ?",
       (1 if value else 0, user_id))


def delete_user(user_id: str) -> None:
    """Supprime le compte et toutes ses notes (RGPD : droit à l'effacement).
    Le nettoyage des fichiers B2 est fait par l'appelant, à partir de list_notes."""
    _q("DELETE FROM notes WHERE user_id = ?", (user_id,))
    _q("DELETE FROM users WHERE id = ?", (user_id,))


def list_users() -> list[dict]:
    return _q(
        "SELECT id, email, created_at, audio_used, video_used, subscribed, "
        "email_verified FROM users ORDER BY created_at"
    )


# ----- notes -----

def create_note(
    *,
    note_id: str,
    user_id: str,
    original_filename: str,
    stored_path: str,
    content_type: str | None,
    size_bytes: int,
    status: str = STATUS_UPLOADED,
) -> dict:
    ts = _now()
    _q(
        """
        INSERT INTO notes (id, user_id, created_at, updated_at, original_filename,
                           stored_path, content_type, size_bytes, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (note_id, user_id, ts, ts, original_filename, stored_path, content_type,
         size_bytes, status),
    )
    return get_note(note_id)


def get_note(note_id: str) -> dict | None:
    rows = _q("SELECT * FROM notes WHERE id = ?", (note_id,))
    return rows[0] if rows else None


def list_notes(user_id: str, limit: int = 200) -> list[dict]:
    return _q(
        "SELECT * FROM notes WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )


def count_notes(user_id: str) -> int:
    rows = _q("SELECT COUNT(*) AS n FROM notes WHERE user_id = ?", (user_id,))
    return int(rows[0]["n"]) if rows else 0


# ----- vue admin (toutes les notes, tous les comptes) -----

def list_all_notes(limit: int = 1000) -> list[dict]:
    return _q(
        "SELECT n.*, u.email AS owner_email "
        "FROM notes n LEFT JOIN users u ON u.id = n.user_id "
        "ORDER BY n.created_at DESC LIMIT ?",
        (limit,),
    )


def global_stats() -> dict:
    users = int(_q("SELECT COUNT(*) AS c FROM users")[0]["c"])
    agg = _q("SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes), 0) AS s FROM notes")[0]
    return {"users": users, "notes": int(agg["c"]), "bytes": int(agg["s"])}


def update_note(note_id: str, **fields) -> dict | None:
    if not fields:
        return get_note(note_id)
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    _q(f"UPDATE notes SET {cols} WHERE id = ?", (*fields.values(), note_id))
    return get_note(note_id)


def delete_note(note_id: str) -> None:
    _q("DELETE FROM notes WHERE id = ?", (note_id,))

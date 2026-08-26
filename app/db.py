"""Accès SQLite (bibliothèque standard).

Volontairement minimaliste : une seule table `notes`. À l'étape 6, cette couche
sera adaptée pour parler à Turso (libSQL) sans changer le reste de l'app.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

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

_SCHEMA = """
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
    emailed_at        TEXT
);
"""

# Colonnes ajoutées après coup : (nom, définition SQL). Appliquées si absentes,
# pour ne pas casser une base déjà créée par une version antérieure.
_MIGRATIONS = [
    ("title", "TEXT"),
    ("archive_links", "TEXT"),  # JSON : [{"provider","name","link"}, ...]
    ("archived_at", "TEXT"),
    ("drive_folder_link", "TEXT"),
    ("emailed_at", "TEXT"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(notes)")}
        for name, ddl in _MIGRATIONS:
            if name not in existing:
                conn.execute(f"ALTER TABLE notes ADD COLUMN {name} {ddl}")


def create_note(
    *,
    note_id: str,
    original_filename: str,
    stored_path: str,
    content_type: str | None,
    size_bytes: int,
    status: str = STATUS_UPLOADED,
) -> dict:
    ts = _now()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notes (id, created_at, updated_at, original_filename,
                               stored_path, content_type, size_bytes, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (note_id, ts, ts, original_filename, stored_path, content_type,
             size_bytes, status),
        )
    return get_note(note_id)


def get_note(note_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row) if row else None


def list_notes(limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_note(note_id: str, **fields) -> dict | None:
    if not fields:
        return get_note(note_id)
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE notes SET {cols} WHERE id = ?", (*fields.values(), note_id))
    return get_note(note_id)

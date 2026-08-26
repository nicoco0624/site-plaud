"""Client Google Drive pour l'archivage (étape 4).

Périmètre OAuth : `drive.file` — l'application ne voit et ne touche QUE les
fichiers et dossiers qu'elle a elle-même créés. C'est un scope « non sensible »,
donc aucune vérification Google n'est nécessaire une fois l'app publiée.

Autorisation : une seule fois, en local, via `python -m app.google_auth`
(ouvre le navigateur). Le jeton obtenu (`token.json`) contient un refresh_token
réutilisable indéfiniment tant que l'app est « En production ».
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from app.config import get_settings

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_FOLDER_MIME = "application/vnd.google-apps.folder"


def _q(value: str) -> str:
    """Échappe une valeur pour la syntaxe de requête Drive (`name = '...'`)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveNotAuthorized(RuntimeError):
    pass


def authorize() -> Path:
    """Lance le flux OAuth interactif et écrit token.json. À faire une seule fois."""
    settings = get_settings()
    if not settings.google_client_secret_path.exists():
        raise DriveNotAuthorized(
            f"{settings.google_client_secret_file} introuvable à la racine du projet."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.google_client_secret_path), SCOPES
    )
    creds = flow.run_local_server(port=0, prompt="consent")
    settings.google_token_path.write_text(creds.to_json(), encoding="utf-8")
    return settings.google_token_path


def _creds() -> Credentials:
    settings = get_settings()
    if not settings.google_token_path.exists():
        raise DriveNotAuthorized(
            "token.json absent — lance d'abord : .venv/bin/python -m app.google_auth"
        )
    creds = Credentials.from_authorized_user_file(
        str(settings.google_token_path), SCOPES
    )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            settings.google_token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise DriveNotAuthorized(
                "Jeton Google invalide/expiré sans refresh_token — relance "
                ".venv/bin/python -m app.google_auth"
            )
    return creds


def _service():
    return build("drive", "v3", credentials=_creds(), cache_discovery=False)


def ensure_folder(name: str, parent_id: str | None = None) -> str:
    """Renvoie l'id du dossier `name` (créé s'il n'existe pas déjà)."""
    svc = _service()
    q = (
        f"name = '{_q(name)}' and mimeType = '{_FOLDER_MIME}' and trashed = false"
        + (f" and '{parent_id}' in parents" if parent_id else "")
    )
    hits = svc.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
    if hits:
        return hits[0]["id"]

    meta = {"name": name, "mimeType": _FOLDER_MIME}
    if parent_id:
        meta["parents"] = [parent_id]
    return svc.files().create(body=meta, fields="id").execute()["id"]


def upload_file(
    local_path: Path, name: str | None = None, parent_id: str | None = None
) -> dict:
    """Téléverse un fichier. Renvoie {'id', 'name', 'link'}."""
    svc = _service()
    mime = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    meta: dict = {"name": name or local_path.name}
    if parent_id:
        meta["parents"] = [parent_id]
    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=True)
    f = (
        svc.files()
        .create(body=meta, media_body=media, fields="id, name, webViewLink")
        .execute()
    )
    return {"id": f["id"], "name": f["name"], "link": f.get("webViewLink", "")}


def folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"

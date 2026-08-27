"""Point d'entrée FastAPI — Site Plaud.

Étape 0 : squelette (config, base SQLite, page d'accueil, healthcheck).
Étape 1 : upload de fichiers audio (drag & drop + bouton) -> statut "uploaded".
Étape 2 : transcription Groq en tâche de fond, statut suivi par polling HTMX,
          consultation du texte transcrit.
Étape 3 : résumé structuré (titre + synthèse + points clés + actions) enchaîné
          après la transcription, consultable en Markdown.
Étape 4 : archivage automatique en ligne (Backblaze B2 par défaut, Drive en option).
Étape 5 : envoi d'un email récapitulatif (Gmail SMTP), renvoyable manuellement.
"""

import hashlib
import hmac
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import b2, db, pipeline, storage
from app.config import BASE_DIR, get_settings

settings = get_settings()

# Statuts « en cours » : tant qu'une note est dans un de ces états, le frontend
# continue de rafraîchir sa ligne.
PENDING_STATUSES = {
    db.STATUS_UPLOADED,
    db.STATUS_TRANSCRIBING,
    db.STATUS_SUMMARIZING,
    db.STATUS_ARCHIVING,
    db.STATUS_SENDING,
}

STATUS_LABELS = {
    db.STATUS_UPLOADED: "reçu",
    db.STATUS_TRANSCRIBING: "transcription…",
    db.STATUS_TRANSCRIBED: "transcrit",
    db.STATUS_SUMMARIZING: "résumé…",
    db.STATUS_DONE: "terminé",
    db.STATUS_ARCHIVING: "archivage…",
    db.STATUS_ARCHIVED: "archivé",
    db.STATUS_SENDING: "envoi email…",
    db.STATUS_SENT: "envoyé",
    db.STATUS_ERROR: "erreur",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    db.init_db()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
_STATIC_DIR = BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def _asset_version(name: str) -> str:
    """Suffixe de cache-busting basé sur la date de modif du fichier."""
    try:
        return str(int((_STATIC_DIR / name).stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["asset_version"] = _asset_version


def _human_size(n: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024 or unit == "Go":
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} Go"


templates.env.filters["human_size"] = lambda n: _human_size(float(n or 0))
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
templates.env.globals["PENDING_STATUSES"] = PENDING_STATUSES
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["auth_required"] = lambda: bool(get_settings().access_password)


# --------------------------------------------------------------------------- #
#  Accès : mot de passe unique (cookie signé). Vide -> site ouvert.
# --------------------------------------------------------------------------- #
_AUTH_COOKIE = "plaud_auth"
_AUTH_EXEMPT = ("/login", "/logout", "/healthz", "/static/", "/favicon.ico")


def _auth_token(password: str) -> str:
    return hashlib.sha256(f"plaud::{password}".encode()).hexdigest()


def _is_authed(request: Request, password: str) -> bool:
    return hmac.compare_digest(
        request.cookies.get(_AUTH_COOKIE, ""), _auth_token(password)
    )


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    password = get_settings().access_password
    if password:
        path = request.url.path
        exempt = any(path == p or path.startswith(p) for p in _AUTH_EXEMPT)
        if not exempt and not _is_authed(request, password):
            if request.headers.get("hx-request") == "true":
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse(f"/login?next={path}", status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if not get_settings().access_password:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next_url": next, "error": False}
    )


@app.post("/login")
def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form("/"),
):
    real = get_settings().access_password
    if real and hmac.compare_digest(password, real):
        target = next if next.startswith("/") else "/"
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(
            _AUTH_COOKIE,
            _auth_token(real),
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return resp
    return templates.TemplateResponse(
        request, "login.html", {"next_url": next, "error": True}, status_code=401
    )


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(_AUTH_COOKIE)
    return resp


def _note_or_404(note_id: str) -> dict:
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note introuvable")
    return note


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "app": settings.app_name}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "max_upload_mb": settings.max_upload_mb,
            "notes_count": len(db.list_notes()),
        },
    )


@app.get("/notes", response_class=HTMLResponse)
def notes_list(request: Request):
    return templates.TemplateResponse(request, "_notes.html", {"notes": db.list_notes()})


@app.get("/notes/{note_id}/row", response_class=HTMLResponse)
def note_row(request: Request, note_id: str):
    """Ligne d'une note, ré-interrogée périodiquement par HTMX tant qu'elle est en cours."""
    if not db.get_note(note_id):
        return HTMLResponse("")  # note supprimée entre-temps : la ligne disparaît
    return templates.TemplateResponse(
        request, "_note_row.html", {"n": _note_or_404(note_id)}
    )


def _serve_text_artifact(note: dict, path_key: str, archived_name: str):
    """Sert un fichier texte local ; s'il a disparu (disque éphémère après
    redémarrage), redirige vers la copie archivée."""
    rel = note[path_key]
    if not rel:
        raise HTTPException(status_code=409, detail="Pas encore disponible")
    local = BASE_DIR / rel
    if local.exists():
        return PlainTextResponse(local.read_text(encoding="utf-8"))
    links = json.loads(note["archive_links"] or "[]")
    if any(l.get("name") == archived_name for l in links):
        return RedirectResponse(f"/notes/{note['id']}/dl/{archived_name}")
    raise HTTPException(status_code=410, detail="Fichier non disponible")


@app.get("/notes/{note_id}/transcript")
def note_transcript(note_id: str):
    return _serve_text_artifact(_note_or_404(note_id), "transcript_path", "transcript.txt")


@app.get("/notes/{note_id}/summary")
def note_summary(note_id: str):
    return _serve_text_artifact(_note_or_404(note_id), "summary_path", "summary.md")


@app.get("/notes/{note_id}/dl/{name}")
def note_download(note_id: str, name: str):
    """Redirige vers une URL de téléchargement fraîche pour un fichier archivé."""
    note = _note_or_404(note_id)
    links = json.loads(note["archive_links"] or "[]")
    entry = next((l for l in links if l.get("name") == name), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    if entry["provider"] == "b2":
        return RedirectResponse(b2.presigned_url(entry["key"], expires=3600))
    if entry["provider"] == "gdrive" and entry.get("link"):
        return RedirectResponse(entry["link"])
    raise HTTPException(status_code=409, detail="Lien de téléchargement indisponible")


@app.post("/notes/{note_id}/email", response_class=HTMLResponse)
def note_send_email(request: Request, background: BackgroundTasks, note_id: str):
    """(Re)déclenche l'envoi de l'email récapitulatif pour une note."""
    note = _note_or_404(note_id)
    if not note["summary_path"]:
        raise HTTPException(status_code=409, detail="Résumé pas encore disponible")
    background.add_task(pipeline.run_email, note_id)
    db.update_note(note_id, status=db.STATUS_SENDING, error=None)
    return templates.TemplateResponse(
        request, "_note_row.html", {"n": db.get_note(note_id)}
    )


@app.delete("/notes/{note_id}")
def note_delete(note_id: str):
    """Supprime une note : fichiers archivés (B2), fichiers locaux, ligne en base."""
    note = _note_or_404(note_id)

    for entry in json.loads(note["archive_links"] or "[]"):
        if entry.get("provider") == "b2" and entry.get("key"):
            try:
                b2.delete(entry["key"])
            except Exception:  # noqa: BLE001 — best effort
                pass

    try:
        note_dir = (BASE_DIR / note["stored_path"]).parent
        if note_dir.is_dir() and note_dir != BASE_DIR:
            shutil.rmtree(note_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass

    db.delete_note(note_id)
    return Response(status_code=200, headers={"HX-Trigger": "refreshNotes"})


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, background: BackgroundTasks, file: UploadFile):
    note_id = uuid.uuid4().hex[:12]
    try:
        rel_path, size = await storage.save_upload(file, note_id)
    except (storage.UnsupportedFormat, storage.UploadTooLarge) as exc:
        return templates.TemplateResponse(
            request, "_upload_result.html", {"error": str(exc)}, status_code=400
        )

    note = db.create_note(
        note_id=note_id,
        original_filename=file.filename or f"{note_id}{Path(rel_path).suffix}",
        stored_path=str(rel_path),
        content_type=file.content_type,
        size_bytes=size,
    )
    # Étape 2 : la transcription démarre en tâche de fond dès la réponse envoyée.
    background.add_task(pipeline.run_transcription, note_id)

    resp = templates.TemplateResponse(request, "_upload_result.html", {"note": note})
    resp.headers["HX-Trigger"] = "refreshNotes"
    return resp

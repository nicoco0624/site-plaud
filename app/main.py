"""Point d'entrée FastAPI — Site Plaud.

Étape 0 : squelette (config, base SQLite, page d'accueil, healthcheck).
Étape 1 : upload de fichiers audio (drag & drop + bouton) -> statut "uploaded".
Étape 2 : transcription Groq en tâche de fond, statut suivi par polling HTMX,
          consultation du texte transcrit.
"""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, pipeline, storage
from app.config import BASE_DIR, get_settings

settings = get_settings()

# Statuts « en cours » : tant qu'une note est dans un de ces états, le frontend
# continue de rafraîchir sa ligne.
PENDING_STATUSES = {db.STATUS_UPLOADED, db.STATUS_TRANSCRIBING, db.STATUS_SUMMARIZING}

STATUS_LABELS = {
    db.STATUS_UPLOADED: "reçu",
    db.STATUS_TRANSCRIBING: "transcription…",
    db.STATUS_TRANSCRIBED: "transcrit",
    db.STATUS_SUMMARIZING: "résumé…",
    db.STATUS_DONE: "terminé",
    db.STATUS_ARCHIVED: "archivé",
    db.STATUS_ERROR: "erreur",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    db.init_db()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def _human_size(n: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024 or unit == "Go":
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} Go"


templates.env.filters["human_size"] = lambda n: _human_size(float(n or 0))
templates.env.globals["PENDING_STATUSES"] = PENDING_STATUSES
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS


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
        request, "index.html", {"max_upload_mb": settings.max_upload_mb}
    )


@app.get("/notes", response_class=HTMLResponse)
def notes_list(request: Request):
    return templates.TemplateResponse(request, "_notes.html", {"notes": db.list_notes()})


@app.get("/notes/{note_id}/row", response_class=HTMLResponse)
def note_row(request: Request, note_id: str):
    """Ligne d'une note, ré-interrogée périodiquement par HTMX tant qu'elle est en cours."""
    return templates.TemplateResponse(
        request, "_note_row.html", {"n": _note_or_404(note_id)}
    )


@app.get("/notes/{note_id}/transcript", response_class=PlainTextResponse)
def note_transcript(note_id: str):
    note = _note_or_404(note_id)
    if not note["transcript_path"]:
        raise HTTPException(status_code=409, detail="Transcription pas encore disponible")
    return (BASE_DIR / note["transcript_path"]).read_text(encoding="utf-8")


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

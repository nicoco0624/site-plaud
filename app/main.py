"""Point d'entrée FastAPI — Site Plaud.

Étape 0 : squelette (config, base SQLite, page d'accueil, healthcheck).
Étape 1 : upload de fichiers audio (drag & drop + bouton), enregistrement disque
          + ligne en base au statut "uploaded".
"""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, storage
from app.config import BASE_DIR, get_settings

settings = get_settings()


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
    return templates.TemplateResponse(
        request, "_notes.html", {"notes": db.list_notes()}
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile):
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
    # L'étape 2 déclenchera ici la transcription en tâche de fond.
    resp = templates.TemplateResponse(
        request, "_upload_result.html", {"note": note}
    )
    resp.headers["HX-Trigger"] = "refreshNotes"
    return resp

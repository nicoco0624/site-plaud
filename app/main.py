"""Point d'entrée FastAPI — Site Plaud.

Pipeline : upload (fichier ou micro) -> transcription (Groq Whisper) -> résumé
(Groq LLM) -> archivage (Backblaze B2) -> email récapitulatif (Resend / SMTP).
Comptes email + mot de passe, notes cloisonnées par utilisateur.
"""

import json
import logging
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import ai, auth, b2, db, mailer, pipeline, ratelimit, storage, youtube
from app.config import BASE_DIR, get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
slog = logging.getLogger("plaud.security")

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
    settings.assert_secure()
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    db.init_db()
    slog.info("démarrage (prod=%s, backend=%s)", settings.is_prod, settings.db_backend)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)
_STATIC_DIR = BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

if settings.allowed_hosts_list and settings.allowed_hosts_list != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list)

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    # lecteur audio de la page détail : la piste est servie via /notes/{id}/dl/…
    # qui redirige (302) vers une URL B2 signée.
    "media-src 'self' https://*.backblazeb2.com; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _safe_next(value: str) -> str:
    """Empêche l'open redirect : n'accepte qu'un chemin interne."""
    if value.startswith("/") and not value.startswith(("//", "/\\")):
        return value
    return "/"


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


# --------------------------------------------------------------------------- #
#  Comptes : email + mot de passe, session par cookie signé.
# --------------------------------------------------------------------------- #
_SESSION_COOKIE = "plaud_session"
_EXEMPT_EXACT = {"/login", "/register", "/logout", "/forgot", "/reset",
                 "/healthz", "/favicon.ico"}
_EXEMPT_PREFIX = ("/static/",)
_SENSITIVE_USER_FIELDS = {"password_hash"}


def _public_user(row: dict | None) -> dict | None:
    """Retire les champs sensibles, ajoute le drapeau admin."""
    if not row:
        return None
    u = {k: v for k, v in row.items() if k not in _SENSITIVE_USER_FIELDS}
    u["is_admin"] = (u.get("email") or "").lower() in settings.admin_emails_list
    return u


def _current_user(request: Request) -> dict | None:
    uid = auth.unsign(request.cookies.get(_SESSION_COOKIE, ""))
    return _public_user(db.get_user(uid)) if uid else None


def _set_session(resp: Response, request: Request, user_id: str) -> None:
    # Cookie de session (pas de max_age / expires) : le navigateur l'efface à sa
    # fermeture -> il faut se reconnecter (email + mot de passe) à chaque
    # nouvelle session. Les notes, elles, restent en base (liées au compte).
    resp.set_cookie(
        _SESSION_COOKIE,
        auth.sign(user_id),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.url.path
    request.state.user = _current_user(request)
    exempt = path in _EXEMPT_EXACT or path.startswith(_EXEMPT_PREFIX)
    if not exempt and request.state.user is None:
        if request.headers.get("hx-request") == "true":
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse(f"/login?next={_safe_next(path)}", status_code=303)
    return await call_next(request)


# Enregistré en dernier -> couche la plus externe : les en-têtes de sécurité
# s'appliquent à TOUTES les réponses, y compris les redirections d'auth.
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Permissions-Policy", "geolocation=(), camera=(), microphone=(self)"
    )
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if request.url.scheme == "https":
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return resp


def require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Non connecté")
    return user


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if _current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next_url": next, "error": None}
    )


@app.post("/login")
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    ip = _client_ip(request)
    email = email.strip().lower()
    if not (ratelimit.allow(f"login-ip:{ip}", 15, 900)
            and ratelimit.allow(f"login-acct:{email}", 6, 900)):
        slog.warning("login rate-limit ip=%s email=%s", ip, email)
        return templates.TemplateResponse(
            request, "login.html",
            {"next_url": _safe_next(next),
             "error": "Trop de tentatives. Réessaie dans quelques minutes."},
            status_code=429,
        )

    user = db.get_user_by_email(email)
    if user and auth.verify_password(password, user["password_hash"]):
        ratelimit.reset(f"login-acct:{email}")
        slog.info("login ok email=%s ip=%s", email, ip)
        resp = RedirectResponse(_safe_next(next), status_code=303)
        _set_session(resp, request, user["id"])
        return resp

    if not user:
        auth.dummy_verify(password)  # égalise le temps de réponse
    slog.warning("login échoué email=%s ip=%s", email, ip)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next_url": _safe_next(next), "error": "Email ou mot de passe incorrect."},
        status_code=401,
    )


# --------------------------------------------------------------------------- #
#  Mot de passe oublié
# --------------------------------------------------------------------------- #
_FORGOT_DONE = ("Si un compte existe pour cette adresse, un email avec un lien "
                "de réinitialisation vient d'être envoyé.")


def _base_url(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


@app.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    if _current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "forgot.html", {"error": None, "done": None})


@app.post("/forgot", response_class=HTMLResponse)
def forgot_submit(request: Request, email: str = Form("")):
    ip = _client_ip(request)
    email = email.strip().lower()
    if not (ratelimit.allow(f"forgot-ip:{ip}", 5, 1800)
            and ratelimit.allow(f"forgot-acct:{email}", 3, 1800)):
        return templates.TemplateResponse(
            request, "forgot.html",
            {"error": "Trop de demandes. Réessaie dans quelques minutes.", "done": None},
            status_code=429,
        )
    user = db.get_user_by_email(email)
    if user:
        link = f"{_base_url(request)}/reset?token={auth.make_reset_token(user)}"
        try:
            mailer.send_reset_email(email, link)
            slog.info("reset demandé email=%s ip=%s", email, ip)
        except Exception:  # noqa: BLE001
            slog.exception("envoi email reset KO email=%s", email)
    else:
        slog.info("reset demandé (compte inconnu) email=%s ip=%s", email, ip)
    return templates.TemplateResponse(
        request, "forgot.html", {"error": None, "done": _FORGOT_DONE}
    )


def _valid_reset_user(token: str) -> dict | None:
    """Utilisateur si le jeton est signé, non expiré ET encore d'actualité
    (le mot de passe n'a pas changé depuis l'émission)."""
    parsed = auth.read_reset_token(token)
    if not parsed:
        return None
    user = db.get_user(parsed[0])
    if not user or auth.hash_fingerprint(user["password_hash"]) != parsed[1]:
        return None
    return user


_RESET_INVALID = {"token": "", "invalid": True,
                  "error": "Lien invalide ou expiré. Refais une demande."}


@app.get("/reset", response_class=HTMLResponse)
def reset_form(request: Request, token: str = ""):
    if not _valid_reset_user(token):
        return templates.TemplateResponse(request, "reset.html", _RESET_INVALID,
                                          status_code=400)
    return templates.TemplateResponse(
        request, "reset.html", {"token": token, "error": None, "invalid": False}
    )


@app.post("/reset", response_class=HTMLResponse)
def reset_submit(
    request: Request,
    token: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
):
    user = _valid_reset_user(token)
    if not user:
        return templates.TemplateResponse(request, "reset.html", _RESET_INVALID,
                                          status_code=400)
    if not (auth.MIN_PASSWORD_LEN <= len(password) <= auth.MAX_PASSWORD_LEN):
        err = (f"Le mot de passe doit faire entre {auth.MIN_PASSWORD_LEN} et "
               f"{auth.MAX_PASSWORD_LEN} caractères.")
    elif password != password2:
        err = "Les deux mots de passe ne correspondent pas."
    else:
        err = None
    if err:
        return templates.TemplateResponse(
            request, "reset.html", {"token": token, "error": err, "invalid": False},
            status_code=400,
        )

    db.set_user_password(user["id"], auth.hash_password(password))
    slog.info("mot de passe réinitialisé user=%s ip=%s", user["id"], _client_ip(request))
    resp = RedirectResponse("/", status_code=303)
    _set_session(resp, request, user["id"])  # connecte directement
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    if _current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "register.html", {"error": None})


@app.post("/register")
def register_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
):
    ip = _client_ip(request)
    if not ratelimit.allow(f"register-ip:{ip}", 5, 3600):
        slog.warning("register rate-limit ip=%s", ip)
        return templates.TemplateResponse(
            request, "register.html",
            {"error": "Trop d'inscriptions depuis cette adresse. Réessaie plus tard."},
            status_code=429,
        )

    email = email.strip().lower()
    err = None
    if not auth.valid_email(email):
        err = "Adresse email invalide."
    elif not (auth.MIN_PASSWORD_LEN <= len(password) <= auth.MAX_PASSWORD_LEN):
        err = (f"Le mot de passe doit faire entre {auth.MIN_PASSWORD_LEN} et "
               f"{auth.MAX_PASSWORD_LEN} caractères.")
    elif password != password2:
        err = "Les deux mots de passe ne correspondent pas."
    elif db.get_user_by_email(email):
        err = "Un compte existe déjà avec cette adresse."
    if err:
        return templates.TemplateResponse(
            request, "register.html", {"error": err}, status_code=400
        )

    user = db.create_user(
        user_id=uuid.uuid4().hex[:12],
        email=email,
        password_hash=auth.hash_password(password),
    )
    slog.info("register email=%s ip=%s", email, ip)
    resp = RedirectResponse("/", status_code=303)
    _set_session(resp, request, user["id"])
    return resp


@app.get("/logout")
@app.post("/logout")
def logout(request: Request):
    user = getattr(request.state, "user", None)
    if user:
        slog.info("logout email=%s", user.get("email"))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(_SESSION_COOKIE)
    return resp


def _owned_note_or_404(note_id: str, user: dict, request: Request) -> dict:
    note = db.get_note(note_id)
    if not note or note.get("user_id") != user["id"]:
        if note:  # existe mais appartient à quelqu'un d'autre
            slog.warning("accès refusé note=%s par user=%s ip=%s",
                         note_id, user["id"], _client_ip(request))
        raise HTTPException(status_code=404, detail="Note introuvable")
    return note


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, user: dict = Depends(require_user)):
    """Page d'accueil : choix entre Résumé Audio et Résumé Vidéo."""
    if user.get("is_admin"):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request, "home.html", {"user": user})


@app.get("/audio", response_class=HTMLResponse)
def audio_app(request: Request, user: dict = Depends(require_user)):
    """L'app Plaud existante (upload audio, transcription, résumé, notes)."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "max_upload_mb": settings.max_upload_mb,
            "notes_count": db.count_notes(user["id"]),
        },
    )


@app.get("/video", response_class=HTMLResponse)
def video_page(request: Request, user: dict = Depends(require_user)):
    """Résumé Vidéo : coller un lien YouTube -> fiche de révision."""
    return templates.TemplateResponse(
        request, "video.html",
        {"user": user, "max_minutes": settings.video_max_minutes},
    )


@app.post("/video", response_class=HTMLResponse)
def video_generate(
    request: Request, url: str = Form(""), user: dict = Depends(require_user)
):
    """Récupère les sous-titres de la vidéo puis génère la fiche via l'IA.
    Renvoie toujours 200 : le résultat (fiche ou message d'erreur) est swappé
    dans #video-result par HTMX."""
    def _err(msg: str):
        return templates.TemplateResponse(
            request, "_video_result.html", {"error": msg}
        )

    if not user.get("is_admin") and not ratelimit.allow(
        f"video:{user['id']}", settings.video_per_hour, 3600
    ):
        return _err("Trop de fiches générées récemment. Réessaie dans un moment.")

    url = url.strip()
    try:
        src = youtube.fetch(url, max_minutes=settings.video_max_minutes)
    except youtube.InvalidURL:
        return _err("Lien YouTube invalide. Colle l'URL complète d'une vidéo "
                    "(youtube.com/watch?v=… ou youtu.be/…).")
    except youtube.NoTranscript:
        return _err("Cette vidéo n'a pas de sous-titres exploitables "
                    "(ni manuels, ni automatiques). Essaie une autre vidéo.")
    except youtube.VideoTooLong as exc:
        return _err(f"Vidéo trop longue (~{exc.approx_minutes} min). "
                    f"Limite actuelle : {settings.video_max_minutes} min.")
    except youtube.FetchError as exc:
        slog.warning("video fetch KO url=%s : %s", url, exc)
        return _err("Impossible de récupérer la transcription — YouTube a "
                    "peut-être bloqué la requête depuis le serveur. Réessaie "
                    "dans quelques minutes ou essaie une autre vidéo.")

    transcript = src["transcript"]
    truncated = len(transcript) > settings.video_transcript_max_chars
    try:
        sheet = ai.build_study_sheet(
            src["title"], transcript[: settings.video_transcript_max_chars]
        )
    except Exception:  # noqa: BLE001
        slog.exception("video IA KO url=%s user=%s", url, user["id"])
        return _err("La génération de la fiche a échoué. Réessaie dans un moment.")

    slog.info("video fiche générée url=%s user=%s chars=%d",
              url, user["id"], len(transcript))
    return templates.TemplateResponse(
        request, "_video_result.html",
        {
            "sheet": sheet,
            "src": src,
            "truncated": truncated,
            "video_url": f"https://www.youtube.com/watch?v={src['video_id']}",
        },
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, user: dict = Depends(require_user)):
    if not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="Not found")
    slog.info("admin dashboard email=%s ip=%s", user["email"], _client_ip(request))
    rows = [
        {**n, "transcript": mailer.load_transcript(n)}
        for n in db.list_all_notes()
    ]
    return templates.TemplateResponse(
        request, "admin.html",
        {"user": user, "rows": rows, "stats": db.global_stats()},
    )


@app.get("/notes", response_class=HTMLResponse)
def notes_list(request: Request, user: dict = Depends(require_user)):
    return templates.TemplateResponse(
        request, "_notes.html", {"notes": db.list_notes(user["id"])}
    )


@app.get("/notes/{note_id}/row", response_class=HTMLResponse)
def note_row(request: Request, note_id: str, user: dict = Depends(require_user)):
    """Ligne d'une note, ré-interrogée périodiquement par HTMX tant qu'elle est en cours."""
    note = db.get_note(note_id)
    if not note or note.get("user_id") != user["id"]:
        return HTMLResponse("")  # note absente / pas à cet utilisateur : ligne vide
    return templates.TemplateResponse(request, "_note_row.html", {"n": note})


@app.get("/notes/{note_id}", response_class=HTMLResponse)
def note_detail(request: Request, note_id: str, user: dict = Depends(require_user)):
    """Page détaillée d'une note, au style du site (résumé + transcription)."""
    note = _owned_note_or_404(note_id, user, request)
    summary = mailer.load_summary(note)
    # URL de lecture de l'audio d'origine, si archivé.
    audio_ext = Path(note["stored_path"]).suffix
    audio_name = f"original{audio_ext}"
    has_audio = any(
        e.get("name") == audio_name
        for e in json.loads(note["archive_links"] or "[]")
    )
    return templates.TemplateResponse(
        request,
        "note_detail.html",
        {
            "user": user,
            "n": note,
            "summary": summary,
            "transcript": mailer.load_transcript(note),
            "files": mailer.download_links(note),
            "audio_url": f"/notes/{note_id}/dl/{audio_name}" if has_audio else None,
            "audio_mime": storage.ALLOWED_EXTENSIONS.get(audio_ext, "audio/mpeg"),
        },
    )


def _resolve_inside(rel: str) -> Path | None:
    """Résout un chemin relatif et vérifie qu'il reste sous BASE_DIR."""
    p = (BASE_DIR / rel).resolve()
    return p if p.is_relative_to(BASE_DIR.resolve()) else None


def _serve_text_artifact(note: dict, path_key: str, archived_name: str):
    """Sert un fichier texte local ; s'il a disparu (disque éphémère après
    redémarrage), redirige vers la copie archivée."""
    rel = note[path_key]
    if not rel:
        raise HTTPException(status_code=409, detail="Pas encore disponible")
    local = _resolve_inside(rel)
    if local and local.is_file():
        try:
            return PlainTextResponse(local.read_text(encoding="utf-8"))
        except OSError:
            pass
    links = json.loads(note["archive_links"] or "[]")
    if any(l.get("name") == archived_name for l in links):
        return RedirectResponse(f"/notes/{note['id']}/dl/{archived_name}")
    raise HTTPException(status_code=410, detail="Fichier non disponible")


@app.get("/notes/{note_id}/transcript")
def note_transcript(request: Request, note_id: str, user: dict = Depends(require_user)):
    return _serve_text_artifact(
        _owned_note_or_404(note_id, user, request), "transcript_path", "transcript.txt"
    )


@app.get("/notes/{note_id}/summary")
def note_summary(request: Request, note_id: str, user: dict = Depends(require_user)):
    return _serve_text_artifact(
        _owned_note_or_404(note_id, user, request), "summary_path", "summary.md"
    )


@app.get("/notes/{note_id}/dl/{name}")
def note_download(
    request: Request, note_id: str, name: str, user: dict = Depends(require_user)
):
    """Redirige vers une URL de téléchargement fraîche pour un fichier archivé."""
    note = _owned_note_or_404(note_id, user, request)
    # `name` sert uniquement à retrouver une entrée, jamais à construire un chemin.
    entry = next(
        (l for l in json.loads(note["archive_links"] or "[]") if l.get("name") == name),
        None,
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    if entry.get("provider") == "b2" and entry.get("key"):
        return RedirectResponse(b2.presigned_url(entry["key"], expires=3600))
    if entry.get("provider") == "gdrive" and entry.get("link"):
        return RedirectResponse(entry["link"])
    raise HTTPException(status_code=409, detail="Lien de téléchargement indisponible")


@app.post("/notes/{note_id}/email", response_class=HTMLResponse)
def note_send_email(
    request: Request,
    background: BackgroundTasks,
    note_id: str,
    back: int = 0,
    user: dict = Depends(require_user),
):
    """(Re)déclenche l'envoi de l'email récapitulatif pour une note."""
    note = _owned_note_or_404(note_id, user, request)
    if not note["summary_path"]:
        raise HTTPException(status_code=409, detail="Résumé pas encore disponible")
    if not ratelimit.allow(f"email:{user['id']}", 12, 3600):
        raise HTTPException(status_code=429, detail="Trop d'envois. Réessaie plus tard.")
    slog.info("email manuel note=%s user=%s", note_id, user["id"])
    background.add_task(pipeline.run_email, note_id)
    db.update_note(note_id, status=db.STATUS_SENDING, error=None)
    if back:  # appelé depuis la page détail : on la recharge
        return Response(status_code=200, headers={"HX-Redirect": f"/notes/{note_id}"})
    return templates.TemplateResponse(
        request, "_note_row.html", {"n": db.get_note(note_id)}
    )


@app.post("/notes/{note_id}/rename")
def note_rename(
    request: Request, note_id: str, title: str = Form(""),
    user: dict = Depends(require_user),
):
    """Renomme le titre d'une note (édition manuelle)."""
    _owned_note_or_404(note_id, user, request)
    clean = " ".join(title.split())[:120].strip()
    if not clean:
        raise HTTPException(status_code=400, detail="Titre vide")
    db.update_note(note_id, title=clean)
    slog.info("note renommée note=%s user=%s", note_id, user["id"])
    return RedirectResponse(f"/notes/{note_id}", status_code=303)


@app.delete("/notes/{note_id}")
def note_delete(
    request: Request, note_id: str, back: int = 0,
    user: dict = Depends(require_user),
):
    """Supprime une note : fichiers archivés (B2), fichiers locaux, ligne en base."""
    note = _owned_note_or_404(note_id, user, request)

    for entry in json.loads(note["archive_links"] or "[]"):
        if entry.get("provider") == "b2" and entry.get("key"):
            try:
                b2.delete(entry["key"])
            except Exception:  # noqa: BLE001 — best effort
                slog.warning("échec suppression B2 key=%s", entry["key"])

    local = _resolve_inside(str(Path(note["stored_path"]).parent))
    if local and local.is_dir() and local != BASE_DIR.resolve():
        shutil.rmtree(local, ignore_errors=True)

    db.delete_note(note_id)
    slog.info("note supprimée note=%s user=%s", note_id, user["id"])
    headers = {"HX-Redirect": "/audio"} if back else {"HX-Trigger": "refreshNotes"}
    return Response(status_code=200, headers=headers)


@app.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile,
    user: dict = Depends(require_user),
):
    ip = _client_ip(request)

    # Quotas anti-abus (Groq / stockage).
    if not ratelimit.allow(f"upload:{user['id']}", settings.max_uploads_per_day, 86400):
        return templates.TemplateResponse(
            request, "_upload_result.html",
            {"error": f"Limite de {settings.max_uploads_per_day} envois / jour atteinte."},
            status_code=429,
        )
    if db.count_notes(user["id"]) >= settings.max_notes_per_user:
        return templates.TemplateResponse(
            request, "_upload_result.html",
            {"error": f"Maximum {settings.max_notes_per_user} notes. Supprimes-en avant d'en ajouter."},
            status_code=409,
        )

    # Rejet précoce si l'en-tête annonce déjà une taille hors limite.
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > settings.max_upload_bytes + 4096:
        return templates.TemplateResponse(
            request, "_upload_result.html",
            {"error": f"Fichier trop volumineux (limite : {settings.max_upload_mb} Mo)."},
            status_code=413,
        )

    note_id = uuid.uuid4().hex[:12]
    try:
        rel_path, size = await storage.save_upload(file, note_id)
    except (storage.UnsupportedFormat, storage.UploadTooLarge) as exc:
        return templates.TemplateResponse(
            request, "_upload_result.html", {"error": str(exc)}, status_code=400
        )
    except Exception:  # noqa: BLE001
        slog.exception("échec écriture upload user=%s ip=%s", user["id"], ip)
        return templates.TemplateResponse(
            request, "_upload_result.html",
            {"error": "Impossible d'enregistrer le fichier. Réessaie."},
            status_code=500,
        )

    note = db.create_note(
        note_id=note_id,
        user_id=user["id"],
        original_filename=file.filename or f"{note_id}{Path(rel_path).suffix}",
        stored_path=str(rel_path),
        content_type=file.content_type,
        size_bytes=size,
    )
    slog.info("upload note=%s user=%s taille=%d ip=%s", note_id, user["id"], size, ip)
    background.add_task(pipeline.run_transcription, note_id)

    resp = templates.TemplateResponse(request, "_upload_result.html", {"note": note})
    resp.headers["HX-Trigger"] = "refreshNotes"
    return resp


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Filet de sécurité : journalise et renvoie une erreur générique (pas de trace)."""
    slog.exception("erreur non gérée sur %s %s", request.method, request.url.path)
    if request.headers.get("hx-request") == "true" or request.url.path.startswith("/notes"):
        return JSONResponse({"detail": "Erreur interne"}, status_code=500)
    return PlainTextResponse("Erreur interne", status_code=500)

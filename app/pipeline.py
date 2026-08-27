"""Traitements de fond déclenchés après l'upload (FastAPI BackgroundTasks).

Étape 2 : transcription.
Étape 3 : résumé, enchaîné après une transcription réussie.
Étape 4 : archivage en ligne (Backblaze B2 par défaut, Google Drive en option).
Étape 5 : envoi de l'email récapitulatif, enchaîné après l'archivage.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app import ai, b2, db, drive, mailer
from app.config import BASE_DIR, get_settings

log = logging.getLogger("plaud.pipeline")

# En dessous de ce nombre de caractères, on ne tente pas de résumé.
_MIN_TRANSCRIPT_CHARS = 20


def run_transcription(note_id: str) -> None:
    """Transcrit l'audio d'une note, puis enchaîne sur le résumé."""
    note = db.get_note(note_id)
    if not note:
        log.warning("note %s introuvable, transcription annulée", note_id)
        return

    db.update_note(note_id, status=db.STATUS_TRANSCRIBING, error=None)
    audio_path = BASE_DIR / note["stored_path"]

    try:
        text = ai.transcribe(audio_path)
    except Exception as exc:  # noqa: BLE001 — job de fond : on capture tout
        log.exception("transcription échouée pour %s", note_id)
        db.update_note(note_id, status=db.STATUS_ERROR, error=f"Transcription : {exc}")
        return

    transcript_path = audio_path.parent / "transcript.txt"
    transcript_path.write_text(text, encoding="utf-8")
    db.update_note(
        note_id,
        status=db.STATUS_TRANSCRIBED,
        transcript_path=str(transcript_path.relative_to(BASE_DIR)),
        error=None,
    )
    log.info("note %s transcrite (%d caractères)", note_id, len(text))

    run_summary(note_id, text)


def run_summary(note_id: str, transcript: str | None = None) -> None:
    """Structure la transcription en titre + résumé + points clés + actions."""
    note = db.get_note(note_id)
    if not note:
        return
    if transcript is None:
        if not note["transcript_path"]:
            return
        transcript = (BASE_DIR / note["transcript_path"]).read_text(encoding="utf-8")

    audio_dir = (BASE_DIR / note["stored_path"]).parent

    if len(transcript.strip()) < _MIN_TRANSCRIPT_CHARS:
        summary = {
            "titre": "Audio trop court",
            "resume": "La transcription est vide ou trop courte pour être résumée.",
            "points_cles": [],
            "actions": [],
        }
    else:
        db.update_note(note_id, status=db.STATUS_SUMMARIZING, error=None)
        try:
            summary = ai.summarize(transcript)
        except Exception as exc:  # noqa: BLE001
            log.exception("résumé échoué pour %s", note_id)
            db.update_note(note_id, status=db.STATUS_ERROR, error=f"Résumé : {exc}")
            return

    (audio_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary_md = audio_dir / "summary.md"
    summary_md.write_text(ai.summary_to_markdown(summary), encoding="utf-8")

    db.update_note(
        note_id,
        status=db.STATUS_DONE,
        title=summary["titre"],
        summary_path=str(summary_md.relative_to(BASE_DIR)),
        error=None,
    )
    log.info("note %s résumée : %s", note_id, summary["titre"])

    run_archive(note_id)


def _files_to_archive(note: dict) -> tuple[list[Path], bool]:
    """Liste des fichiers à archiver + drapeau 'audio trop volumineux'."""
    settings = get_settings()
    audio_dir = (BASE_DIR / note["stored_path"]).parent
    audio_path = BASE_DIR / note["stored_path"]

    files = [p for p in (audio_dir / "transcript.txt", audio_dir / "summary.md",
                         audio_dir / "summary.json") if p.exists()]
    oversized = (
        audio_path.exists()
        and audio_path.stat().st_size > settings.large_file_threshold_bytes
    )
    if audio_path.exists() and not oversized:
        files.insert(0, audio_path)
    return files, oversized


def _archive_b2(note: dict, files: list[Path]) -> tuple[list[dict], str]:
    day = note["created_at"][:10]
    prefix = f"notes/{day}/{note['id']}"
    links = []
    for p in files:
        key = f"{prefix}/{p.name}"
        b2.upload(p, key)
        links.append({"provider": "b2", "name": p.name, "key": key})
    return links, ""


def _archive_drive(note: dict, files: list[Path]) -> tuple[list[dict], str]:
    settings = get_settings()
    root_id = drive.ensure_folder(settings.drive_root_folder)
    day = note["created_at"][:10]
    folder_name = f"{day} · {note.get('title') or note['id']}"[:120]
    folder_id = drive.ensure_folder(folder_name, parent_id=root_id)
    links = [
        {"provider": "gdrive", **drive.upload_file(p, parent_id=folder_id)}
        for p in files
    ]
    return links, drive.folder_link(folder_id)


def run_archive(note_id: str) -> None:
    """Archive les fichiers d'une note en ligne, puis déclenche l'email."""
    note = db.get_note(note_id)
    if not note:
        return

    backend = get_settings().effective_archive_backend
    files, audio_oversized = _files_to_archive(note)

    if backend == "none":
        log.info("note %s : aucun backend d'archivage configuré", note_id)
        db.update_note(
            note_id,
            status=db.STATUS_DONE,
            error="Archivage : aucun backend configuré (B2 ou Drive).",
        )
        run_email(note_id)
        return

    db.update_note(note_id, status=db.STATUS_ARCHIVING, error=None)
    try:
        if backend == "b2":
            links, folder_link = _archive_b2(note, files)
        else:
            links, folder_link = _archive_drive(note, files)
    except Exception as exc:  # noqa: BLE001
        # Archivage non bloquant : transcription et résumé restent accessibles,
        # l'email doit quand même partir. On signale juste le souci.
        log.warning("archivage %s (%s) échoué : %s", note_id, backend, exc)
        db.update_note(note_id, status=db.STATUS_DONE, error=f"Archivage : {exc}")
        run_email(note_id)
        return

    if audio_oversized:
        audio_name = Path(note["stored_path"]).name
        links.append({
            "provider": "mega",
            "name": audio_name,
            "note": "audio volumineux — archivage MEGA (étape 4b)",
        })

    db.update_note(
        note_id,
        status=db.STATUS_ARCHIVED,
        archive_links=json.dumps(links, ensure_ascii=False),
        archived_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        drive_folder_link=folder_link or None,
        error=None,
    )
    log.info("note %s archivée sur %s (%d fichiers)", note_id, backend, len(links))

    run_email(note_id)


def run_email(note_id: str) -> None:
    """Envoie l'email récapitulatif à l'adresse du propriétaire de la note."""
    note = db.get_note(note_id)
    if not note:
        return
    # On garde une éventuelle alerte d'archivage : l'email ne doit pas l'effacer.
    prior_error = note["error"]

    owner = db.get_user(note["user_id"]) if note.get("user_id") else None
    recipient = owner["email"] if owner else None

    if not get_settings().email_enabled or not recipient:
        db.update_note(
            note_id,
            error=prior_error or "Email : transport non configuré.",
        )
        log.info("note %s : email non envoyé (config/destinataire)", note_id)
        return

    resume_status = note["status"] if note["status"] != db.STATUS_SENDING else db.STATUS_ARCHIVED
    db.update_note(note_id, status=db.STATUS_SENDING)
    try:
        to = mailer.send_note_email(db.get_note(note_id), recipient=recipient)
    except Exception as exc:  # noqa: BLE001
        # L'email est la dernière étape, optionnelle : on n'échoue pas la note.
        log.warning("envoi email échoué pour %s : %s", note_id, exc)
        db.update_note(note_id, status=resume_status, error=f"Email : {exc}")
        return

    db.update_note(
        note_id,
        status=db.STATUS_SENT,
        emailed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        error=prior_error,
    )
    log.info("note %s : email envoyé à %s", note_id, to)

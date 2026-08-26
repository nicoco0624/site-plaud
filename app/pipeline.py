"""Traitements de fond déclenchés après l'upload (FastAPI BackgroundTasks).

Étape 2 : transcription.
Étape 3 : résumé, enchaîné automatiquement après une transcription réussie.
"""

import json
import logging

from app import ai, db
from app.config import BASE_DIR

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

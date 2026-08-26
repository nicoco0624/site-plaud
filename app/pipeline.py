"""Traitements de fond déclenchés après l'upload (FastAPI BackgroundTasks).

Étape 2 : transcription.
Étape 3 : résumé (viendra s'enchaîner ici).
"""

import logging

from app import ai, db
from app.config import BASE_DIR

log = logging.getLogger("plaud.pipeline")


def run_transcription(note_id: str) -> None:
    """Transcrit l'audio d'une note et écrit transcript.txt à côté de l'original."""
    note = db.get_note(note_id)
    if not note:
        log.warning("note %s introuvable, transcription annulée", note_id)
        return

    db.update_note(note_id, status=db.STATUS_TRANSCRIBING, error=None)
    audio_path = BASE_DIR / note["stored_path"]

    try:
        text = ai.transcribe(audio_path)
    except Exception as exc:  # noqa: BLE001 — on veut tout capturer côté job
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

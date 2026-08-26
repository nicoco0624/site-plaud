"""Appels à l'API Groq : transcription (étape 2) et résumé (étape 3)."""

from pathlib import Path

from groq import Groq

from app.config import get_settings


class GroqNotConfigured(RuntimeError):
    pass


def _client() -> Groq:
    settings = get_settings()
    if not settings.groq_api_key:
        raise GroqNotConfigured(
            "GROQ_API_KEY manquante dans .env — voir .env.example (étape 2)."
        )
    return Groq(api_key=settings.groq_api_key)


def transcribe(audio_path: Path) -> str:
    """Transcrit un fichier audio via Groq Whisper. Renvoie le texte brut."""
    settings = get_settings()
    client = _client()
    kwargs: dict = {
        "model": settings.groq_transcribe_model,
        "response_format": "text",
        "temperature": 0.0,
    }
    if settings.transcribe_language:
        kwargs["language"] = settings.transcribe_language

    with audio_path.open("rb") as fh:
        result = client.audio.transcriptions.create(
            file=(audio_path.name, fh.read()), **kwargs
        )
    # Avec response_format="text", le SDK renvoie directement une chaîne.
    text = result if isinstance(result, str) else getattr(result, "text", "")
    return text.strip()

"""Appels à l'API Groq : transcription (étape 2) et résumé (étape 3)."""

import json
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


_SUMMARY_SYSTEM = (
    "Tu es un assistant qui structure des notes vocales en français. "
    "À partir de la transcription fournie, produis un objet JSON STRICT avec "
    "exactement ces clés :\n"
    '  "titre"       : titre court et parlant (max 80 caractères)\n'
    '  "resume"      : synthèse de 3 à 6 phrases, en français\n'
    '  "points_cles" : tableau de 3 à 8 chaînes (les idées importantes)\n'
    '  "actions"     : tableau de chaînes (tâches / choses à faire mentionnées), '
    "[] s'il n'y en a pas\n"
    "Ne renvoie que le JSON, sans texte autour, sans bloc de code."
)

# Clés attendues dans la réponse -> valeur par défaut si absente.
_SUMMARY_KEYS = {"titre": "", "resume": "", "points_cles": [], "actions": []}


def summarize(transcript: str) -> dict:
    """Structure une transcription en {titre, resume, points_cles, actions}."""
    settings = get_settings()
    client = _client()

    resp = client.chat.completions.create(
        model=settings.groq_summary_model,
        messages=[
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {"role": "user", "content": transcript},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
        max_tokens=2000,
        reasoning_effort="low",
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"resume": raw.strip()}

    out: dict = {}
    for key, default in _SUMMARY_KEYS.items():
        val = data.get(key, default)
        if isinstance(default, list) and not isinstance(val, list):
            val = [str(val)] if val else []
        out[key] = val
    if not out["titre"]:
        out["titre"] = (out["resume"][:77] + "…") if out["resume"] else "Note sans titre"
    return out


def summary_to_markdown(s: dict) -> str:
    """Rend le dict de résumé en Markdown lisible (fichier summary.md)."""
    lines = [f"# {s['titre']}", "", s["resume"], ""]
    if s["points_cles"]:
        lines.append("## Points clés")
        lines += [f"- {p}" for p in s["points_cles"]]
        lines.append("")
    if s["actions"]:
        lines.append("## Actions")
        lines += [f"- [ ] {a}" for a in s["actions"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

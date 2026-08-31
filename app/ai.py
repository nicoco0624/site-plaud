"""Appels à l'API Groq : transcription (étape 2) et résumé (étape 3)."""

import json
import re
from pathlib import Path

import groq
from groq import Groq

from app.config import get_settings


def _lenient_json(raw: str) -> dict:
    """Parse un JSON éventuellement mal formé renvoyé par le modèle
    (doubles accolades, virgules traînantes, bloc ```json)."""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    for candidate in (
        s,
        re.sub(r"\}\}(\s*[,\]])", r"}\1", s),                    # }} -> } avant , ou ]
        re.sub(r",(\s*[}\]])", r"\1",
               re.sub(r"\}\}(\s*[,\]])", r"}\1", s)),            # + virgules traînantes
    ):
        try:
            out = json.loads(candidate)
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            continue
    return {}


def _chat_json(client: Groq, **kwargs) -> dict:
    """Appel chat en mode JSON, tolérant : réessaie une fois, et récupère la
    tentative du modèle si Groq rejette pour JSON invalide."""
    for attempt in (1, 2):
        try:
            resp = client.chat.completions.create(**kwargs)
            return _lenient_json(resp.choices[0].message.content or "{}")
        except groq.BadRequestError as exc:
            body = getattr(exc, "body", None) or {}
            failed = (body.get("error") or {}).get("failed_generation")
            if failed:
                data = _lenient_json(failed)
                if data:
                    return data
            if attempt == 2:
                raise
    return {}


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
    # Titre sur une seule ligne, borné : sert d'objet d'email (anti-injection
    # d'en-têtes) et de titre affiché.
    out["titre"] = re.sub(r"\s+", " ", str(out["titre"])).strip()[:120] or "Note"
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


# --------------------------------------------------------------------------- #
#  Fiche de révision à partir d'une transcription de vidéo YouTube
# --------------------------------------------------------------------------- #

_STUDY_SYSTEM = (
    "Tu es un assistant pédagogique. À partir de la transcription d'une vidéo "
    "(fournie par l'utilisateur), tu produis une FICHE DE RÉVISION en français, "
    "sous forme d'un objet JSON STRICT avec exactement ces clés :\n"
    '  "titre"    : titre clair de la fiche (max 100 caractères)\n'
    '  "sections" : tableau de 3 à 7 objets {"titre": str, "points": [str, ...]} '
    "— chaque section couvre une partie du contenu, 2 à 6 points par section, "
    "formulés comme des idées à retenir\n"
    '  "concepts" : tableau de 3 à 10 objets {"terme": str, "definition": str} '
    "— les notions/définitions importantes du contenu\n"
    '  "questions": tableau de 3 à 8 questions de révision (chaînes) portant sur '
    "le contenu, sans les réponses\n"
    "Ne renvoie que le JSON, sans texte autour, sans bloc de code. "
    "Si la transcription est trop pauvre pour une fiche, fais au mieux avec "
    "ce qui est disponible."
)


def build_study_sheet(title: str, transcript: str) -> dict:
    """Transforme une transcription de vidéo en fiche de révision structurée :
    {titre, sections:[{titre,points}], concepts:[{terme,definition}], questions}."""
    settings = get_settings()
    client = _client()

    user_msg = f"Titre de la vidéo : {title}\n\nTranscription :\n{transcript}"
    data = _chat_json(
        client,
        model=settings.groq_summary_model,
        messages=[
            {"role": "system", "content": _STUDY_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
        max_tokens=3200,
        reasoning_effort="low",
    )

    out: dict = {
        "titre": re.sub(r"\s+", " ", str(data.get("titre") or title)).strip()[:140]
                 or "Fiche de révision",
        "sections": [],
        "concepts": [],
        "questions": [],
    }
    for sec in data.get("sections") or []:
        if isinstance(sec, dict):
            pts = [str(p).strip() for p in (sec.get("points") or []) if str(p).strip()]
            st = str(sec.get("titre") or "").strip()
            if st or pts:
                out["sections"].append({"titre": st or "Section", "points": pts})
    for c in data.get("concepts") or []:
        if isinstance(c, dict):
            terme = str(c.get("terme") or "").strip()
            deff = str(c.get("definition") or "").strip()
            if terme and deff:
                out["concepts"].append({"terme": terme, "definition": deff})
    out["questions"] = [str(q).strip() for q in (data.get("questions") or []) if str(q).strip()]
    return out

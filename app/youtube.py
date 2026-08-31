"""Récupération du titre et de la transcription d'une vidéo YouTube à partir de
son URL, sans télécharger la vidéo.

Deux méthodes, dans l'ordre :
  1. youtube-transcript-api  (léger, rapide quand ça passe)
  2. yt-dlp                   (plus résistant au blocage, récupère aussi la durée)

Le titre vient de l'API oEmbed publique de YouTube (fiable, sans clé).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from app.config import get_settings

_ID_RE = re.compile(
    r"(?:v=|/shorts/|/embed/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})"
)
_LANGS = ["fr", "fr-FR", "fr-CA", "en", "en-US", "en-GB"]
# Au-delà, on refuse (transcription ~ 900 caractères / minute de parole).
MAX_TRANSCRIPT_CHARS = 260_000


class InvalidURL(Exception):
    pass


class NoTranscript(Exception):
    pass


class VideoTooLong(Exception):
    def __init__(self, approx_minutes: int):
        super().__init__(f"Vidéo trop longue (~{approx_minutes} min).")
        self.approx_minutes = approx_minutes


class FetchError(Exception):
    pass


def video_id(url: str) -> str | None:
    s = (url or "").strip()
    m = _ID_RE.search(s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    return None


def _oembed_title(url: str) -> str | None:
    api = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
    try:
        with urllib.request.urlopen(api, timeout=15) as r:
            return json.load(r).get("title")
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
#  Méthode 0 : Supadata (API tierce) — indispensable en prod car YouTube bloque
#  les requêtes directes depuis les IP de datacenter (Render).
# --------------------------------------------------------------------------- #

def _via_supadata(url: str, api_key: str) -> str:
    endpoint = ("https://api.supadata.ai/v1/transcript?text=true&url="
                + urllib.parse.quote(url, safe=""))
    req = urllib.request.Request(endpoint, headers={"x-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.load(r)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        low = body.lower()
        if exc.code in (404, 422) or "no transcript" in low or "not found" in low:
            raise NoTranscript() from exc
        if exc.code in (401, 403):
            raise FetchError("clé Supadata invalide") from exc
        raise FetchError(f"Supadata HTTP {exc.code} : {body[:200]}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"Supadata injoignable : {exc.reason}") from exc

    content = data.get("content")
    if isinstance(content, list):
        content = " ".join(
            seg.get("text", "") for seg in content if isinstance(seg, dict)
        )
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if len(text) < 40:
        raise NoTranscript()
    return text


# --------------------------------------------------------------------------- #
#  Méthode 1 : youtube-transcript-api
# --------------------------------------------------------------------------- #

def _via_transcript_api(vid: str) -> str:
    from youtube_transcript_api import (
        IpBlocked,
        NoTranscriptFound,
        RequestBlocked,
        TranscriptsDisabled,
        VideoUnavailable,
        YouTubeTranscriptApi,
    )

    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(vid, languages=_LANGS)
    except (NoTranscriptFound, TranscriptsDisabled):
        # dernier recours : n'importe quelle langue disponible
        try:
            tlist = api.list(vid)
            transcript = next(iter(tlist))
            fetched = transcript.fetch()
        except Exception as exc:  # noqa: BLE001
            raise NoTranscript() from exc
    except VideoUnavailable as exc:
        raise InvalidURL() from exc
    except (RequestBlocked, IpBlocked) as exc:
        raise FetchError("YouTube a bloqué la requête (transcript-api)") from exc
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"transcript-api : {exc}") from exc

    text = " ".join(snip.text.strip() for snip in fetched if snip.text.strip())
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
#  Méthode 2 : yt-dlp
# --------------------------------------------------------------------------- #

def _pick_caption_url(info: dict) -> tuple[str, str] | None:
    """(url, ext) d'une piste de sous-titres, sous-titres manuels d'abord."""
    for src_key in ("subtitles", "automatic_captions"):
        src = info.get(src_key) or {}
        for lang in _LANGS + [k for k in src if k not in _LANGS]:
            for track in src.get(lang, []):
                if track.get("ext") == "json3" and track.get("url"):
                    return track["url"], "json3"
            for track in src.get(lang, []):
                if track.get("ext") in ("vtt", "srv1", "srv3") and track.get("url"):
                    return track["url"], track["ext"]
    return None


def _parse_json3(raw: str) -> str:
    data = json.loads(raw)
    parts = []
    for ev in data.get("events", []):
        for seg in ev.get("segs") or []:
            t = seg.get("utf8", "")
            if t and t != "\n":
                parts.append(t)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _parse_vtt(raw: str) -> str:
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if (not line or "-->" in line or line.isdigit()
                or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE"))):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line and (not out or out[-1] != line):
            out.append(line)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def _via_ytdlp(url: str, max_minutes: int) -> tuple[str, int]:
    import yt_dlp

    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc).lower()
        if any(k in msg for k in ("private", "unavailable", "does not exist",
                                  "removed", "not a valid url")):
            raise InvalidURL() from exc
        raise FetchError(f"yt-dlp : {str(exc)[:180]}") from exc

    dur = int(info.get("duration") or 0)
    if dur and dur > max_minutes * 60:
        raise VideoTooLong(round(dur / 60))

    picked = _pick_caption_url(info)
    if not picked:
        raise NoTranscript()
    cap_url, ext = picked
    try:
        with urllib.request.urlopen(cap_url, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"sous-titres injoignables : {exc}") from exc

    text = _parse_json3(raw) if (ext == "json3" or raw.lstrip().startswith("{")) else _parse_vtt(raw)
    return text, dur


# --------------------------------------------------------------------------- #
#  API publique
# --------------------------------------------------------------------------- #

def fetch(url: str, max_minutes: int = 90) -> dict:
    """Renvoie {title, transcript, duration_s, video_id}. Lève InvalidURL /
    NoTranscript / VideoTooLong / FetchError avec des messages exploitables."""
    vid = video_id(url)
    if not vid:
        raise InvalidURL()

    title = _oembed_title(url) or "Vidéo YouTube"
    transcript, dur = "", 0
    key = get_settings().supadata_api_key

    # 0) Supadata (si configuré) — seule méthode qui passe depuis un datacenter
    if key:
        try:
            transcript = _via_supadata(url, key)
        except (NoTranscript, FetchError):
            pass

    # 1) youtube-transcript-api
    if len(transcript) < 40:
        try:
            transcript = _via_transcript_api(vid)
        except (NoTranscript, FetchError):
            pass
        except InvalidURL:
            raise

    # 2) yt-dlp (fallback, ou pour la durée)
    if len(transcript) < 40:
        transcript, dur = _via_ytdlp(url, max_minutes)

    transcript = transcript.strip()
    if len(transcript) < 40:
        raise NoTranscript()
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise VideoTooLong(round(len(transcript) / 900))

    return {"title": title, "transcript": transcript,
            "duration_s": dur, "video_id": vid}

"""Envoi de l'email récapitulatif d'une note.

Transports (par préférence) :
  - Brevo (API HTTPS)  : 300 mails/jour gratuits, vers n'importe quelle adresse
    dès qu'un expéditeur unique est vérifié. Transport visé en prod.
  - Resend (API HTTPS) : sans domaine vérifié, n'envoie que vers l'adresse du
    compte Resend -> ne convient pas pour écrire au client.
  - SMTP               : bloqué en sortie par Render -> usage local uniquement.
"""

from __future__ import annotations

import base64
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from html import escape
from pathlib import Path

from app import b2
from app.config import BASE_DIR, get_settings

_TRANSCRIPT_MAX = 6000  # caractères inclus dans l'email


class EmailNotConfigured(RuntimeError):
    pass


class EmailSendError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
#  Chargement du contenu
# --------------------------------------------------------------------------- #

def load_summary(note: dict) -> dict:
    if note.get("summary_path"):
        js = (BASE_DIR / note["summary_path"]).with_suffix(".json")
        if js.exists():
            try:
                return json.loads(js.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
    return {"titre": note.get("title") or "Note", "resume": "",
            "points_cles": [], "actions": []}


def load_transcript(note: dict) -> str:
    rel = note.get("transcript_path")
    if rel:
        p = BASE_DIR / rel
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8")
            except OSError:
                pass
    for e in json.loads(note.get("archive_links") or "[]"):
        if (e.get("name") == "transcript.txt" and e.get("provider") == "b2"
                and e.get("key")):
            try:
                return b2.fetch_text(e["key"])
            except Exception:  # noqa: BLE001
                return ""
    return ""


def download_links(note: dict) -> list[tuple[str, str]]:
    base = get_settings().public_base_url.rstrip("/")
    out: list[tuple[str, str]] = []
    for entry in json.loads(note.get("archive_links") or "[]"):
        name = entry.get("name")
        if not name:
            continue
        if base:
            out.append((name, f"{base}/notes/{note['id']}/dl/{name}"))
        elif entry.get("link"):
            out.append((name, entry["link"]))
    return out


# --------------------------------------------------------------------------- #
#  Rendu : texte + HTML responsive (identité visuelle du site)
# --------------------------------------------------------------------------- #

_GRAD = "linear-gradient(180deg,#1c1c1f 0%,#141416 100%)"
_LABEL = ("margin:26px 0 8px;font:700 11px/1 -apple-system,Segoe UI,Roboto,"
          "Arial,sans-serif;letter-spacing:2px;text-transform:uppercase;"
          "color:#9a7b1e;")


def _plain_text(note: dict, s: dict, transcript: str, links) -> str:
    out = [s.get("titre", "Note"), "=" * len(s.get("titre", "Note")), ""]
    if s.get("resume"):
        out += ["RÉSUMÉ", s["resume"], ""]
    if s.get("points_cles"):
        out += ["POINTS CLÉS"] + [f"  - {p}" for p in s["points_cles"]] + [""]
    if s.get("actions"):
        out += ["ACTIONS"] + [f"  - [ ] {a}" for a in s["actions"]] + [""]
    if transcript:
        out += ["TRANSCRIPTION", transcript, ""]
    if links:
        out += ["FICHIERS"] + [f"  - {n} : {u}" for n, u in links] + [""]
    out += ["—", f"Traité automatiquement par {get_settings().app_name} · {note.get('original_filename', '')}"]
    return "\n".join(out).strip() + "\n"


def _html(note: dict, s: dict, transcript: str, links) -> str:
    titre = escape(s.get("titre", "Note"))
    resume = escape(s.get("resume", "")).replace("\n", "<br>")

    def bullets(items, box=False):
        rows = ""
        for it in items:
            mark = "☐&nbsp;" if box else (
                '<span style="color:#9a7b1e;">•</span>&nbsp;')
            rows += (
                f'<tr><td style="padding:5px 0;font:400 15px/1.5 -apple-system,'
                f'Segoe UI,Roboto,Arial,sans-serif;color:#2b2721;">{mark}'
                f'{escape(str(it))}</td></tr>'
            )
        return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>'

    parts = []
    if resume:
        parts.append(
            f'<p style="{_LABEL}">Résumé</p>'
            f'<p style="margin:0;font:400 16px/1.6 -apple-system,Segoe UI,Roboto,'
            f'Arial,sans-serif;color:#241f18;">{resume}</p>'
        )
    if s.get("points_cles"):
        parts.append(f'<p style="{_LABEL}">Points clés</p>' + bullets(s["points_cles"]))
    if s.get("actions"):
        parts.append(f'<p style="{_LABEL}">Actions</p>' + bullets(s["actions"], box=True))

    if transcript:
        t = transcript.strip()
        truncated = len(t) > _TRANSCRIPT_MAX
        body = escape(t[:_TRANSCRIPT_MAX]).replace("\n", "<br>")
        if truncated:
            body += '<br><span style="color:#8a8374;">… (tronqué — texte complet dans l\'application)</span>'
        parts.append(
            f'<p style="{_LABEL}">Transcription</p>'
            f'<div style="background:#faf7ef;border:1px solid #ece3cd;border-radius:14px;'
            f'padding:16px 18px;font:400 14px/1.6 -apple-system,Segoe UI,Roboto,Arial,'
            f'sans-serif;color:#443d30;">{body}</div>'
        )

    if links:
        chips = ""
        for n, u in links:
            chips += (
                f'<a href="{escape(u)}" style="display:inline-block;margin:4px 6px 4px 0;'
                f'padding:8px 14px;border-radius:999px;background:#f7f2e3;color:#8a6d16;'
                f'font:600 13px/1 -apple-system,Segoe UI,Roboto,Arial,sans-serif;'
                f'text-decoration:none;">↓ {escape(n)}</a>'
            )
        parts.append(f'<p style="{_LABEL}">Fichiers</p><div>{chips}</div>')

    inner = "".join(parts)
    orig = escape(note.get("original_filename", ""))

    F = "-apple-system,Segoe UI,Roboto,Arial,sans-serif"
    return (
        '<!doctype html><html lang="fr"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light">'
        f"<title>{titre}</title></head>"
        '<body style="margin:0;padding:0;background:#f2f0ea;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f0ea;">'
        '<tr><td align="center" style="padding:28px 14px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;border-collapse:separate;">'
        f'<tr><td style="background:#161618;background-image:{_GRAD};border-radius:20px 20px 0 0;border-bottom:3px solid #d4af37;padding:34px 30px 30px;">'
        f'<div style="font:800 12px/1 {F};letter-spacing:4px;color:#d4af37;">{get_settings().app_name.upper()}</div>'
        f'<h1 style="margin:12px 0 0;font:800 24px/1.3 {F};color:#ffffff;">{titre}</h1>'
        '</td></tr>'
        '<tr><td style="background:#ffffff;border-radius:0 0 20px 20px;padding:26px 30px 30px;box-shadow:0 20px 50px -24px rgba(0,0,0,.16);">'
        f'{inner}'
        '</td></tr>'
        f'<tr><td style="padding:18px 12px 4px;text-align:center;font:400 12px/1.6 {F};color:#9a917d;">'
        f'Trait&eacute; automatiquement par {escape(get_settings().app_name)} &middot; {orig}'
        '</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def _render(note: dict) -> tuple[str, str, str]:
    s = load_summary(note)
    transcript = load_transcript(note)
    links = download_links(note)
    subject = f"🎙️ {s.get('titre', 'Note')}"
    return subject, _plain_text(note, s, transcript, links), _html(note, s, transcript, links)


def _attachments(note: dict) -> list[tuple[str, bytes]]:
    """Pièces jointes : le transcript en .txt. Le résumé est déjà dans le corps ;
    on ne joint pas summary.md (extension refusée par Brevo)."""
    out = []
    rel = note.get("transcript_path")
    if rel and (BASE_DIR / rel).exists():
        out.append(("transcription.txt", (BASE_DIR / rel).read_bytes()))
    return out


# --------------------------------------------------------------------------- #
#  Transports
# --------------------------------------------------------------------------- #

def _post_json(url: str, headers: dict, payload: dict) -> None:
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "Veyra/1.0", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise EmailSendError(f"{url.split('/')[2]} HTTP {e.code} : "
                             f"{e.read().decode('utf-8', 'replace')[:400]}") from e
    except urllib.error.URLError as e:
        raise EmailSendError(f"{url.split('/')[2]} injoignable : {e.reason}") from e


def _send_brevo(st, to, subject, text, html, atts) -> str:
    payload = {
        "sender": {"name": st.mail_from_name, "email": st.brevo_sender},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
        "textContent": text,
    }
    if atts:
        payload["attachment"] = [
            {"name": n, "content": base64.b64encode(d).decode()} for n, d in atts
        ]
    _post_json("https://api.brevo.com/v3/smtp/email", {"api-key": st.brevo_api_key}, payload)
    return to


def _send_resend(st, to, subject, text, html, atts) -> str:
    payload = {"from": st.effective_mail_from, "to": [to], "subject": subject,
               "text": text, "html": html}
    if atts:
        payload["attachments"] = [
            {"filename": n, "content": base64.b64encode(d).decode()} for n, d in atts
        ]
    _post_json("https://api.resend.com/emails",
               {"Authorization": f"Bearer {st.resend_api_key}"}, payload)
    return to


def _send_smtp(st, to, subject, text, html, atts) -> str:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = st.effective_mail_from
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for n, d in atts:
        msg.add_attachment(d, maintype="text", subtype="plain", filename=n)
    try:
        with smtplib.SMTP(st.smtp_host, st.smtp_port, timeout=30) as srv:
            srv.starttls(context=ssl.create_default_context())
            srv.login(st.smtp_user, st.smtp_password)
            srv.send_message(msg)
    except OSError as e:
        raise EmailSendError(f"SMTP {st.smtp_host}:{st.smtp_port} : {e}") from e
    return to


_SENDERS = {"brevo": _send_brevo, "resend": _send_resend, "smtp": _send_smtp}


def _dispatch(to: str, subject: str, text: str, html: str, atts=None) -> str:
    st = get_settings()
    to = (to or "").strip()
    provider = st.email_provider
    if provider == "none":
        raise EmailNotConfigured("aucun transport email configuré "
                                 "(BREVO_API_KEY+BREVO_SENDER, RESEND_API_KEY, ou SMTP_*)")
    if not to:
        raise EmailNotConfigured("aucun destinataire")
    return _SENDERS[provider](st, to, subject, text, html, atts or [])


def send_note_email(note: dict, recipient: str | None = None) -> str:
    """Envoie le récap d'une note. Lève EmailNotConfigured / EmailSendError."""
    to = (recipient or get_settings().mail_to or "")
    subject, text, html = _render(note)
    return _dispatch(to, subject, text, html, _attachments(note))


def _simple_shell(title: str, inner: str) -> str:
    F = "-apple-system,Segoe UI,Roboto,Arial,sans-serif"
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light">'
        f"<title>{escape(title)}</title></head>"
        '<body style="margin:0;padding:0;background:#f2f0ea;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f0ea;">'
        '<tr><td align="center" style="padding:28px 14px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:520px;">'
        f'<tr><td style="background:#161618;background-image:{_GRAD};border-radius:20px 20px 0 0;border-bottom:3px solid #d4af37;padding:30px;">'
        f'<div style="font:800 12px/1 {F};letter-spacing:4px;color:#d4af37;">{get_settings().app_name.upper()}</div>'
        f'<h1 style="margin:10px 0 0;font:800 22px/1.3 {F};color:#fff;">{escape(title)}</h1></td></tr>'
        f'<tr><td style="background:#fff;border-radius:0 0 20px 20px;padding:28px 30px;font:400 15px/1.6 {F};color:#2b2721;">'
        f'{inner}</td></tr></table></td></tr></table></body></html>'
    )


def send_reset_email(to: str, link: str) -> str:
    """Envoie le lien de réinitialisation de mot de passe."""
    btn = (
        f'<a href="{escape(link)}" style="display:inline-block;margin:18px 0;'
        f'padding:13px 26px;border-radius:12px;background:#8a6d16;color:#fff;'
        f'font-weight:700;text-decoration:none;">Choisir un nouveau mot de passe</a>'
    )
    inner = (
        "<p>Tu as demandé à réinitialiser ton mot de passe. Ce lien est valable "
        "1 heure&nbsp;:</p>"
        f"{btn}"
        f'<p style="font-size:13px;color:#8a8374;word-break:break-all;">{escape(link)}</p>'
        "<p style=\"font-size:13px;color:#8a8374;\">Si tu n'es pas à l'origine de "
        "cette demande, ignore cet email.</p>"
    )
    text = f"Réinitialise ton mot de passe (valable 1h) : {link}"
    return _dispatch(to, f"Réinitialisation de ton mot de passe — {get_settings().app_name}",
                     text, _simple_shell("Mot de passe oublié", inner))


def send_verification_email(to: str, link: str) -> str:
    """Envoie le lien de confirmation d'adresse email (à l'inscription)."""
    app = get_settings().app_name
    btn = (
        f'<a href="{escape(link)}" style="display:inline-block;margin:18px 0;'
        f'padding:13px 26px;border-radius:12px;background:#8a6d16;color:#fff;'
        f'font-weight:700;text-decoration:none;">Confirmer mon adresse</a>'
    )
    inner = (
        f"<p>Bienvenue sur {escape(app)}&nbsp;! Confirme ton adresse email pour "
        "activer ton compte. Ce lien est valable 7&nbsp;jours&nbsp;:</p>"
        f"{btn}"
        f'<p style="font-size:13px;color:#8a8374;word-break:break-all;">{escape(link)}</p>'
        "<p style=\"font-size:13px;color:#8a8374;\">Si tu n'es pas à l'origine de "
        "cette inscription, ignore cet email.</p>"
    )
    text = f"Confirme ton adresse pour activer ton compte {app} (valable 7 jours) : {link}"
    return _dispatch(to, f"Confirme ton adresse — {app}",
                     text, _simple_shell("Confirmation d'adresse", inner))


def send_video_sheet(to: str, video_url: str, video_title: str, sheet: dict) -> str:
    """Envoie la fiche de révision d'une vidéo + le lien de la vidéo."""
    F = "-apple-system,Segoe UI,Roboto,Arial,sans-serif"
    LBL = (f"margin:24px 0 6px;font:700 11px/1 {F};letter-spacing:2px;"
           "text-transform:uppercase;color:#9a7b1e;")

    parts = [
        f'<p style="margin:0 0 4px;"><a href="{escape(video_url)}" '
        f'style="color:#8a6d16;font-weight:600;">▶ {escape(video_title)}</a></p>'
    ]
    for sec in sheet.get("sections") or []:
        parts.append(f'<p style="{LBL}">{escape(sec.get("titre", ""))}</p>')
        pts = "".join(
            f'<li style="margin:5px 0;">{escape(str(p))}</li>'
            for p in sec.get("points") or []
        )
        if pts:
            parts.append(f'<ul style="margin:0;padding-left:20px;">{pts}</ul>')
    if sheet.get("concepts"):
        parts.append(f'<p style="{LBL}">Concepts clés</p>')
        for c in sheet["concepts"]:
            parts.append(
                f'<p style="margin:8px 0 0;"><strong>{escape(c.get("terme",""))}</strong>'
                f' — {escape(c.get("definition",""))}</p>'
            )
    if sheet.get("questions"):
        parts.append(f'<p style="{LBL}">Questions de révision</p>')
        qs = "".join(
            f'<li style="margin:6px 0;">{escape(str(q))}</li>' for q in sheet["questions"]
        )
        parts.append(f'<ol style="margin:0;padding-left:20px;">{qs}</ol>')

    html = _simple_shell(sheet.get("titre") or "Fiche de révision", "".join(parts))

    txt = [sheet.get("titre") or "Fiche de révision", "", f"Vidéo : {video_url}", ""]
    for sec in sheet.get("sections") or []:
        txt.append(sec.get("titre", "").upper())
        txt += [f"  - {p}" for p in sec.get("points") or []]
        txt.append("")
    if sheet.get("concepts"):
        txt.append("CONCEPTS CLÉS")
        txt += [f"  - {c.get('terme','')} : {c.get('definition','')}" for c in sheet["concepts"]]
        txt.append("")
    if sheet.get("questions"):
        txt.append("QUESTIONS DE RÉVISION")
        txt += [f"  {i}. {q}" for i, q in enumerate(sheet["questions"], 1)]
    text = "\n".join(txt).strip() + "\n"

    subject = f"🎬 {sheet.get('titre') or 'Fiche de révision'}"
    return _dispatch(to, subject, text, html)

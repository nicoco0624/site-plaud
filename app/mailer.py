"""Envoi de l'email récapitulatif d'une note (étape 5).

Deux transports, choisis dans la config :
  - Resend (API HTTPS)  -> fonctionne partout, y compris sur Render où le SMTP
    sortant est bloqué. Transport par défaut en ligne.
  - SMTP Gmail          -> pratique en local (mot de passe d'application).
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

from app.config import BASE_DIR, get_settings


class EmailNotConfigured(RuntimeError):
    pass


def _load_summary(note: dict) -> dict:
    if note.get("summary_path"):
        sp = BASE_DIR / note["summary_path"]
        js = sp.with_suffix(".json")
        if js.exists():
            return json.loads(js.read_text(encoding="utf-8"))
    return {"titre": note.get("title") or "Note", "resume": "", "points_cles": [],
            "actions": []}


def _archive_links(note: dict) -> list[tuple[str, str]]:
    """(libellé, url) pour chaque fichier archivé, si une URL publique est connue."""
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


def _bodies(note: dict, summary: dict) -> tuple[str, str]:
    """Construit (texte brut, HTML) de l'email."""
    pts = summary.get("points_cles") or []
    acts = summary.get("actions") or []
    drive = note.get("drive_folder_link") or ""
    files = _archive_links(note)

    lines = [summary.get("resume", ""), ""]
    if pts:
        lines.append("Points clés :")
        lines += [f"  - {p}" for p in pts]
        lines.append("")
    if acts:
        lines.append("Actions :")
        lines += [f"  - [ ] {a}" for a in acts]
        lines.append("")
    if drive:
        lines.append(f"Dossier Drive : {drive}")
    if files:
        lines.append("Fichiers archivés :")
        lines += [f"  - {name} : {url}" for name, url in files]
    lines += ["", f"Fichier d'origine : {note.get('original_filename', '')}"]
    text = "\n".join(lines).strip() + "\n"

    def ul(items, prefix=""):
        return "<ul>" + "".join(f"<li>{prefix}{escape(str(i))}</li>" for i in items) + "</ul>"

    html = [f"<h2>{escape(summary.get('titre', 'Note'))}</h2>"]
    if summary.get("resume"):
        html.append(f"<p>{escape(summary['resume'])}</p>")
    if pts:
        html.append("<h3>Points clés</h3>" + ul(pts))
    if acts:
        html.append("<h3>Actions</h3>" + ul(acts, prefix="☐ "))
    if drive:
        html.append(f'<p><a href="{escape(drive)}">Ouvrir le dossier Drive</a></p>')
    if files:
        html.append(
            "<h3>Fichiers archivés</h3><ul>"
            + "".join(
                f'<li><a href="{escape(url)}">{escape(name)}</a></li>'
                for name, url in files
            )
            + "</ul>"
        )
    html.append(
        f"<p style='color:#888;font-size:12px'>Fichier d'origine : "
        f"{escape(note.get('original_filename', ''))}</p>"
    )
    return text, "\n".join(html)


def _attachments(note: dict) -> list[tuple[str, bytes]]:
    out = []
    for key in ("transcript_path", "summary_path"):
        rel = note.get(key)
        if rel and (BASE_DIR / rel).exists():
            p = BASE_DIR / rel
            out.append((p.name, p.read_bytes()))
    return out


def _send_resend(settings, to, subject, text, html, atts) -> str:
    payload = {
        "from": settings.effective_mail_from,
        "to": [to],
        "subject": subject,
        "text": text,
        "html": f"<!doctype html><html><body>{html}</body></html>",
    }
    if atts:
        payload["attachments"] = [
            {"filename": name, "content": base64.b64encode(data).decode()}
            for name, data in atts
        ]
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        method="POST",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
            # Sans User-Agent réaliste, Cloudflare devant l'API renvoie 403 (1010).
            "User-Agent": "SitePlaud/1.0 (+https://github.com/nicoco0624/site-plaud)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            json.load(r)
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        raise RuntimeError(f"Resend {e.code}: {e.read().decode()[:200]}") from e
    return to


def _send_smtp(settings, to, subject, text, html, atts) -> str:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.effective_mail_from
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(f"<!doctype html><html><body>{html}</body></html>", subtype="html")
    for name, data in atts:
        msg.add_attachment(data, maintype="text", subtype="plain", filename=name)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
    return to


def send_note_email(note: dict, recipient: str | None = None) -> str:
    """Envoie le récap de la note. `recipient` prime sur MAIL_TO ; renvoie l'adresse."""
    settings = get_settings()
    to = recipient or settings.mail_to
    if not settings.email_enabled or not to:
        raise EmailNotConfigured(
            "Aucun transport email (RESEND_API_KEY ou SMTP_*) ou pas de destinataire."
        )

    summary = _load_summary(note)
    text, html = _bodies(note, summary)
    subject = f"[Plaud] {summary.get('titre', 'Note')}"
    atts = _attachments(note)

    if settings.email_provider == "resend":
        return _send_resend(settings, to, subject, text, html, atts)
    return _send_smtp(settings, to, subject, text, html, atts)

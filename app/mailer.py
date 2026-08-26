"""Envoi de l'email récapitulatif d'une note (étape 5).

Transport : SMTP Gmail (smtp.gmail.com:587, STARTTLS) avec un **mot de passe
d'application** (nécessite la validation en 2 étapes sur le compte Google).
Gratuit, ~500 destinataires/jour.
"""

from __future__ import annotations

import json
import smtplib
import ssl
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


def send_note_email(note: dict) -> str:
    """Envoie le récap de la note. Renvoie l'adresse destinataire."""
    settings = get_settings()
    if not settings.email_enabled:
        raise EmailNotConfigured(
            "SMTP_USER / SMTP_PASSWORD / MAIL_TO manquants dans .env (étape 5)."
        )

    summary = _load_summary(note)
    text, html = _bodies(note, summary)

    msg = EmailMessage()
    msg["Subject"] = f"[Plaud] {summary.get('titre', 'Note')}"
    msg["From"] = settings.effective_mail_from
    msg["To"] = settings.mail_to
    msg.set_content(text)
    msg.add_alternative(
        f"<!doctype html><html><body>{html}</body></html>", subtype="html"
    )

    # Pièces jointes : transcription + résumé Markdown (petits fichiers texte).
    for key in ("transcript_path", "summary_path"):
        rel = note.get(key)
        if rel and (BASE_DIR / rel).exists():
            p = BASE_DIR / rel
            msg.add_attachment(
                p.read_bytes(), maintype="text", subtype="plain", filename=p.name
            )

    ctx = ssl.create_default_context()
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
    return settings.mail_to

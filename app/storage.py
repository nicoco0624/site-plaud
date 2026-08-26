"""Stockage local des fichiers uploadés.

Arborescence : uploads/{AAAA-MM-JJ}/{id-note}/original.{ext}
Le chemin renvoyé est relatif à la racine du projet (pratique pour la base et
pour un futur portage vers un stockage distant).
"""

from datetime import date
from pathlib import Path

from fastapi import UploadFile

from app.config import BASE_DIR, get_settings

# Formats audio acceptés (extension -> type MIME indicatif).
ALLOWED_EXTENSIONS = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}

# Taille des morceaux lus pendant l'écriture sur disque.
_CHUNK = 1024 * 1024


class UploadTooLarge(Exception):
    def __init__(self, limit_mb: int):
        super().__init__(f"Fichier trop volumineux (limite : {limit_mb} Mo).")
        self.limit_mb = limit_mb


class UnsupportedFormat(Exception):
    def __init__(self, ext: str):
        super().__init__(f"Format non pris en charge : {ext or '(inconnu)'}.")
        self.ext = ext


def extension_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


async def save_upload(upload: UploadFile, note_id: str) -> tuple[Path, int]:
    """Écrit le fichier sur disque en streaming. Renvoie (chemin_relatif, taille)."""
    settings = get_settings()
    ext = extension_of(upload.filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedFormat(ext)

    rel_dir = Path(settings.upload_dir) / date.today().isoformat() / note_id
    abs_dir = BASE_DIR / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)

    rel_path = rel_dir / f"original{ext}"
    abs_path = BASE_DIR / rel_path

    size = 0
    try:
        with abs_path.open("wb") as out:
            while chunk := await upload.read(_CHUNK):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise UploadTooLarge(settings.max_upload_mb)
                out.write(chunk)
    except Exception:
        abs_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    return rel_path, size

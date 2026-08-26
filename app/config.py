"""Configuration de l'application, lue depuis l'environnement / le fichier .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Racine du projet (dossier qui contient ce paquet "app").
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Site Plaud"
    upload_dir: str = "uploads"
    database_url: str = "sqlite:///data/plaud.db"
    max_upload_mb: int = 200

    # --- Groq (étapes 2 & 3) ---
    groq_api_key: str = ""
    groq_transcribe_model: str = "whisper-large-v3-turbo"
    groq_summary_model: str = "openai/gpt-oss-120b"
    # Langue attendue des audios (ISO-639-1). "" = détection automatique.
    transcribe_language: str = "fr"

    @property
    def upload_path(self) -> Path:
        p = Path(self.upload_dir)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def sqlite_path(self) -> Path:
        """Chemin du fichier SQLite déduit de database_url (schéma sqlite:/// en dev)."""
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError(
                "En dev, DATABASE_URL doit commencer par 'sqlite:///'. "
                "La bascule Turso arrive à l'étape 6."
            )
        raw = self.database_url[len(prefix):]
        p = Path(raw)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()

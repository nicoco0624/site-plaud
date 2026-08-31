"""Configuration de l'application, lue depuis l'environnement / le fichier .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Racine du projet (dossier qui contient ce paquet "app").
BASE_DIR = Path(__file__).resolve().parent.parent

_DEFAULT_SECRET_KEY = "dev-insecure-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Veyra"
    app_tagline: str = "Une nouvelle ère de la voix."
    upload_dir: str = "uploads"
    # sqlite:///chemin  -> fichier local (dev)
    # libsql://xxx.turso.io ou https://xxx.turso.io -> Turso (+ turso_auth_token)
    database_url: str = "sqlite:///data/plaud.db"
    turso_auth_token: str = ""
    max_upload_mb: int = 50

    # Sécurité / garde-fous
    allowed_hosts: str = "*"          # liste séparée par des virgules, ou "*"
    max_uploads_per_day: int = 20     # quota d'uploads par utilisateur / 24 h
    max_notes_per_user: int = 100     # nombre total de notes par utilisateur
    admin_emails: str = ""            # comptes admin, emails séparés par des virgules

    # Clé secrète pour l'endpoint de sauvegarde /tasks/backup (appelé par un cron
    # externe). Vide -> endpoint désactivé (404).
    backup_key: str = ""

    # --- Essais gratuits / abonnement ---
    free_audio: int = 1              # résumés audio gratuits par compte
    free_video: int = 1             # fiches vidéo gratuites par compte
    subscription_label: str = "plusieurs formules"

    # --- Gumroad (abonnement payant) ---
    gumroad_product_url: str = "https://novastudio47.gumroad.com/l/Veyra"
    gumroad_product_permalink: str = "Veyra"   # champ product_permalink du webhook (comparé sans casse)
    gumroad_product_id: str = ""               # id produit Gumroad (contrôle webhook)
    gumroad_seller_id: str = ""                # id vendeur Gumroad (contrôle webhook)
    gumroad_access_token: str = ""             # token API Gumroad (re-vérification de chaque vente)

    @property
    def gumroad_configured(self) -> bool:
        return bool(self.gumroad_seller_id or self.gumroad_access_token)

    # --- Résumé Vidéo (YouTube) ---
    video_max_minutes: int = 90              # au-delà : « vidéo trop longue »
    video_transcript_max_chars: int = 35000  # tronqué avant envoi à l'IA
    video_per_hour: int = 20                 # garde-fou anti-abus (hors admin)
    # API tierce pour les sous-titres (YouTube bloque les datacenters).
    supadata_api_key: str = ""

    @property
    def admin_emails_list(self) -> list[str]:
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]

    @property
    def db_backend(self) -> str:
        return "turso" if self.database_url.startswith(("libsql://", "https://")) else "sqlite"

    @property
    def is_prod(self) -> bool:
        """Heuristique : on est « en prod » dès qu'on parle à Turso."""
        return self.db_backend == "turso"

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    # --- Groq (étapes 2 & 3) ---
    groq_api_key: str = ""
    groq_transcribe_model: str = "whisper-large-v3-turbo"
    groq_summary_model: str = "openai/gpt-oss-120b"
    # Langue attendue des audios (ISO-639-1). "" = détection automatique.
    transcribe_language: str = "fr"

    # --- Archivage (étape 4) ---
    # Backend : "auto" (B2 si configuré, sinon Drive si token présent, sinon rien),
    # ou forcé à "b2" / "drive" / "none".
    archive_backend: str = "auto"
    # URL publique du site (pour les liens de téléchargement dans l'email).
    public_base_url: str = ""

    # Backblaze B2 (API compatible S3).
    b2_key_id: str = ""
    b2_app_key: str = ""
    b2_bucket: str = ""
    b2_endpoint: str = ""  # ex : https://s3.us-west-004.backblazeb2.com

    # Google Drive (facultatif, surtout pour un usage local).
    google_client_secret_file: str = "google_client_secret.json"
    google_token_file: str = "token.json"
    drive_root_folder: str = "Veyra"

    # Audio > ce seuil -> MEGA (étape 4b) ; sinon -> backend d'archivage.
    large_file_threshold_mb: int = 50

    @property
    def b2_enabled(self) -> bool:
        return bool(self.b2_key_id and self.b2_app_key and self.b2_bucket
                    and self.b2_endpoint)

    @property
    def effective_archive_backend(self) -> str:
        if self.archive_backend != "auto":
            return self.archive_backend
        if self.b2_enabled:
            return "b2"
        if self.google_token_path.exists():
            return "drive"
        return "none"

    # Clé de signature des cookies de session. DOIT être définie en prod
    # (stable entre redémarrages, sinon toutes les sessions sautent).
    secret_key: str = _DEFAULT_SECRET_KEY

    def assert_secure(self) -> None:
        """Refuse de démarrer en prod avec des réglages dangereux."""
        if self.is_prod and self.secret_key == _DEFAULT_SECRET_KEY:
            raise RuntimeError(
                "SECRET_KEY non défini en production : les sessions seraient "
                "falsifiables. Définis la variable d'environnement SECRET_KEY."
            )

    # --- Email (étape 5) ---
    # Transports possibles, par ordre de préférence :
    #  - Brevo (API HTTPS)  : 300 mails/jour gratuits, envoi vers N'IMPORTE QUELLE
    #    adresse dès qu'un expéditeur unique est vérifié (pas besoin de domaine).
    #  - Resend (API HTTPS) : sans domaine vérifié, n'envoie QUE vers l'adresse
    #    du compte Resend -> inadapté pour envoyer au client.
    #  - SMTP               : bloqué en sortie par Render (tous ports) -> local seulement.
    mail_from_name: str = "Veyra"
    brevo_api_key: str = ""
    brevo_sender: str = ""          # adresse expéditeur vérifiée dans Brevo
    resend_api_key: str = ""
    resend_from: str = "Veyra <onboarding@resend.dev>"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_to: str = ""              # repli optionnel si aucun destinataire fourni
    mail_from: str = ""            # override SMTP/Resend

    @property
    def email_provider(self) -> str:
        if self.brevo_api_key and self.brevo_sender:
            return "brevo"
        if self.resend_api_key:
            return "resend"
        if self.smtp_user and self.smtp_password:
            return "smtp"
        return "none"

    @property
    def email_enabled(self) -> bool:
        return self.email_provider != "none"

    @property
    def effective_mail_from(self) -> str:
        if self.mail_from:
            return self.mail_from
        if self.email_provider == "resend":
            return self.resend_from
        if self.email_provider == "brevo":
            return self.brevo_sender
        return self.smtp_user

    @property
    def google_client_secret_path(self) -> Path:
        p = Path(self.google_client_secret_file)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def google_token_path(self) -> Path:
        p = Path(self.google_token_file)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def large_file_threshold_bytes(self) -> int:
        return self.large_file_threshold_mb * 1024 * 1024

    @property
    def upload_path(self) -> Path:
        p = Path(self.upload_dir)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def sqlite_path(self) -> Path:
        """Chemin du fichier SQLite local (utilisé quand db_backend == 'sqlite')."""
        prefix = "sqlite:///"
        raw = self.database_url[len(prefix):] if self.database_url.startswith(prefix) else "data/plaud.db"
        p = Path(raw)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()

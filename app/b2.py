"""Stockage d'archivage sur Backblaze B2 via l'API compatible S3.

Pas d'OAuth : une simple paire de clés (keyID / applicationKey). Le bucket est
privé ; les fichiers sont partagés par des URL signées à durée limitée,
générées à la demande par l'app (voir la route /notes/{id}/dl/{name}).
"""

from __future__ import annotations

from pathlib import Path

import boto3
from botocore.config import Config

from app.config import get_settings


class B2NotConfigured(RuntimeError):
    pass


def _client():
    s = get_settings()
    if not s.b2_enabled:
        raise B2NotConfigured(
            "B2_KEY_ID / B2_APP_KEY / B2_BUCKET / B2_ENDPOINT manquants dans .env."
        )
    return boto3.client(
        "s3",
        endpoint_url=s.b2_endpoint,
        aws_access_key_id=s.b2_key_id,
        aws_secret_access_key=s.b2_app_key,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def upload(local_path: Path, key: str) -> str:
    """Téléverse un fichier sous la clé donnée. Renvoie la clé."""
    s = get_settings()
    _client().upload_file(str(local_path), s.b2_bucket, key)
    return key


def put_text(text: str, key: str, content_type: str = "application/json") -> str:
    """Écrit une chaîne directement sous la clé donnée (sans fichier local)."""
    s = get_settings()
    _client().put_object(
        Bucket=s.b2_bucket, Key=key,
        Body=text.encode("utf-8"), ContentType=content_type,
    )
    return key


def list_keys(prefix: str = "") -> list[dict]:
    """Liste les objets sous un préfixe : [{key, last_modified, size}, ...]."""
    s = get_settings()
    out: list[dict] = []
    token = None
    while True:
        kw = {"Bucket": s.b2_bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = _client().list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            out.append({"key": o["Key"], "last_modified": o["LastModified"],
                        "size": o["Size"]})
        if not resp.get("IsTruncated"):
            return out
        token = resp.get("NextContinuationToken")


def delete(key: str) -> None:
    """Supprime un objet du bucket."""
    s = get_settings()
    _client().delete_object(Bucket=s.b2_bucket, Key=key)


def fetch_text(key: str, max_bytes: int = 300_000) -> str:
    """Télécharge un objet texte (transcription) et le renvoie en str."""
    s = get_settings()
    obj = _client().get_object(Bucket=s.b2_bucket, Key=key)
    return obj["Body"].read(max_bytes).decode("utf-8", "replace")


def presigned_url(key: str, expires: int = 3600) -> str:
    """URL de téléchargement temporaire pour un objet privé."""
    s = get_settings()
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": s.b2_bucket, "Key": key},
        ExpiresIn=expires,
    )


def check() -> str:
    """Vérifie l'accès au bucket. Renvoie le nom du bucket si OK."""
    s = get_settings()
    _client().head_bucket(Bucket=s.b2_bucket)
    return s.b2_bucket

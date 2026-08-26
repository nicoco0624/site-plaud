"""Autorisation Google Drive — à lancer une seule fois, en local.

    .venv/bin/python -m app.google_auth

Ouvre le navigateur, demande l'accès à ton Drive (périmètre restreint aux
fichiers créés par l'app), puis écrit token.json à la racine du projet.
"""

from app.drive import authorize

if __name__ == "__main__":
    path = authorize()
    print(f"OK — jeton écrit dans {path}")

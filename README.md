# Site Plaud

Clone auto-hébergé de Plaud : on **upload un audio**, il est **transcrit**,
**résumé par une IA**, **archivé en ligne**, puis **envoyé par email**.

Pipeline : `upload → transcription → résumé → archivage → email`, entièrement
en tâches de fond, suivi en direct dans le navigateur.

## Stack (100 % gratuit)

| Rôle | Techno |
|---|---|
| Backend | FastAPI + Jinja2 + HTMX |
| Transcription | Groq `whisper-large-v3-turbo` |
| Résumé | Groq `openai/gpt-oss-120b` |
| Archivage | Backblaze B2 (compatible S3) — Google Drive en option locale |
| Email | SMTP Gmail (mot de passe d'application) |
| Base de données | SQLite en local · Turso (libSQL) en ligne |
| Tâches asynchrones | FastAPI BackgroundTasks |

## Lancer en local

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # puis remplir les valeurs
.venv/bin/uvicorn app.main:app --reload --port 8000
```

## Déploiement (Render, offre gratuite)

Service **Web** de type **Docker**, créé depuis un dépôt Git public. Les
variables d'environnement (`GROQ_API_KEY`, `B2_*`, `SMTP_*`, `DATABASE_URL`,
`TURSO_AUTH_TOKEN`, `PUBLIC_BASE_URL`) se règlent dans **Environment**, jamais
dans le dépôt. Voir `.env.example` et `render.yaml` pour la liste complète.
L'offre gratuite met le service en veille après 15 min d'inactivité (réveil en
~30 s à la visite suivante).

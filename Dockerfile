# Image de déploiement (Render / tout hébergeur Docker).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# HF Spaces exécute le conteneur en uid 1000.
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user:user . .
# WORKDIR est créé root : rendre tout le dossier inscriptible par 'user'
# (l'app y crée uploads/ au démarrage).
RUN mkdir -p uploads && chown -R user:user /home/user/app
USER user

# L'hébergeur fournit $PORT (Render : 10000). 7860 par défaut en local.
ENV PORT=7860
EXPOSE 7860
# --forwarded-allow-ips : faire confiance au X-Forwarded-Proto du proxy Render
#   (sinon request.url.scheme reste "http" -> cookies de session non "Secure").
# --no-server-header : ne pas exposer "server: uvicorn".
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --forwarded-allow-ips='*' --no-server-header --proxy-headers"]

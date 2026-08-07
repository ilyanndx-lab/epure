# Épure — backend FastAPI
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    EPURE_FICHES_DIR=/data/fiches

# Dépendances système : ffmpeg + libsndfile pour l'audio (Whisper/Piper),
# build-essential pour les rares paquets sans wheel, curl pour le healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ffmpeg \
        libsndfile1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

# Couche dépendances : mise en cache tant que requirements.txt est inchangé.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Code applicatif (les données/modèles sont fournis via des volumes au runtime).
COPY . .

EXPOSE 8000

# Les modèles ML (sentence-transformers, Whisper, Piper) se téléchargent au
# premier démarrage : laisser une période de démarrage généreuse au healthcheck.
#
# /health et non /openapi.json : seuls /health et /pair sont exempts du token
# d'API (cf. _AUTH_EXEMPT_PATHS dans main.py). /openapi.json répondait donc 401,
# `curl -f` échouait, et le conteneur restait « unhealthy » en permanence — un
# `depends_on: condition: service_healthy` ne se déclenchait jamais.
# `curl` envoie `Host: localhost`, accepté par défaut par TrustedHostMiddleware.
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=5 \
    CMD curl -fsS http://localhost:8000/health > /dev/null || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

# Image de l'orchestrateur, déployée par Railway à chaque push sur main.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WHITELIST_PATH=/app/config/sources_whitelist.yaml

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY apps/orchestrator/pyproject.toml apps/orchestrator/
COPY apps/orchestrator/contentmachine apps/orchestrator/contentmachine
RUN pip install ./apps/orchestrator
COPY config config

RUN useradd --create-home app
USER app

# Railway fournit $PORT
CMD ["sh", "-c", "uvicorn contentmachine.api:app --host 0.0.0.0 --port ${PORT:-8000}"]

# Image de base fixée (version et empreinte) : une reconstruction donne la même image. À monter à la main.
FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Paris

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Utilisateur non-root ; l'UID 1000 correspond en général au premier utilisateur Debian,
# ce qui évite les problèmes de droits sur le volume ./data.
RUN useradd --create-home --uid 1000 bot && mkdir -p /app/data /app/backups && chown bot:bot /app/data /app/backups

COPY --chown=bot:bot src ./src
COPY --chown=bot:bot config ./config

USER bot

# Le bot réécrit data/heartbeat toutes les 30 s.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/app/data/heartbeat')<180 else 1)"

CMD ["python", "-m", "src.main", "run"]

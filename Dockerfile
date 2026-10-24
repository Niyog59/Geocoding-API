# API image. Runs as a non-root user with dependencies installed from the
# hash-locked requirements file.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY app ./app
COPY main.py ./
RUN useradd --system --uid 10001 api
USER api

ENV PORT=8000 WEB_CONCURRENCY=1
EXPOSE 8000
# Uvicorn stops accepting connections on SIGTERM and gives in-flight requests
# up to 20 seconds to finish before the lifespan closes the shared clients.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY} --no-access-log --timeout-graceful-shutdown 20"]

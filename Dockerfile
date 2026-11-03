# One image for both the fabric and the studio (the studio also serves the built web UI).
# GPU workers and Ollama run natively on the host; see README.

FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY cinemafabric ./cinemafabric
RUN pip install --no-cache-dir ".[postgres,s3,c2pa]"
COPY --from=ui /ui/dist ./frontend/dist
RUN useradd --create-home --uid 1000 cinemafabric && mkdir /data && chown cinemafabric /data
ENV CF_DATA_DIR=/data \
    CF_FRONTEND_DIST=/app/frontend/dist \
    PYTHONUNBUFFERED=1
USER cinemafabric
EXPOSE 8000 8100
CMD ["uvicorn", "cinemafabric.studio.app:app", "--host", "0.0.0.0", "--port", "8000"]

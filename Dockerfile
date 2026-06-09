# The Python image backing three containers (ADR-0009): the HTTP API, the
# admission worker, and the scheduled daily pipeline. They share one codebase and
# differ only by command, so one image serves all three.
FROM python:3.12-slim

# yt-dlp/ffmpeg are needed by the daily pipeline's audio path; harmless for the
# API and worker. Kept minimal.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for layer caching, then the package itself.
COPY pyproject.toml ./
COPY health_bok ./health_bok
RUN pip install --no-cache-dir ".[web]"

# Default to the API; docker-compose overrides `command` for the worker and the
# scheduled pipeline.
EXPOSE 8000
CMD ["uvicorn", "health_bok.api:app", "--host", "0.0.0.0", "--port", "8000"]

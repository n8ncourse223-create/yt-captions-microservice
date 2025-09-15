# Dockerfile
# ----------
# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Install system deps (optional, minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY main.py /app/main.py

# Install Python deps
RUN pip install --no-cache-dir fastapi uvicorn yt-dlp

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

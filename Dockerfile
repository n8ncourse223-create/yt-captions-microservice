# Dockerfile
# ----------
# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Install system deps + curl (needed to install Deno)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
 && rm -rf /var/lib/apt/lists/*

# Install Deno (required for yt-dlp JS challenge solving)
RUN curl -fsSL https://deno.land/install.sh | sh \
 && ln -s /root/.deno/bin/deno /usr/local/bin/deno

WORKDIR /app
COPY main.py /app/main.py

# Install Python deps
RUN pip install --no-cache-dir fastapi uvicorn yt-dlp

EXPOSE 8000
CMD ["sh","-c","uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]

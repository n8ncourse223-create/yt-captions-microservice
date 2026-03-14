# syntax=docker/dockerfile:1
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    unzip \
 && rm -rf /var/lib/apt/lists/*

# Install Deno (for yt-dlp EJS JS-challenge solving)
RUN curl -fsSL https://deno.land/install.sh | sh \
 && ln -s /root/.deno/bin/deno /usr/local/bin/deno

WORKDIR /app
COPY main.py /app/main.py

RUN pip install --no-cache-dir fastapi uvicorn --pre "yt-dlp[default]"

EXPOSE 8000
CMD ["sh","-c","uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]

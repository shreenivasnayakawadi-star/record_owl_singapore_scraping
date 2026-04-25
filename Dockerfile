# syntax=docker/dockerfile:1.6
# RecordOwl Scraper — Docker image
# Python + Google Chrome (for Selenium) + the app code.
# Postgres is NOT bundled — connect to it via DATABASE_URL (compose service or Render managed DB).

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    CHROME_BIN=/usr/bin/google-chrome \
    PORT=8000

# System deps for Selenium/Chrome + a Postgres client (psql) for ad-hoc debugging
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget curl gnupg ca-certificates unzip \
        fonts-liberation libnss3 libxss1 libasound2 libatk-bridge2.0-0 \
        libgtk-3-0 libdrm2 libgbm1 libx11-xcb1 xdg-utils \
        libxcomposite1 libxdamage1 libxrandr2 libxshmfence1 libcups2 \
        postgresql-client \
    && wget -q -O /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Cache mount keeps downloaded wheels between builds — speeds up incremental builds
# and survives mid-build cancellation. Drop --no-cache-dir so pip writes into the mount.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install -r requirements.txt

COPY . .
RUN chmod +x /app/start.sh

# Persistent runtime dirs (mount Render Disk on /app/data in production)
RUN mkdir -p /app/data /app/.cookies /app/credentials /app/input_files /app/json_data

EXPOSE 8000

CMD ["/app/start.sh"]

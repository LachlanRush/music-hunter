# Music Hunter add-on (SPEC.md §18.4): the web app, database and audio library on the Pi. No torch or MAEST here —
# downloads and analysis are queued for the PC worker ([worker] mode = remote, set by run.sh).
ARG BUILD_FROM=python:3.12-slim-trixie
FROM ${BUILD_FROM}

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# The app's light dependencies, at the versions in uv.lock.
RUN pip install --no-cache-dir "fastapi==0.162.0" "uvicorn[standard]==0.54.0" "requests==2.34.2" "numpy==2.5.4" \
    "mutagen==1.48.1" "yt-dlp[default]==2026.10.26" "ytmusicapi==1.12.3"

# The source: a copy placed in source/ (local add-on, or a test build), else the matching release from GitHub.
ARG BUILD_VERSION=main
COPY . /addon
# Run from the source folder (not installed), so the project's config.toml sits next to it as the defaults.
RUN if [ -d /addon/source ]; then cp -r /addon/source /app; \
    else mkdir /app && curl -fsSL "https://github.com/LachlanRush/music-hunter/archive/refs/tags/v${BUILD_VERSION}.tar.gz" \
         | tar xz --strip-components=1 -C /app; fi \
 && cp /addon/run.sh /run.sh && chmod +x /run.sh

ENV MUSIC_HUNTER_ROOT=/data PYTHONPATH=/app/src PYTHONUNBUFFERED=1
WORKDIR /data
CMD ["/run.sh"]

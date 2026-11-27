# Kombüse: self-hosted kitchen app. Python standard library, plus the Anthropic SDK for AI runs with Claude. No build step.
# Video import (docs/video.md): ffmpeg, yt-dlp (pinned; sites change often, so the app can update it into the data
# directory, Settings → Video & Whisper) and QuickJS as the JavaScript runtime yt-dlp needs for YouTube.
FROM python:3.13-alpine
ARG YTDLP_VERSION=2026.11.23

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KOMBUESE_DATA=/data \
    KOMBUESE_PORT=9926 \
    KOMBUESE_SECRETS=/secrets \
    TZ=Europe/Berlin

RUN apk add --no-cache tzdata ffmpeg quickjs \
 && pip install --no-cache-dir "anthropic>=1,<2" "yt-dlp[default]==${YTDLP_VERSION}" \
 && adduser -D -u 1000 kombuese \
 && mkdir -p /data /secrets \
 && chown kombuese:kombuese /data

COPY app/ /app/
COPY tools/ /app/tools/

USER kombuese
WORKDIR /app
EXPOSE 9926
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:9926/api/version >/dev/null || exit 1
CMD ["python", "/app/server.py"]

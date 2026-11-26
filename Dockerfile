FROM python:3.12-slim

LABEL org.opencontainers.image.title="Jukebox" \
      org.opencontainers.image.description="Shared, live-synced Plex music playlists" \
      org.opencontainers.image.source="https://github.com/slinky34/jukebox"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JUKEBOX_DATA=/data

WORKDIR /app
COPY requirements.lock .
RUN pip install -r requirements.lock

COPY run.py .
COPY jukebox ./jukebox
COPY static ./static

# Run as an ordinary user. /data is created here so a new volume inherits the owner.
RUN useradd --uid 1000 --create-home jukebox && mkdir /data && chown jukebox /data
USER jukebox
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/config', timeout=4)"

CMD ["python", "run.py"]

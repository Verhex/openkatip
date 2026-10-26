# Toplantı botu: Chromium + Xvfb + PulseAudio.
# Bot mikrofonu bilerek sessiz bir null-source'a bağlanır; toplantı sesi ise
# sayfadan WebRTC track'i olarak yakalanır (PulseAudio yalnızca Zoom fallback'i için).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DISPLAY=:99 \
    PULSE_SERVER=unix:/tmp/pulse/native \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    KATIP_BOT_SANDBOX=on \
    KATIP_DATA_DIR=/tmp/katip \
    XDG_CONFIG_HOME=/tmp/config

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11-utils \
      pulseaudio pulseaudio-utils \
      ffmpeg \
      fonts-liberation fonts-noto-color-emoji \
      ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin katip
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && playwright install --with-deps chromium
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

COPY docker/entrypoint-bot.sh /usr/local/bin/entrypoint-bot.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint-bot.sh && chmod +x /usr/local/bin/entrypoint-bot.sh

USER 1000:1000

ENTRYPOINT ["/usr/local/bin/entrypoint-bot.sh"]

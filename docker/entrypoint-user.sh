#!/usr/bin/env bash
# Yetkisiz kullanıcıyla çalışan kısım: sanal ekran + ses sunucusu (bot için), ardından API.
set -euo pipefail

PORT="${PORT:-8000}"
# Bot aynı konteynerde: API'ye yerel adresten bağlanır.
export KATIP_INGEST_WS_URL="ws://127.0.0.1:${PORT}/ws/ingest"

# PulseAudio: Chromium'a bir ses çıkışı sağlar. Sesli cevap tarayıcı içindeki sentetik
# mikrofonla verildiği için PulseAudio kalkmasa da bot çalışır; hata bu yüzden ölümcül değil.
# PULSE_SERVER tanımlıyken PulseAudio "sunucu zaten ayarlı" deyip sessizce başlamıyordu (bu
# yüzden üretimde hiç çalışmadı; Teams "hoparlör bulunamadı" uyarısı veriyordu). Daemon o
# değişken olmadan başlatılır; istemciler (Chromium, pactl) değişkenle bağlanır.
if env -u PULSE_SERVER pulseaudio --start --exit-idle-time=-1 --disallow-exit \
     --load="module-native-protocol-unix socket=/tmp/pulse/native" 2>/dev/null \
   && pactl info >/dev/null 2>&1; then
  pactl load-module module-null-sink sink_name=katip_out >/dev/null 2>&1 || true
  pactl set-default-sink katip_out >/dev/null 2>&1 || true
else
  echo "uyarı: PulseAudio başlatılamadı; bot sessiz ses çıkışıyla devam edecek" >&2
fi

Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
for _ in $(seq 1 30); do
  xdpyinfo -display :99 >/dev/null 2>&1 && break
  sleep 0.2
done

# X-Forwarded-For yalnızca yerel süreçten kabul edilir: dışarıdan gelen bir istek başlığı
# taklit edip kendini 127.0.0.1 (bot) gibi gösteremez. Loglarda istemci IP'si proxy'ninki olur.
exec uvicorn katip.api.main:app --host 0.0.0.0 --port "$PORT" \
  --proxy-headers --forwarded-allow-ips="127.0.0.1" --ws-max-size 4194304

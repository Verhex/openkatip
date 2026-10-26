#!/usr/bin/env bash
# Sanal ekran + sanal ses aygıtlarını hazırlar, sonra botu çalıştırır.
set -euo pipefail

export XDG_RUNTIME_DIR=/tmp/runtime-katip
mkdir -p /tmp/pulse "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
env -u PULSE_SERVER pulseaudio --start --exit-idle-time=-1 --disallow-exit \
  --load="module-native-protocol-unix socket=/tmp/pulse/native"

# Toplantı sesinin karışık (mixed) yedek kaydı için çıkış sink'i.
pactl load-module module-null-sink sink_name=katip_out sink_properties=device.description=katip_out
# Bot mikrofonu: tamamen sessiz kaynak (kimse botun ortam sesini duymasın).
pactl load-module module-null-sink sink_name=katip_mic sink_properties=device.description=katip_mic
pactl set-default-sink katip_out
pactl set-default-source katip_mic.monitor

Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
for _ in $(seq 1 30); do
  xdpyinfo -display :99 >/dev/null 2>&1 && break
  sleep 0.2
done

exec python -m katip.bot.runner "$@"

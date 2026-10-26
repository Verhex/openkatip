"""Katip'i bir toplantıya gönderir ve imzalı panel linkini yazdırır.

Kullanım:
    python scripts/join.py "https://teams.microsoft.com/meet/…"
    python scripts/join.py "<link>" --mode kayit      # gün boyu sessiz kayıt modu

Yönetici token'ı `.env` dosyasındaki KATIP_ADMIN_TOKEN'dan (ya da ortam değişkeninden) okunur;
ekrana yazdırılmaz. Sunucu adresi KATIP_PUBLIC_BASE_URL ya da yerel API adresidir.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:8000"


def _env(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Katip'i toplantıya gönder")
    parser.add_argument("url", help="Teams / Meet / Zoom toplantı linki")
    parser.add_argument("--mode", choices=["tam", "kayit"], default=None)
    args = parser.parse_args()

    token = _env("KATIP_ADMIN_TOKEN")
    if not token:
        print("KATIP_ADMIN_TOKEN bulunamadı (.env ya da ortam değişkeni).", file=sys.stderr)
        return 2
    base = (_env("KATIP_PUBLIC_BASE_URL") or DEFAULT_BASE).rstrip("/")
    body = {"url": args.url}
    if args.mode:
        body["mode"] = args.mode
    request = urllib.request.Request(
        f"{base}/api/meetings",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"Gönderilemedi ({exc.code}): {detail}", file=sys.stderr)
        return 1
    print(f"Katip yola çıktı. Oturum: {data['session_id']} ({data['platform']})")
    print(f"Canlı panel: {base}{data['panel_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

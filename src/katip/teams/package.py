import argparse
import io
import json
import os
import struct
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import jsonschema

from katip.config import Settings, get_settings

SCHEMA_VERSION = "1.30"
SCHEMA_URL = f"https://developer.microsoft.com/json-schemas/teams/v{SCHEMA_VERSION}/MicrosoftTeams.schema.json"


def assets_dir() -> Path:
    # Paket verisi: Docker imajları web/ klasörünü kopyalamadan önce paketi kurar.
    return Path(__file__).parent / "assets"


def public_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Teams paketi için geçerli bir genel HTTPS adresi gerekli")
    return value


def manifest(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    app_id = str(UUID(settings.teams_bot_app_id))
    base = public_url(
        settings.public_base_url.rstrip("/")
        or (
            "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
            if os.environ.get("RAILWAY_PUBLIC_DOMAIN")
            else ""
        )
    )
    host = urlsplit(base).hostname
    package_id = (
        str(UUID(settings.teams_app_package_id))
        if settings.teams_app_package_id
        else str(uuid5(NAMESPACE_URL, "urn:katip:teams:" + app_id))
    )
    # "team" kapsamı manifest 1.25+ sürümünde kanal özellikleri beyanı (supportsChannelFeatures)
    # ister; kanal toplantıları bu aşamada desteklenmez.
    scopes = ["personal", "groupChat"]
    commands = [
        ("yardım", "Kullanılabilir komutları gösterir."),
        ("katıl", "Yetkili panel hesabıyla Katip'i bu toplantıya çağırır."),
        ("bağla", "Bu sohbeti yönettiğiniz canlı Katip toplantısına bağlar."),
        ("durum", "Bağlı toplantıda Katip'in durumunu ve modunu gösterir."),
        ("özetle", "Bağlı canlı toplantının güncel özetini verir."),
        ("not al", "not al: … ile toplantıya not ekler."),
        ("karar", "karar: … ile toplantıya karar ekler."),
        ("aksiyon", "aksiyon: … ile toplantıya aksiyon ekler."),
        ("rapor", "Hazır toplantı raporunun bağlantısını verir."),
    ]
    result = {
        "$schema": SCHEMA_URL,
        "manifestVersion": SCHEMA_VERSION,
        "version": "1.0.1",
        "id": package_id,
        "name": {"short": "Katip", "full": settings.bot_display_name},
        "developer": {
            "name": settings.company_name or "Katip",
            "websiteUrl": base,
            "privacyUrl": public_url(settings.teams_privacy_url or base + "/gizlilik"),
            "termsOfUseUrl": public_url(settings.teams_terms_url or base + "/gizlilik"),
        },
        "description": {
            "short": "Toplantı notları, özetler ve toplantı sahibi mail onayı.",
            "full": "Katip toplantı sohbetinde not, karar ve aksiyon alır; güncel özet ve rapor "
            "bağlantısını paylaşır. Toplantı notu maili yetkili panel hesabının kişisel "
            "kart onayıyla gönderilir. Ses mevcut tarayıcı botuyla işlenir.",
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#1c3f94",
        "bots": [
            {
                "botId": app_id,
                "scopes": scopes,
                "isNotificationOnly": False,
                "supportsFiles": False,
                "commandLists": [
                    {
                        "scopes": scopes,
                        "commands": [
                            {"title": title, "description": description}
                            for title, description in commands
                        ],
                    }
                ],
            }
        ],
        "validDomains": [host],
        "webApplicationInfo": {"id": app_id, "resource": "api://botid-" + app_id},
        "authorization": {
            "permissions": {
                "resourceSpecific": [
                    {"name": "OnlineMeeting.ReadBasic.Chat", "type": "Application"}
                ]
            }
        },
    }
    schema = json.loads((assets_dir() / "MicrosoftTeams.schema.json").read_text(encoding="utf-8"))
    jsonschema.validators.validator_for(schema)(
        schema,
        format_checker=jsonschema.FormatChecker(),
    ).validate(result)
    return result


def build_package(settings: Settings | None = None) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json", json.dumps(manifest(settings), ensure_ascii=False, indent=2)
        )
        for name, size in (("color.png", 192), ("outline.png", 32)):
            raw = (assets_dir() / name).read_bytes()
            if raw[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", raw[16:24]) != (size, size):
                raise ValueError("Teams simgesi boyutu geçersiz")
            archive.writestr(name, raw)
    return result.getvalue()


def main():
    parser = argparse.ArgumentParser(description="Katip Teams uygulama paketi")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    # Paket üreticisi .env okumaz; yalnız süreç ortamını kullanır.
    args.out.write_bytes(build_package(Settings(_env_file=None)))


if __name__ == "__main__":
    main()

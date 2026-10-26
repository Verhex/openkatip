from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from katip.config import get_settings


@dataclass(frozen=True)
class Credentials:
    app_id: str
    tenant: str
    secret: str = field(repr=False)


def credentials() -> Credentials | None:
    settings = get_settings()
    secret = settings.teams_bot_secret
    if settings.teams_bot_secret_file:
        try:
            secret = Path(settings.teams_bot_secret_file).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    tenant = settings.teams_bot_tenant_id or settings.m365_tenant_id
    if not (settings.teams_bot_app_id and tenant and secret):
        return None
    try:
        return Credentials(str(UUID(settings.teams_bot_app_id)), str(UUID(tenant)), secret)
    except ValueError:
        return None

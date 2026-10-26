import asyncio
import time
from collections import OrderedDict
from uuid import UUID

import httpx
import jwt

from katip.async_utils import LoopLocal
from katip.logging import get_logger
from katip.teams.activity import Activity
from katip.teams.config import Credentials, credentials

OPENID = "https://login.botframework.com/v1/.well-known/openidconfiguration"
ISSUER = "https://api.botframework.com"
KEYS_URL = "https://login.botframework.com/v1/.well-known/keys"
SKEW = 30
log = get_logger(__name__)
last_validation_error = False


class AuthenticationError(ValueError):
    pass


class Validator:
    def __init__(self):
        self.keys: dict[str, dict] = {}
        self.expires = 0.0
        self.refreshed = -float("inf")
        self.unknown_refreshed = -float("inf")
        self.lock = asyncio.Lock()

    async def refresh(self):
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            metadata = await client.get(OPENID)
            metadata.raise_for_status()
            data = metadata.json()
            if (
                data.get("issuer") != ISSUER
                or data.get("jwks_uri") != KEYS_URL
                or "RS256" not in data.get("id_token_signing_alg_values_supported", [])
            ):
                raise AuthenticationError()
            response = await client.get(KEYS_URL)
            response.raise_for_status()
            self.keys = {
                key["kid"]: key
                for key in response.json()["keys"]
                if key.get("kty") == "RSA"
                and key.get("use", "sig") == "sig"
                and key.get("alg", "RS256") == "RS256"
            }
        self.refreshed = time.monotonic()
        self.expires = self.refreshed + 3600

    async def validate(self, header: str | None, activity: Activity, config: Credentials):
        global last_validation_error
        reason, token = "başlık", ""
        try:
            parts = (header or "").split()
            if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > 16000:
                raise AuthenticationError()
            token = parts[1]
            reason = "algoritma"
            jose = jwt.get_unverified_header(token)
            kid = jose.get("kid")
            if jose.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                raise AuthenticationError()
            async with self.lock:
                if self.expires <= time.monotonic():
                    await self.refresh()
                elif kid not in self.keys and time.monotonic() - self.unknown_refreshed >= 5:
                    # Bilinmeyen kid için bir yenileme; rastgele kid yağmuru sınırlandırılır.
                    self.unknown_refreshed = time.monotonic()
                    await self.refresh()
                key = self.keys.get(kid)
            reason = "anahtar" if not key else "onay"
            if not key or "msteams" not in key.get("endorsements", []):
                raise AuthenticationError()
            reason = "imza veya talepler"
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(key).key,
                algorithms=["RS256"],
                audience=config.app_id,
                issuer=ISSUER,
                leeway=SKEW,
                options={"require": ["iss", "aud", "exp"], "strict_aud": True},
            )
            reason = "serviceUrl"
            service_url = _service_url(claims)
            if not service_url or service_url.lower() != activity.service_url.lower():
                raise AuthenticationError()
            # Connector token'ında tid bulunmayabilir; activity kiracısı her zaman zorunlu.
            reason = "kiracı"
            tenants = [activity.channel_data.tenant.id]
            if activity.conversation.tenant_id is not None:
                tenants.append(activity.conversation.tenant_id)
            if "tid" in claims:
                tenants.append(claims["tid"])
            if any(
                not isinstance(tenant, str) or str(UUID(tenant)) != config.tenant
                for tenant in tenants
            ):
                raise AuthenticationError()
            last_validation_error = False
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            last_validation_error = True
            _log_rejection(reason, exc, token, activity, config)
            raise AuthenticationError() from exc


def _service_url(claims: dict) -> str | None:
    # Belgede "serviceUrl"; Bot Connector token'ında alan adı küçük harfle "serviceurl" gelir.
    values = {v for k, v in claims.items() if k.lower() == "serviceurl" and isinstance(v, str)}
    # İki farklı yazımla iki farklı adres gelirse belirsizdir; kabul edilmez.
    return values.pop() if len(values) == 1 else None


def _log_rejection(reason, exc, token, activity, config) -> None:
    # Token yazılmaz; yalnız doğrulamanın kaldığı yeri gösteren gizli olmayan alanlar.
    try:
        claims = jwt.decode(token, options={"verify_signature": False}) if token else {}
        kid = jwt.get_unverified_header(token).get("kid") if token else None
    except jwt.PyJWTError:
        claims, kid = {}, None
    log.warning(
        "Teams kimlik doğrulaması reddedildi",
        reason=reason,
        error_type=type(exc.__cause__ or exc).__name__,
        issuer=str(claims.get("iss", ""))[:120],
        audience_ok=claims.get("aud") == config.app_id,
        tenant_claim_ok=claims.get("tid") in (None, config.tenant),
        service_url_ok=(_service_url(claims) or "").lower() == activity.service_url.lower(),
        missing_claim=str(getattr(exc.__cause__ or exc, "claim", ""))[:40],
        kid_known=kid in validator.get().keys,
        channel=str(activity.channel_id or "")[:20],
    )


validator = LoopLocal(Validator)


class RateLimit:
    def __init__(self):
        self.windows: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def take(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        since, count = self.windows.pop(key, (now, 0))
        if now - since >= 60:
            since, count = now, 0
        self.windows[key] = (since, count + 1)
        while len(self.windows) > 10000:
            self.windows.popitem(last=False)
        return count < limit


limits = LoopLocal(RateLimit)


def health() -> tuple[bool | None, str]:
    if not credentials():
        return None, "kapalı"
    return (False, "son doğrulama hatası") if last_validation_error else (True, "hazır")

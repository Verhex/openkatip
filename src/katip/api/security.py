"""Erişim denetimi: yönetici, toplantıya özel bot anahtarı ve imzalı görüntüleme linkleri.

Üç tür kimlik var:
- Yönetici: `KATIP_ADMIN_TOKEN` (Authorization: Bearer). Bot başlatma, ayarlar, kişiler,
  oturum listesi, teşhis.
- Bot: her toplantı için üretilen tek kullanımlık rastgele anahtar. Yalnızca o toplantının
  ingest soketine ve ekran görüntüsü yüklemesine yeter; bot sürecine yönetici token'ı ve
  API anahtarları hiç verilmez.
- Görüntüleyen: HMAC imzalı, süreli, toplantıya bağlı link (`?t=`). Panel, transkript,
  rapor ve ekran görüntüleri bununla açılır; başka toplantıya geçmez, süresi dolunca biter.

İmza anahtarı `KATIP_SECRET_KEY`; tanımlı değilse yönetici token'ından türetilir (ikisi de
yoksa imzalı link üretilemez ve okuma uçları kapalıdır).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Query, Request
from starlette.requests import HTTPConnection

from katip.config import get_settings

# Aktif toplantıların bot anahtarları (süreç içinde; bot aynı konteynerde).
_BOT_TOKENS: dict[str, str] = {}
_BOT_EXPIRIES: dict[str, float] = {}


# ------------------------------------------------------------------ yönetici
def is_admin(authorization: str | None) -> bool:
    token = get_settings().admin_token
    if not token:
        return False
    given = (authorization or "").removeprefix("Bearer ").strip()
    return bool(given) and secrets.compare_digest(given, token)


@dataclass(frozen=True)
class PanelAccess:
    account_id: int | None = None
    role: str = "admin"

    async def allows(self, meeting_id: str, permission: str = "read") -> bool:
        from katip.store import repo

        return await repo.meeting_access(meeting_id, self.account_id, self.role, permission)

    async def check(self, meeting_id: str, permission: str = "read") -> None:
        if not await self.allows(meeting_id, permission):
            raise HTTPException(404, "Toplantı bulunamadı")

    async def meeting_ids(self, permission: str = "read") -> set[str] | None:
        from katip.store import repo

        if self.role == "admin":
            return None
        return await repo.accessible_meeting_ids(self.account_id, self.role, permission)


async def panel_identity(connection: HTTPConnection) -> PanelAccess | None:
    from katip.api.auth import current_admin_session
    from katip.store import repo

    if is_admin(connection.headers.get("authorization")):
        return PanelAccess()
    session = await current_admin_session(connection)
    if session is None:
        return None
    if session.account_id is None:
        return PanelAccess()
    account = await repo.get_panel_account(session.account_id)
    return PanelAccess(account.id, account.role) if account and account.enabled else None


async def _require_roles(
    request: Request, authorization: str | None, roles: set[str]
) -> PanelAccess:
    """Yönetici: ya betikler için `Authorization: Bearer <KATIP_ADMIN_TOKEN>` ya da yönetim
    panelinin oturum çerezi. Panel oturumunda değişiklik yapan istekler CSRF anahtarı ister."""
    if is_admin(authorization):
        return PanelAccess()
    from katip.api.auth import csrf_ok, current_admin_session

    session = await current_admin_session(request)
    if session is None:
        if not get_settings().admin_token and not get_settings().admin_password_hash:
            raise HTTPException(status_code=503, detail="Yönetici girişi tanımlı değil")
        raise HTTPException(status_code=401, detail="yetkisiz erişim")
    access = PanelAccess()
    if session.account_id is not None:
        from katip.store import repo

        account = await repo.get_panel_account(session.account_id)
        if account is None or not account.enabled or account.role not in roles:
            raise HTTPException(status_code=403, detail="Bu işlem için panel rolünüz yeterli değil")
        access = PanelAccess(account.id, account.role)
    if request.method not in ("GET", "HEAD", "OPTIONS") and not csrf_ok(request, session):
        raise HTTPException(status_code=403, detail="CSRF anahtarı geçersiz")
    return access


async def require_admin(
    request: Request, authorization: str | None = Header(default=None)
) -> PanelAccess:
    return await _require_roles(request, authorization, {"admin"})


async def require_operator(
    request: Request, authorization: str | None = Header(default=None)
) -> PanelAccess:
    return await _require_roles(request, authorization, {"admin", "operator"})


async def require_panel(
    request: Request, authorization: str | None = Header(default=None)
) -> PanelAccess:
    return await _require_roles(request, authorization, {"admin", "operator", "viewer"})


def _meeting_id(request: Request) -> str:
    return request.path_params.get("session_id") or request.path_params["meeting_id"]


async def require_meeting_read(
    request: Request, access: Annotated[PanelAccess, Depends(require_panel)]
) -> PanelAccess:
    await access.check(_meeting_id(request))
    return access


async def require_meeting_manage(
    request: Request, access: Annotated[PanelAccess, Depends(require_operator)]
) -> PanelAccess:
    await access.check(_meeting_id(request), "manage")
    return access


async def require_meeting_admin(
    request: Request, access: Annotated[PanelAccess, Depends(require_meeting_read)]
) -> PanelAccess:
    """Yöneticiye özel toplantı uçları da erişilemeyen kimlikleri gizler."""
    if access.role != "admin":
        raise HTTPException(403, "Bu işlem yalnız yöneticiye açıktır")
    return access


# ------------------------------------------------------------------ bot anahtarı
def issue_bot_token(session_id: str) -> str:
    token = secrets.token_urlsafe(32)
    _BOT_TOKENS[session_id] = token
    _BOT_EXPIRIES.pop(session_id, None)
    return token


def revoke_bot_token(session_id: str) -> None:
    _BOT_TOKENS.pop(session_id, None)
    _BOT_EXPIRIES.pop(session_id, None)


def check_bot_token(session_id: str, token: str | None) -> bool:
    if _BOT_EXPIRIES.get(session_id, float("inf")) <= time.time():
        return False
    expected = _BOT_TOKENS.get(session_id)
    if expected and expected.startswith("sha256:"):
        return bool(token) and secrets.compare_digest(
            expected[7:], hashlib.sha256(token.encode()).hexdigest()
        )
    return bool(expected and token) and secrets.compare_digest(expected, token)


def register_bot_token_hash(session_id: str, digest: str, *, expires: float | None = None) -> None:
    _BOT_TOKENS[session_id] = "sha256:" + digest
    if expires is not None:
        _BOT_EXPIRIES[session_id] = expires


def extend_bot_token(session_id: str, expires: float) -> None:
    if session_id in _BOT_TOKENS:
        _BOT_EXPIRIES[session_id] = expires


# ------------------------------------------------------------------ imzalı linkler
def _key() -> bytes | None:
    settings = get_settings()
    if settings.secret_key:
        return settings.secret_key.encode()
    if settings.admin_token:
        return hmac.new(
            settings.admin_token.encode(), b"katip-link-signing", hashlib.sha256
        ).digest()
    return None


def _mac(key: bytes, purpose: str, session_id: str, expires: int) -> str:
    msg = f"{purpose}:{session_id}:{expires}".encode()
    digest = hmac.new(key, msg, hashlib.sha256).digest()[:20]
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def sign(session_id: str, *, ttl_s: int, purpose: str = "view") -> str:
    key = _key()
    if key is None:
        raise RuntimeError("imza anahtarı yok: KATIP_SECRET_KEY ya da KATIP_ADMIN_TOKEN tanımlayın")
    expires = int(time.time()) + ttl_s
    return f"{expires:x}.{_mac(key, purpose, session_id, expires)}"


def verify(session_id: str, token: str | None, *, purpose: str = "view") -> bool:
    key = _key()
    if key is None or not token or "." not in token:
        return False
    raw_exp, mac = token.split(".", 1)
    try:
        expires = int(raw_exp, 16)
    except ValueError:
        return False
    if expires < time.time():
        return False
    return secrets.compare_digest(mac, _mac(key, purpose, session_id, expires))


def absolute_url(path: str) -> str:
    """Toplantı chat'ine yazılacak tam adres (Railway alan adı ya da KATIP_PUBLIC_BASE_URL)."""
    import os

    base = get_settings().public_base_url.rstrip("/")
    if not base and (domain := os.environ.get("RAILWAY_PUBLIC_DOMAIN")):
        base = f"https://{domain}"
    return f"{base}{path}"


def view_link_params(session_id: str, *, days: float | None = None) -> str:
    """Rapor/panel linklerine eklenecek sorgu parametresi: "t=…"."""
    ttl = int((days if days is not None else get_settings().share_link_days) * 86400)
    return f"t={sign(session_id, ttl_s=ttl)}"


async def require_viewer(
    session_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    t: str | None = Query(default=None, description="İmzalı görüntüleme anahtarı"),
) -> None:
    """Toplantıya erişebilen panel hesabı veya geçerli imzalı link sahibi."""
    if is_admin(authorization) or verify(session_id, t):
        return
    access = await panel_identity(request)
    if access is not None:
        await access.check(session_id)
        return
    raise HTTPException(status_code=401, detail="yetkisiz erişim")

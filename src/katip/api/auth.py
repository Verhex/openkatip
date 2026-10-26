"""Yönetim paneli girişi: parola, oturum çerezi, CSRF ve kaba kuvvet koruması.

Tasarım:
- Parola yalnız scrypt özeti olarak tutulur (`KATIP_ADMIN_PASSWORD_HASH`; panelden
  değiştirilirse veritabanındaki özet önceliklidir). Parolanın kendisi hiçbir yerde saklanmaz.
- Giriş başarılı olunca rastgele 32 baytlık oturum anahtarı üretilir; tarayıcıya HttpOnly,
  Secure, SameSite=Strict çerezle verilir, veritabanında yalnız özeti tutulur. Oturum 60 dk
  hareketsizlikte ve en geç 12 saatte düşer.
- Değişiklik yapan her istek (POST/PUT/DELETE) oturuma bağlı CSRF anahtarını
  `X-CSRF-Token` başlığında ister.
- Kaba kuvvet: IP başına 15 dk'da 5 hatalı deneme → 15 dk kilit; dağıtık saldırıya karşı
  15 dk'da toplam 20 hata → tüm girişler 10 dk kilit; her hatalı denemede 1 sn gecikme.
- Her başarılı/başarısız giriş denetim kaydına yazılır (IP'nin kendisi değil özeti).
- İleride Microsoft Entra ID girişi (ve 2FA) buraya ikinci bir yöntem olarak eklenecek.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from katip.config import get_settings
from katip.identity import company_email, normalize_email
from katip.logging import get_logger
from katip.store import repo

log = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_COOKIE = "katip_admin"
CSRF_COOKIE = "katip_csrf"
_SCRYPT = {"n": 2**15, "r": 8, "p": 1}
_MAXMEM = 64 * 1024 * 1024
_FAIL_DELAY_S = 1.0  # her hatalı denemede bekleme (kaba kuvveti yavaşlatır)


# ------------------------------------------------------------------ parola özeti
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, maxmem=_MAXMEM, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
            maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, _unb64(digest))


async def current_password_hash() -> str:
    stored = (await repo.get_app_settings()).get("admin_password_hash")
    return str(stored or get_settings().admin_password_hash or "")


# ------------------------------------------------------------------ kaba kuvvet koruması
class LoginGuard:
    """Süreç içi deneme sayacı (tek servis; yeniden başlayınca sıfırlanır)."""

    def __init__(
        self,
        per_client: int = 5,
        global_limit: int = 20,
        window_s: float = 900,
        client_lock_s: float = 900,
        global_lock_s: float = 600,
    ) -> None:
        self.per_client, self.global_limit, self.window_s = per_client, global_limit, window_s
        self.client_lock_s, self.global_lock_s = client_lock_s, global_lock_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._global: deque[float] = deque()
        self._locked: dict[str, float] = {}
        self._global_locked = 0.0

    def _trim(self, q: deque[float], now: float) -> None:
        while q and now - q[0] > self.window_s:
            q.popleft()

    def locked_for(self, client: str, now: float | None = None) -> float:
        now = now or time.monotonic()
        return max(self._locked.get(client, 0.0), self._global_locked) - now

    def fail(self, client: str, now: float | None = None) -> None:
        now = now or time.monotonic()
        q = self._fails[client]
        q.append(now)
        self._global.append(now)
        self._trim(q, now)
        self._trim(self._global, now)
        if len(q) >= self.per_client:
            self._locked[client] = now + self.client_lock_s
        if len(self._global) >= self.global_limit:
            self._global_locked = now + self.global_lock_s

    def success(self, client: str) -> None:
        self._fails.pop(client, None)
        self._locked.pop(client, None)


guard = LoginGuard()


def client_key(request: Request) -> str:
    """Deneme sayacı ve denetim kaydı için istemci özeti; yetki kararı vermez.

    CF modunda ASGI client gerçek TCP eşi olmalıdır (Uvicorn proxy headers kapalı).
    Başlık eksik, tekrarlı veya geçersizse eşin adresi kullanılır; XFF'ye dönülmez.
    """
    settings = get_settings()
    peer = request.client.host if request.client else "?"
    ip = peer
    if settings.client_ip_header == "x-forwarded-for":
        forwarded = request.headers.get("x-forwarded-for", "")
        ip = forwarded.split(",")[-1].strip() or peer
    else:
        try:
            address = ipaddress.ip_address(peer)
            if str(address) in settings.client_ip_trusted_proxies:
                values = request.headers.getlist("cf-connecting-ip")
                if len(values) == 1:
                    ip = str(ipaddress.ip_address(values[0].strip()))
        except ValueError:
            pass
    return hashlib.sha256(f"katip-client:{ip}".encode()).hexdigest()[:16]


# ------------------------------------------------------------------ oturum
def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def current_admin_session(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    session = await repo.get_admin_session(_sha(token))
    if session is None:
        return None
    s = get_settings()
    if session.account_id is not None:
        account = await repo.get_panel_account(session.account_id)
        if (
            account is None
            or not account.enabled
            or not company_email(account.email, s.panel_email_domains)
        ):
            await repo.delete_admin_session(session.token_hash)
            return None
    now = datetime.now(UTC)
    expires = (
        session.expires_at if session.expires_at.tzinfo else session.expires_at.replace(tzinfo=UTC)
    )
    last = session.last_seen if session.last_seen.tzinfo else session.last_seen.replace(tzinfo=UTC)
    if now >= expires or now - last > timedelta(minutes=s.admin_idle_minutes):
        await repo.delete_admin_session(session.token_hash)
        return None
    if (now - last).total_seconds() > 60:
        await repo.touch_admin_session(session.id)
    return session


def csrf_ok(request: Request, session) -> bool:
    given = request.headers.get("x-csrf-token", "")
    return bool(given) and hmac.compare_digest(_sha(given), session.csrf_hash)


def _set_cookies(response: Response, token: str, csrf: str, max_age: int) -> None:
    secure = get_settings().admin_cookie_secure
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    # CSRF çerezi JS tarafından okunur ve başlıkta geri gönderilir (çift gönderim).
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
    )


# ------------------------------------------------------------------ uçlar
class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    email: str = Field("", max_length=320)


@router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response) -> dict:
    client = client_key(request)
    wait = guard.locked_for(client)
    if wait > 0:
        await repo.audit("admin", "admin_login_locked", client=client)
        raise HTTPException(
            status_code=429,
            detail=f"Çok fazla hatalı deneme. {int(wait // 60) + 1} dakika sonra tekrar deneyin.",
        )
    s = get_settings()
    email = req.email.strip()
    account = None
    if email:
        try:
            email = normalize_email(email)
            if company_email(email, s.panel_email_domains):
                account = await repo.find_panel_account(email)
        except ValueError:
            pass
    legacy_email = s.admin_login_email.strip()
    legacy_domain_ok = not legacy_email
    if legacy_email:
        try:
            legacy_email = normalize_email(legacy_email)
            legacy_domain_ok = company_email(legacy_email, s.panel_email_domains)
        except ValueError:
            legacy_domain_ok = False
    legacy_ok = (
        legacy_domain_ok
        and email == legacy_email
        and (bool(legacy_email) or not await repo.list_panel_accounts())
    )
    stored = (
        (account.password_hash if account.enabled else "")
        if account
        else (await current_password_hash() if legacy_ok else "")
    )
    # Bilinmeyen e-posta, yanlış parola ve kapalı hesap aynı cevapla reddedilir.
    if not stored or not await asyncio.to_thread(verify_password, req.password, stored):
        guard.fail(client)
        await repo.audit("admin", "admin_login_failed", client=client)
        log.warning("panel girişi başarısız", client=client)
        await asyncio.sleep(_FAIL_DELAY_S)
        raise HTTPException(status_code=401, detail="E-posta veya parola hatalı")
    guard.success(client)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    lifetime = timedelta(hours=s.admin_session_hours)
    await repo.purge_expired_admin_sessions()
    await repo.create_admin_session(
        _sha(token),
        _sha(csrf),
        datetime.now(UTC) + lifetime,
        client,
        request.headers.get("user-agent"),
        account_id=account.id if account else None,
    )
    _set_cookies(response, token, csrf, int(lifetime.total_seconds()))
    await repo.audit(
        "admin", "admin_login", client=client, account_id=account.id if account else None
    )
    log.info("panel girişi", client=client)
    return {"ok": True, "csrf": csrf, "idle_minutes": s.admin_idle_minutes}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await repo.delete_admin_session(_sha(token))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


async def require_session(request: Request) -> None:
    if await current_admin_session(request) is None:
        raise HTTPException(401, "Oturum yok")


@router.get("/me", dependencies=[Depends(require_session)])
async def me(request: Request) -> dict:
    session = await current_admin_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Oturum yok")
    account = await repo.get_panel_account(session.account_id) if session.account_id else None
    return {
        "ok": True,
        "email": account.email if account else get_settings().admin_login_email,
        "role": account.role if account else "admin",
        "since": session.created_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
        "idle_minutes": get_settings().admin_idle_minutes,
    }


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=256)
    new: str = Field(min_length=16, max_length=256)


def _strong_enough(password: str) -> bool:
    classes = [
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ]
    return len(password) >= 16 and sum(classes) >= 3


@router.post("/password", dependencies=[Depends(require_session)])
async def change_password(req: PasswordChange, request: Request, response: Response) -> dict:
    session = await current_admin_session(request)
    if session is None or not csrf_ok(request, session):
        raise HTTPException(status_code=401, detail="Oturum ya da CSRF anahtarı geçersiz")
    account = await repo.get_panel_account(session.account_id) if session.account_id else None
    stored = account.password_hash if account else await current_password_hash()
    if not await asyncio.to_thread(verify_password, req.current, stored):
        await repo.audit("admin", "admin_password_change_failed", client=client_key(request))
        await asyncio.sleep(_FAIL_DELAY_S)
        raise HTTPException(status_code=401, detail="Mevcut parola hatalı")
    if not _strong_enough(req.new):
        raise HTTPException(
            status_code=422,
            detail="Yeni parola en az 16 karakter olmalı ve harf, rakam, sembol karışımı içermeli",
        )
    hashed = await asyncio.to_thread(hash_password, req.new)
    if account:
        await repo.change_panel_password(account.id, hashed)
    else:
        await repo.set_app_settings({"admin_password_hash": hashed})
    # Parola değişince tüm oturumlar kapanır (çalınmış bir oturum da düşer).
    if account is None:
        await repo.delete_all_admin_sessions()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    await repo.audit("admin", "admin_password_changed", client=client_key(request))
    return {"ok": True, "relogin": True}


async def _panel_admin(request: Request) -> None:
    from katip.api.security import require_admin

    await require_admin(request, request.headers.get("authorization"))


class AccountRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=16, max_length=256)
    role: Literal["admin", "operator", "viewer"] = "operator"


class AccountUpdate(BaseModel):
    role: Literal["admin", "operator", "viewer"] | None = None
    enabled: bool | None = None


def _account_row(account) -> dict:
    return {
        "id": account.id,
        "email": account.email,
        "role": account.role,
        "enabled": account.enabled,
        "created_at": account.created_at.isoformat(),
    }


async def _update_account(account_id: int, request: Request, **changes):
    from katip.api.security import panel_identity

    access = await panel_identity(request)
    settings = get_settings()
    return await repo.update_panel_account(
        account_id,
        actor_account_id=access.account_id if access else None,
        env_admin=bool(settings.admin_password_hash or settings.admin_token),
        **changes,
    )


@router.get("/accounts", dependencies=[Depends(_panel_admin)])
async def accounts() -> list[dict]:
    from katip.pipeline.session import registry

    now = datetime.now(UTC)
    sessions = await repo.list_admin_sessions()
    usage = await repo.usage_report(30)
    live_owners = set()
    for live in registry.all():
        meeting = await repo.get_meeting(live.session_id)
        if meeting:
            live_owners.add(meeting.owner_account_id)
    rows = []
    for account in await repo.list_panel_accounts():
        own_sessions = [s for s in sessions if s.account_id == account.id]
        seen = max((s.last_seen.replace(tzinfo=UTC) for s in own_sessions), default=None)
        active_sessions = (
            sum(
                s.expires_at.replace(tzinfo=UTC) > now
                and now - s.last_seen.replace(tzinfo=UTC)
                < timedelta(minutes=get_settings().admin_idle_minutes)
                for s in own_sessions
            )
            if account.enabled
            else 0
        )
        own_usage = [(m, u) for m, u in usage if m.owner_account_id == account.id]
        async with repo.session_scope() as session:
            from sqlalchemy import func, select

            from katip.store.models import Meeting

            count = await session.scalar(
                select(func.count())
                .select_from(Meeting)
                .where(
                    Meeting.owner_account_id == account.id,
                    Meeting.started_at >= now - timedelta(days=30),
                )
            )
        rows.append(
            {
                **_account_row(account),
                "last_seen": seen.isoformat() if seen else None,
                "active_sessions": active_sessions,
                "meetings_30d": count,
                "minutes_30d": round(sum((u.stt_seconds or 0) / 60 for _, u in own_usage), 1),
                "cost_30d": round(
                    sum(
                        (u.llm_usd or 0) + (u.stt_usd or 0) + (u.tts_usd or 0) for _, u in own_usage
                    ),
                    4,
                ),
                "active": account.enabled
                and (
                    account.id in live_owners or bool(seen and now - seen < timedelta(minutes=15))
                ),
            }
        )
    return rows


@router.post("/accounts", dependencies=[Depends(_panel_admin)])
async def create_account(req: AccountRequest, request: Request) -> dict:
    try:
        email = normalize_email(req.email)
        allowed = company_email(email, get_settings().panel_email_domains)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not allowed:
        raise HTTPException(
            status_code=422, detail="Yalnız izin verilen şirket e-postaları kullanılabilir"
        )
    if not _strong_enough(req.password):
        raise HTTPException(
            status_code=422, detail="Parola en az 16 karakter ve 3 karakter türü içermeli"
        )
    hashed = await asyncio.to_thread(hash_password, req.password)
    if existing := await repo.find_panel_account(email):
        account = await _update_account(
            existing.id, request, role=req.role, enabled=True, password_hash=hashed
        )
    else:
        account = await repo.set_panel_account(email, hashed, role=req.role)
        await repo.audit("admin", "panel_account_created", account_id=account.id, role=account.role)
    await repo.audit("admin", "panel_account_saved", account_id=account.id)
    return {
        "id": account.id,
        "email": account.email,
        "enabled": account.enabled,
        "role": account.role,
    }


@router.patch("/accounts/{account_id}", dependencies=[Depends(_panel_admin)])
async def update_account(account_id: int, req: AccountUpdate, request: Request) -> dict:
    return _account_row(
        await _update_account(account_id, request, role=req.role, enabled=req.enabled)
    )


@router.delete("/accounts/{account_id}", dependencies=[Depends(_panel_admin)])
async def disable_account(account_id: int, request: Request) -> dict:
    await _update_account(account_id, request, enabled=False, deleting=True)
    return {"disabled": True}


@router.post("/logout-all", dependencies=[Depends(_panel_admin)])
async def logout_all(request: Request, response: Response) -> dict:
    session = await current_admin_session(request)
    if session is None or not csrf_ok(request, session):
        raise HTTPException(status_code=401, detail="Oturum ya da CSRF anahtarı geçersiz")
    count = await repo.delete_all_admin_sessions()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    await repo.audit("admin", "admin_logout_all", sessions=count)
    return {"ok": True, "closed": count}

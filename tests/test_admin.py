"""Yönetim paneli: giriş güvenliği, CSRF, ayarlar, logo, sağlık ve sayfa başlıkları."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from katip.api import auth
from katip.api.main import app
from katip.config import get_settings

PASSWORD = "Doğru-Parola-2026!xyz"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("KATIP_ADMIN_PASSWORD_HASH", auth.hash_password(PASSWORD))
    get_settings.cache_clear()
    monkeypatch.setattr(auth, "guard", auth.LoginGuard())

    monkeypatch.setattr(auth, "_FAIL_DELAY_S", 0)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def login(client) -> str:
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrf"]


def test_password_hash_roundtrip() -> None:
    stored = auth.hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert auth.verify_password(PASSWORD, stored)
    assert not auth.verify_password(PASSWORD + "x", stored)
    assert not auth.verify_password(PASSWORD, "bozuk")


def test_login_sets_hardened_cookies_and_opens_admin_api(client) -> None:
    assert client.get("/api/admin/overview").status_code == 401
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    cookies = r.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("katip_admin="))
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag.lower() in session_cookie.lower()
    assert client.get("/api/auth/me").status_code == 200
    overview = client.get("/api/admin/overview").json()
    assert overview["bots"]["limit"] == 1 and overview["sessions"] == []


@pytest.mark.parametrize(
    "configured_email, submitted_email",
    [
        ("ÖZGÜR@EXAMPLE.COM", "özgür@example.com"),
        ("\u00a0özgür@example.com\u00a0", "\u00a0ÖZGÜR@EXAMPLE.COM\u00a0"),
        ("ayse@example.com", "\u00a0AYSE@EXAMPLE.COM\u00a0"),
    ],
)
def test_legacy_admin_login_normalizes_turkish_email_and_whitespace(
    client, monkeypatch, configured_email, submitted_email
) -> None:
    monkeypatch.setenv("KATIP_ADMIN_LOGIN_EMAIL", configured_email)
    get_settings.cache_clear()
    response = client.post("/api/auth/login", json={"email": submitted_email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    assert client.get("/api/admin/overview").status_code == 200


def test_wrong_passwords_lock_the_client(client) -> None:
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "yanlis"}).status_code == 401
    # Artık doğru parola da kilitli (kaba kuvvet koruması).
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 429 and "dakika" in r.json()["detail"]


def test_global_lock_against_distributed_attack() -> None:
    guard = auth.LoginGuard(per_client=5, global_limit=20)
    for i in range(20):
        guard.fail(f"istemci-{i}", now=100.0)  # her biri tek deneme, farklı IP
    assert guard.locked_for("yeni-istemci", now=101.0) > 0
    assert guard.locked_for("yeni-istemci", now=100.0 + 601) <= 0


def test_state_changes_need_csrf(client) -> None:
    csrf = login(client)
    body = {"company_name": "Example", "bot_display_name": "Katip", "retention_days": 365}
    assert client.put("/api/admin/settings", json=body).status_code == 403
    r = client.put("/api/admin/settings", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["values"]["company_name"] == "Example"
    assert get_settings().company_name == "Example"  # çalışan ayara uygulandı
    bad = client.put(
        "/api/admin/settings", json={"daily_meeting_minutes": 99999}, headers={"X-CSRF-Token": csrf}
    )
    assert bad.status_code == 422  # sınır dışı değer reddedilir


def test_logo_upload_validates_content(client) -> None:
    csrf = login(client)
    h = {"X-CSRF-Token": csrf}
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = client.post(
        "/api/admin/branding/logo", content=svg, headers={**h, "Content-Type": "image/png"}
    )
    assert r.status_code == 415  # uzantı/tür yalanına rağmen içerik kontrol edilir
    big = PNG + b"\x00" * (1024 * 1024)
    assert client.post("/api/admin/branding/logo", content=big, headers=h).status_code == 413
    assert client.post("/api/admin/branding/logo", content=PNG, headers=h).status_code == 200
    logo = client.get("/branding/logo")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
    assert logo.headers["x-content-type-options"] == "nosniff"
    assert client.delete("/api/admin/branding/logo", headers=h).status_code == 200
    assert client.get("/branding/logo").status_code == 404


def test_password_change_revokes_sessions(client) -> None:
    csrf = login(client)
    h = {"X-CSRF-Token": csrf}
    weak = client.post("/api/auth/password", json={"current": PASSWORD, "new": "kisa"}, headers=h)
    assert weak.status_code == 422
    new = "Yeni-Güçlü-Parola-2026#"
    r = client.post("/api/auth/password", json={"current": PASSWORD, "new": new}, headers=h)
    assert r.status_code == 200 and r.json()["relogin"] is True
    assert client.get("/api/auth/me").status_code == 401  # tüm oturumlar kapandı
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 401
    assert client.post("/api/auth/login", json={"password": new}).status_code == 200


def test_admin_page_has_strict_security_headers(client) -> None:
    r = client.get("/admin")
    assert r.status_code == 200 and "Katip" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert "max-age" in r.headers["strict-transport-security"]
    js = client.get("/admin/static/admin.js")
    assert js.status_code == 200 and "innerHTML" not in js.text  # veriler yalnız textContent ile


def test_panels_use_only_same_origin_assets_and_external_code(client) -> None:
    web = Path(__file__).resolve().parents[1] / "web"

    class Assets(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            assert "style" not in attrs
            assert not any(key.startswith("on") for key in attrs)
            assert tag != "style"
            if tag == "script":
                assert attrs.get("src"), "CSP forbids inline scripts"
            for key in ("src", "href"):
                value = attrs.get(key)
                if value and tag != "a":
                    parsed = urlsplit(value)
                    assert not parsed.scheme and not parsed.netloc, value
                    assert value.startswith("/") and not value.startswith("//"), value
                    assert client.get(value).status_code == 200, value

    for path in (
        web / "admin" / "index.html",
        web / "panel" / "index.html",
        web / "hesap" / "index.html",
    ):
        Assets().feed(path.read_text(encoding="utf-8"))
    for folder in (web / "admin", web / "panel", web / "fonts"):
        for path in folder.glob("*.css"):
            css = path.read_text(encoding="utf-8")
            assert not re.search(r"https?://|@import", css)
            for asset in re.findall(r"url\(['\"]?([^)'\"]+)", css):
                assert asset.startswith("/") and not asset.startswith("//"), asset
                assert client.get(asset).status_code == 200, asset


def test_admin_script_id_and_navigation_contracts() -> None:
    web = Path(__file__).resolve().parents[1] / "web" / "admin"
    html = (web / "index.html").read_text(encoding="utf-8")
    script = (web / "admin.js").read_text(encoding="utf-8")
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), "Element IDs must remain unique"
    selectors = set(re.findall(r'\$\$?\("#([\w-]+)', script))
    assert selectors <= set(ids), f"Missing script targets: {selectors - set(ids)}"
    tabs = set(re.findall(r'data-tab="([^"]+)"', html))
    views = set(re.findall(r'data-view="([^"]+)"', html))
    assert tabs == {
        "overview",
        "meetings",
        "usage",
        "settings",
        "branding",
        "people",
        "security",
        "users",
    }
    assert tabs <= views


@pytest.mark.parametrize("family", ["sora", "plus-jakarta-sans", "jetbrains-mono"])
@pytest.mark.parametrize("subset", ["latin", "latin-ext"])
def test_self_hosted_fonts_are_served_as_woff2(client, family, subset) -> None:
    response = client.get(f"/fonts/{family}-{subset}.woff2")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "font/woff2"
    assert response.content.startswith(b"wOF2")
    assert response.headers["x-content-type-options"] == "nosniff"
    license_response = client.get(f"/fonts/{family}-OFL.txt")
    assert license_response.status_code == 200 and "SIL OPEN FONT LICENSE" in license_response.text


def test_live_panel_has_the_same_strict_csp(client) -> None:
    login(client)
    response = client.get("/panel/cini-test")
    assert response.status_code == 200
    assert (
        response.headers["content-security-policy"]
        == client.get("/admin").headers["content-security-policy"]
    )
    assert "unsafe" not in response.headers["content-security-policy"]


def test_health_and_history_endpoints(client, monkeypatch) -> None:
    from katip.api.routes import admin

    async def fake_soniox():
        return True, "anahtar geçerli"

    async def fake_anthropic():
        return True, "anahtar geçerli"

    monkeypatch.setattr(admin, "_check_soniox", fake_soniox)
    monkeypatch.setattr(admin, "_check_anthropic", fake_anthropic)
    login(client)
    checks = client.get("/api/admin/health?refresh=true").json()["checks"]
    keys = {c["key"]: c for c in checks}
    assert keys["soniox"]["ok"] is True and keys["database"]["ok"] is True
    assert keys["voice_key"]["ok"] is True  # testlerde anahtar tanımlı
    assert client.get("/api/admin/meetings").json() == {"meetings": []}
    events = client.get("/api/admin/logins").json()["events"]
    assert events and events[0]["action"] == "admin_login"


def test_bearer_token_still_works_for_scripts(client) -> None:
    r = client.get("/api/admin/overview", headers={"Authorization": "Bearer test-admin"})
    assert r.status_code == 200


async def test_saved_settings_are_applied_on_startup(database) -> None:
    from katip import runtime_settings
    from katip.store import repo

    await repo.set_app_settings({"bot_display_name": "Example Katip", "daily_meeting_minutes": 600})
    await runtime_settings.load()
    s = get_settings()
    assert s.bot_display_name == "Example Katip" and s.daily_meeting_minutes == 600
    from katip.bot.supervisor import bot_environment

    assert bot_environment("x")["KATIP_BOT_DISPLAY_NAME"] == "Example Katip"

from __future__ import annotations

import secrets as _secrets

import pytest

from katip.config import Settings, get_settings
from katip.store import db

# Test hesap parolaları her çalıştırmada üretilir; repoda sabit parola tutulmaz
# (gizli bilgi tarayıcıları sabit test parolalarını gerçek sızıntı sanıyor).
TEST_PASSWORD = "Kt1!" + _secrets.token_urlsafe(18)
OTHER_TEST_PASSWORD = "Kt2!" + _secrets.token_urlsafe(18)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Her test kendi geçici veritabanı ve veri dizinini kullanır; ağa çıkan özellikler kapalı.

    Yerel .env dosyası (gerçek API anahtarları) testlere asla sızmaz.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv(
        "KATIP_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}"
    )
    monkeypatch.setenv("KATIP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KATIP_AUTO_REPORT", "false")
    for key in ("KATIP_DATA_KEY", "KATIP_DATA_KEY_FILE", "KATIP_DATA_KEY_PREVIOUS"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("KATIP_REQUIRE_ENCRYPTION", "false")
    monkeypatch.setenv("KATIP_ADMIN_TOKEN", "test-admin")
    for key in ("ANTHROPIC_API_KEY", "SONIOX_API_KEY", "DEEPGRAM_API_KEY"):
        monkeypatch.setenv(key, "")
    # Ses kimliği ayrı testlerde; oturum testlerinde modeli her seferinde yükleme.
    monkeypatch.setenv("KATIP_VOICE_ENABLED", "false")
    # Agent beklemesi (söz kesmeme) ayrı testte; diğer testlerde istek anında yürütülür.
    monkeypatch.setenv("KATIP_AGENT_TURN_WAIT_S", "0")
    # Ses izleri şifreli saklanır; testler her seferinde rastgele bir anahtar kullanır.
    import base64
    import secrets

    monkeypatch.setenv("KATIP_VOICEPRINT_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    monkeypatch.setenv("KATIP_PANEL_EMAIL_DOMAINS", '["example.com","example.net"]')
    monkeypatch.setenv("KATIP_CALENDAR_ORGANIZER_DOMAINS", '["example.com","example.net"]')
    monkeypatch.setenv("KATIP_M365_MAILBOX", "katip@example.com")
    get_settings.cache_clear()
    from katip.pipeline.session import registry

    monkeypatch.setattr(registry, "_sessions", {})
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_real_bots(monkeypatch: pytest.MonkeyPatch):
    """Güvenlik ağı: testler hiçbir koşulda gerçek bir bot (Chromium) süreci başlatamaz."""
    from katip.bot.supervisor import BotSupervisor

    async def refuse(self, session_id, url):
        raise RuntimeError("testte gerçek bot süreci başlatılamaz")

    # Yalnızca bot başlatma engellenir; Playwright'ın kendi süreçleri (PDF, ekran görüntüsü
    # testleri) etkilenmez. Testler gerekirse örnek düzeyinde kendi sahte start'ını koyar.
    monkeypatch.setattr(BotSupervisor, "start", refuse)


@pytest.fixture
async def database():
    db.configure()
    await db.init_db()
    yield
    await db.dispose()


@pytest.fixture
def api_client():
    """HTTP, panel ve ingest aynı döngüyü ve düzenli uygulama kapanışını kullanır."""
    from fastapi.testclient import TestClient

    from katip.api.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def parser_service(monkeypatch):
    """Container/ağ soketi kullanmadan gerçek istemci ve parser servisi yolu."""
    from types import SimpleNamespace

    import httpx

    from katip.documents.server import create_app

    settings = get_settings()
    settings.parser_url = "http://parser:8001"
    settings.parser_key = "parser-test-" + "s" * 40
    server_settings = Settings(
        _env_file=None,
        parser_key=settings.parser_key,
        document_max_bytes=settings.document_max_bytes,
        document_parser_timeout_s=settings.document_parser_timeout_s,
    )
    app = create_app(server_settings)
    real_client = httpx.AsyncClient

    def client(**kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=app)
        return real_client(**kwargs)

    monkeypatch.setattr("katip.documents.client.httpx.AsyncClient", client)
    return SimpleNamespace(settings=server_settings, app=app, client=client)

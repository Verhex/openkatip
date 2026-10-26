"""Uygulama ayarları. Tüm ortam değişkenleri KATIP_ öneki ile okunur."""

import os
import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from katip.identity import normalize_email


def organizer_domains(values: list[str]) -> list[str]:
    """Alan adlarını tam eşleşme için doğrular; joker ve alt alan adı örtüşmesi yoktur."""
    result = []
    for value in values:
        if not value.isascii():
            raise ValueError("Alan adı yalnız ASCII karakterleri içerebilir")
        value = value.strip().lower()
        if len(value) > 253 or not re.fullmatch(
            r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
            value,
        ):
            raise ValueError("Geçerli bir alan adı girin; joker kullanılamaz")
        result.append(value)
    return sorted(set(result))


def organizer_emails(values: list[str]) -> list[str]:
    """Güven listesini küçük harfle ve tekrarsız saklar."""
    result = []
    for value in values:
        value = normalize_email(value)
        if re.search(r"[\x00-\x20\x7f]", value):
            raise ValueError("E-posta kontrol karakteri içeremez")
        organizer_domains([value.rsplit("@", 1)[1]])
        result.append(value)
    return sorted(set(result))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KATIP_", env_file=".env", extra="ignore", case_sensitive=False
    )

    # Claude — raporlar kaliteli model, canlı komutlar hızlı model.
    anthropic_api_key: str = Field("", validation_alias="ANTHROPIC_API_KEY")
    # Rapor ve canlı model kimlikleri kuruluma göre ayrı ayrı seçilir.
    llm_model: str = "claude-sonnet-5-5"
    # Canlı cevaplarda efor ve düşünme desteği seçilen sağlayıcıya bağlıdır.
    agent_model: str = "claude-sonnet-5-5"
    agent_effort: Literal["low", "medium", "high"] = "low"
    # Destekleyen sağlayıcılarda canlı cevaplar için adaptif düşünme.
    agent_thinking: bool = True
    # Raporda düşünme açık: karar/öneri ayrımı ve aksiyon ataması daha doğru; toplantı
    # başına tek çağrı olduğu için maliyet farkı küçük.
    report_thinking: bool = True
    # Toplantı sonu raporu Message Batches API ile (%50 ucuz; genelde birkaç dakika, en geç
    # 24 saat). Ara raporlar ("Katip, raporu hazırla") her zaman anında üretilir.
    report_batch: bool = True
    # Batch bu kadar saniyede bitmezse iptal edilir ve rapor anında üretilir (gecikme sınırı).
    report_batch_max_wait_s: int = 1200
    # Boş STT zinciri eski stt_provider seçimini korur; yedekler açıkça eklenir.
    llm_live_chain: list[Literal["anthropic", "bedrock", "vertex"]] = ["anthropic"]
    llm_report_chain: list[Literal["anthropic", "bedrock", "vertex"]] = ["anthropic"]
    stt_chain: list[Literal["soniox", "assemblyai", "deepgram"]] = []
    provider_deadline_s: float = Field(60, gt=0)
    provider_attempt_timeout_s: float = Field(20, gt=0)
    report_deadline_s: float = Field(600, gt=0)
    report_attempt_timeout_s: float = Field(120, gt=0)
    bedrock_region: str = "eu-central-1"
    bedrock_aws_profile: str = ""
    bedrock_inference_profile: Literal["eu"] = "eu"
    bedrock_model_prefix: str = "eu.anthropic."
    bedrock_model_ids: dict[str, str] = {}
    vertex_project: str = ""
    vertex_region: Literal["eu"] = "eu"
    # Sağlayıcı/model/bölge anahtarı; oranlar USD / milyon token, listeden farklıysa.
    provider_llm_prices: dict[str, dict[str, float]] = {}
    provider_stt_prices: dict[str, float] = {}
    assemblyai_api_key: str = Field("", validation_alias="ASSEMBLYAI_API_KEY")
    assemblyai_region: Literal["eu"] = "eu"

    @model_validator(mode="after")
    def validate_provider_chains(self):
        for chain in (self.llm_live_chain, self.llm_report_chain, self.stt_chain):
            if len(chain) != len(set(chain)):
                raise ValueError("Sağlayıcı zincirinde tekrar olamaz")
        if not self.llm_live_chain or not self.llm_report_chain:
            raise ValueError("LLM zinciri boş olamaz")
        if "bedrock" in self.llm_live_chain + self.llm_report_chain:
            if not self.bedrock_region.startswith("eu-"):
                raise ValueError("Bedrock bölgesi Avrupa'da olmalı")
        return self

    # Katip'e seslenilen istekleri kim yorumlar: "llm" = Claude araç çağrılarıyla (agent),
    # "rules" = yalnızca kural tabanlı komutlar (API anahtarı yoksa zaten bu kullanılır).
    agent_mode: Literal["llm", "rules"] = "llm"
    # Söz kesmeme: istek sahibi bu kadar saniye susmadan Katip işe başlamaz. Cümle yarım
    # görünüyorsa ("Katip, karar şu:") daha uzun beklenir; kişi konuştukça bekleme uzar.
    agent_turn_wait_s: float = 1.5
    agent_incomplete_wait_s: float = 6.0
    # Uzun, duraksamalı anlatımlar erken kesilmesin.
    agent_turn_max_s: float = 120.0
    # Sesli cevap öncesinde kısa bir doğal boşluk beklenir; açık mikrofon cevabı engellemez.
    voice_busy_window_s: float = 1.2
    voice_floor_wait_s: float = 2.0
    voice_ambient_window_s: float = 1.5
    voice_interrupt_sustain_s: float = 1.2
    voice_resume_quiet_s: float = 1.2

    # Raporlar
    company_name: str | None = None
    data_dir: str = "data"
    data_key: str = Field("", repr=False)
    data_key_file: str = ""
    data_key_previous: str = Field("", repr=False)
    require_encryption: bool = False
    report_lang: str = "Türkçe"
    report_include_transcript: bool = True  # Word ekinde transkript
    report_transcript_pdf: bool = False  # PDF'te de transkript (varsayılan: PDF kısa kalsın)
    # Raporlarda gösterilen saat dilimi (sunucu UTC çalışır).
    timezone: str = "Europe/Istanbul"
    auto_report: bool = True

    # Eski ses kimliği ayarları uyumluluk için tutulur; MeetingSession özelliği açmaz.
    voice_enabled: bool = False
    voice_model: str = "wespeaker-resnet34-lm"
    # Toplantı sesiyle kalibre edilmeli (Opus codec skorları düşürebilir).
    voice_match_threshold: float = 0.55
    voice_match_margin: float = 0.08
    voice_min_enroll_s: float = 8.0
    # Tanınan kişiyi selamlama: "chat" | "off"
    greeting: str = "chat"
    # Model dosyası imaja gömülüyse (Docker/Railway) yolu; yoksa data_dir/models'a indirilir.
    voice_model_path: str | None = None

    # Cevap modu: "chat" (tamamen yazılı) | "voice" (tamamen sesli, Soniox TTS).
    # Toplantıda "Katip, sesli cevap ver" / "/yazılı" ya da panelden değiştirilir.
    reply_mode: Literal["chat", "voice"] = "chat"
    tts_model: str = "tts-rt-v2"
    # Ses kimliği kuruluma göre değiştirilebilir; sağlayıcı hesabında doğrulanmalıdır.
    tts_voice: str = "Rania"
    tts_ws_url: str = "wss://tts-rt.soniox.com/tts-websocket"
    # Tarayıcıdaki ses bağlamı 48 kHz: aynı hızda üretilen parçalar yeniden örneklenmez,
    # parça ekleri arasında çıtırtı oluşmaz.
    tts_sample_rate: int = 48_000
    # auto: normalizer-tr varsa kullan; ntr: kütüphaneyi seç; katip: yalnız yerel kurallar.
    # Kütüphane hatasında her iki seçim de yerel kurallara döner.
    tts_normalizer: Literal["auto", "ntr", "katip"] = "auto"
    # Telaffuz sözlüğü: TTS'e gitmeden önce yazılış → okunuş. Marka ve yabancı kelimeler için.
    # Ortam değişkeninde JSON: KATIP_TTS_LEXICON='{"Example": "Eksampıl", "API": "ey pi ay"}'
    tts_lexicon: dict[str, str] = {}

    # Yönetim uçları için Bearer token (boşsa yazma/silme uçları kapalı).
    admin_token: str = ""
    # Yönetim paneli (/admin) parolasının scrypt özeti (katip.api.auth.hash_password ile üretilir).
    # Parolanın kendisi hiçbir yerde tutulmaz. Panelden değiştirilirse veritabanındaki geçerlidir.
    admin_password_hash: str = ""
    # Önceden panelde açılmış şirket hesapları e-posta + kişisel parola ile giriş yapar.
    panel_email_domains: list[str] = []
    # Eski tek parolalı giriş yalnızca ilk hesapları açmak için kullanılabilir.
    admin_login_email: str = ""
    # Microsoft Graph: uygulama izniyle Katip takvimindeki davetlilerin e-postaları.
    m365_tenant_id: str = ""
    m365_client_id: str = ""
    m365_client_secret: str = ""
    m365_mailbox: str = ""
    mail_enabled: bool = False
    mail_internal_domains: str = ""
    teams_bot_app_id: str = ""
    teams_bot_secret: str = Field("", repr=False)
    teams_bot_secret_file: str = ""
    teams_bot_tenant_id: str = ""
    teams_notify: bool = True
    teams_app_package_id: str = ""
    teams_privacy_url: str = ""
    teams_terms_url: str = ""

    @field_validator("mail_internal_domains")
    @classmethod
    def _mail_domains(cls, value: str) -> str:
        return ",".join(organizer_domains(value.split(","))) if value.strip() else ""

    calendar_autojoin: bool = False
    calendar_join_policy: Literal["kapalı", "onay", "güvenilir", "hepsi"] = "kapalı"
    calendar_daily_autojoins: int = Field(20, ge=0, le=1000)
    calendar_max_pending: int = Field(200, ge=1, le=1000)
    calendar_poll_s: int = Field(60, ge=30, le=3600)
    calendar_organizer_domains: list[str] = Field(default=[], max_length=100)
    calendar_organizer_emails: list[str] = Field(default=[], max_length=200)

    _calendar_domains = field_validator("calendar_organizer_domains")(organizer_domains)
    _calendar_emails = field_validator("calendar_organizer_emails")(organizer_emails)

    @model_validator(mode="after")
    def _legacy_calendar_policy(self):
        if "calendar_join_policy" not in self.model_fields_set:
            self.calendar_join_policy = "güvenilir" if self.calendar_autojoin else "kapalı"
        return self

    admin_idle_minutes: int = 60  # hareketsizlikte oturum düşer
    admin_session_hours: int = 12  # oturumun en uzun ömrü
    admin_cookie_secure: bool = True  # yalnız HTTPS üzerinden gönderilen çerez
    # Railway son X-Forwarded-For değerini kullanır. CF modu yalnız açık proxy listesine güvenir.
    client_ip_header: Literal["x-forwarded-for", "cf-connecting-ip"] = "x-forwarded-for"
    client_ip_trusted_proxies: list[str] = ["127.0.0.1", "::1"]

    @field_validator("client_ip_trusted_proxies")
    @classmethod
    def _proxy_addresses(cls, values: list[str]) -> list[str]:
        from ipaddress import ip_address

        return [str(ip_address(value)) for value in values]

    # İmzalı görüntüleme linkleri için anahtar; boşsa yönetici token'ından türetilir.
    secret_key: str = ""
    # Panel/rapor paylaşım linklerinin geçerlilik süresi (gün).
    share_link_days: float = 7
    # Rapor/panel linklerinde kullanılacak genel adres; boşsa Railway'in verdiği alan adı.
    public_base_url: str = ""
    # Ses izlerini şifreleyen anahtar (base64, 32 bayt; AES-256-GCM). Tanımlı değilse ses
    # tanıma kapalıdır: biyometrik veri şifresiz saklanmaz. Veritabanından ayrı tutulmalı.
    voiceprint_key: str = ""

    # KVKK: toplantıya girince aydınlatma metnini chat'e yaz ("chat"), sesli de oku
    # ("voice") ya da kapalı ("off"). Geç katılanlara kısa hatırlatma yapılır.
    privacy_notice_on_join: Literal["chat", "voice", "off"] = "chat"
    # Aydınlatma metninde KVKK başvuru adresi (ör. kvkk@example.com) ve saklama süresi ifadesi.
    privacy_contact: str = ""
    privacy_server_location: str = Field(
        "Katip şu an için yurt dışındaki sunucularda çalışmaktadır", min_length=1, max_length=300
    )

    @field_validator("privacy_server_location")
    @classmethod
    def _server_location(cls, value: str) -> str:
        value = value.strip()
        if not value or re.search(r"[\x00-\x1f\x7f]", value):
            raise ValueError("Sunucu konumu boş olamaz ve kontrol karakteri içeremez")
        return value

    # Boşsa saklama süresinden türetilir ("12 ay").
    retention_text: str = ""
    # Toplantı verileri bu kadar gün sonra silinir (0 = silme kapalı). Teşhis dosyaları ayrıca.
    retention_days: int = 365
    debug_retention_days: int = 7
    # Toplantı sonu raporu hazır olunca imzalı linki toplantı chat'ine yaz.
    post_report_to_chat: bool = True
    # Her toplantıda sayfa teşhisi (HTML + ekran görüntüsü) kaydet: kişisel veri içerdiği için
    # varsayılan kapalı; katılma başarısız olursa teşhis yine kaydedilir.
    auto_diagnose: bool = False

    # Harcama sigortaları — sürpriz faturaya karşı uygulama tarafı sınırlar.
    # (Asıl sert sınırlar sağlayıcı konsollarında: Soniox bütçe, Anthropic workspace limiti,
    # Railway hard limit. Bunlar ikinci kat.)
    max_meeting_minutes: int = 90  # bot bu süre dolunca toplantıdan çıkar ("tam" mod)
    # Mod: "tam" = agent + sesli cevap + çeviri; "kayit" = gün boyu sessiz kayıt (yalnız
    # transkript ve rapor; seslenilirse chat'ten cevap, ses ve çeviri kapalı).
    default_mode: Literal["tam", "kayit"] = "tam"
    record_max_minutes: int = 600  # "kayit" modunda en uzun süre
    record_idle_leave_minutes: int = 60  # "kayit" modunda bu kadar sessizlikte ayrıl
    # Toplantıda Katip'ten başka kimse görünmüyor ve kimse konuşmuyorsa bu kadar dakikada ayrıl.
    alone_leave_minutes: int = 5
    # Soniox tek bağlantıyı en fazla 300 dk açık tutar: süre dolmadan akış yenilenir.
    stt_rotate_minutes: int = 280
    max_concurrent_bots: int = 1
    daily_meeting_minutes: int = 660  # gün içinde toplam bot süresi (gün boyu kayıt dahil)
    max_agent_calls_per_meeting: int = 60  # Claude'a giden canlı soru/özet sayısı
    presentation_narrations_per_meeting: int = Field(80, ge=0)
    # Bu kadar dakika kimse konuşmazsa bot ayrılır ve raporu hazırlar (boş odada kalıp
    # ses akıtmasın). 0 = kapalı.
    idle_leave_minutes: int = 15
    # Ses kaydında parçalar iki belirgin kümeye ayrılıyorsa (kümeler arası benzerlik bunun
    # altında ve küçük küme en az şu payda) iki kişi karışmıştır; kayıt tamamlanmaz
    # (aynı cihazdan konuşan birden fazla kişiyi tek kimlikle kaydetmemek için).
    voice_mixed_max_similarity: float = 0.5
    # Cümle bazlı ses doğrulaması için en kısa cümle (daha kısası güvenilmez).
    voice_segment_min_s: float = 2.0
    voice_mixed_min_share: float = 0.25
    # Bot aynı konteynerde çalışıyorsa (Railway tek servis) ingest yalnızca yerelden kabul edilir.
    ingest_local_only: bool = False

    # STT — Soniox birincil; AssemblyAI isteğe bağlı yedek, Deepgram yalnız açık seçimle.
    stt_provider: Literal["soniox", "assemblyai", "deepgram"] = "soniox"
    soniox_api_key: str = Field("", validation_alias="SONIOX_API_KEY")
    soniox_model: str = "stt-rt-v5"
    soniox_ws_url: str = "wss://stt-rt.soniox.com/transcribe-websocket"
    # Soniox veri bölgesi: "eu" seçilince ses ve metin AB'de (Frankfurt) işlenir ve kalır.
    # Soniox projede AB bölgesini açmadan (support@soniox.com) "eu" seçilirse bağlantı kurulamaz.
    soniox_region: Literal["us", "eu"] = "us"

    def soniox_endpoint(self, url: str) -> str:
        """Bölgeye göre uç nokta: stt-rt.soniox.com → stt-rt.eu.soniox.com."""
        if self.soniox_region == "eu" and ".eu.soniox.com" not in url:
            for host in ("stt-rt.soniox.com", "tts-rt.soniox.com", "api.soniox.com"):
                url = url.replace(host, host.replace(".soniox.com", ".eu.soniox.com"))
        return url

    # Konuşma sonunu ne kadar çabuk kesinleştirsin (500–3000 ms). Düşük = hızlı altyazı.
    soniox_max_endpoint_delay_ms: int = 1000
    # Şirkete özel terimler (ürün adları, kısaltmalar) — tanıma doğruluğunu artırır.
    # Ortam değişkeninde JSON liste: KATIP_STT_TERMS='["Example","Katip"]'
    stt_terms: list[str] = []
    deepgram_api_key: str = Field("", validation_alias="DEEPGRAM_API_KEY")

    # Diller
    source_lang: str = "tr"
    target_lang: str = "en"

    # Ses formatı — tüm zincir boyunca sabit
    sample_rate: int = 16_000
    frame_ms: int = 20

    # Altyapı
    # Varsayılan SQLite: kurulum gerektirmez, 5-10 kişilik ekip ve açık kaynak sürüm için yeterli.
    # Kurumsal kurulumda Postgres: postgresql+asyncpg://katip:katip@host:5432/katip
    database_url: str = "sqlite+aiosqlite:///data/katip.db"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    ingest_ws_url: str = "ws://localhost:8000/ws/ingest"
    # Docker işçilerinde sunucu diski ve sırları bulunmaz. Yerel mod güvenilen tek
    # kurulum içindir; işletim sistemi düzeyinde yalıtım sağlamaz.
    bot_backend: Literal["local", "docker", "remote"] = "local"
    bot_image: str = "katip-worker:local"
    bot_image_digest: str = ""
    bot_network: str = "katip-workers"
    bot_worker_ingest_url: str = "ws://api:8000/ws/ingest"
    bot_memory_mb: int = Field(1024, ge=256, le=16384)
    bot_cpus: float = Field(1.0, ge=0.25, le=16)
    bot_block_private_network: bool = True
    # Her yönetici yalnız kendi işçi kiralarını alabilir, yenileyebilir ve bitirebilir.
    worker_manager_keys: dict[str, str] = {}
    worker_lease_s: int = Field(60, ge=30, le=300)
    worker_queue_timeout_s: int = Field(300, ge=30, le=3600)
    bot_disconnect_timeout_s: int = Field(60, ge=15, le=300)
    worker_api_url: str = ""
    # Yalnız Compose iç ağındaki tek servis adı için HTTP istisnası.
    worker_api_internal_hostname: str = ""
    worker_manager_id: str = ""
    worker_manager_key: str = ""
    document_max_bytes: int = Field(8 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    document_max_per_meeting: int = Field(8, ge=1, le=20)
    document_context_chars: int = Field(24000, ge=2000, le=60000)
    document_parser_timeout_s: int = Field(20, ge=1, le=60)
    parser_url: str = ""
    parser_key: str = ""
    parser_key_file: str = ""

    # Bot
    bot_display_name: str = "Katip"
    # Chromium korumalı alanı: "auto" önce dener, ortam izin vermezse korumasız açar ve loglar;
    # "on" açılamazsa bot başlamaz; "off" hiç denemez.
    bot_sandbox: Literal["auto", "on", "off"] = "on"
    # Site yalıtımı (her site ayrı süreç) açık olmalı; Teams sesinde sorun olursa geçici kapatılır.
    bot_site_isolation: bool = True
    bot_headless: bool = False
    # Teams harici botları lobide tutuyor (Haziran 2026'dan beri varsayılan): kabul için süre.
    bot_join_timeout_s: int = 900

    # Entegrasyonlar
    monday_api_token: str = Field("", validation_alias="MONDAY_API_TOKEN")

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000


@lru_cache
def get_settings() -> Settings:
    # Tarayıcı işçileri paylaşılan .env dosyasından sunucu sırlarını yeniden okumaz.
    if os.environ.get("KATIP_BOT_TOKEN"):
        return Settings(_env_file=None)
    return Settings()

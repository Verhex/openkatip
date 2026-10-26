"""Kalıcı veri modeli.

Tasarım notları:
- Ses izi (Voiceprint) KVKK'da biyometrik = özel nitelikli veridir. Yalnızca açık
  rıza kaydı (Consent) olan kişiler için tutulur ve kişi silinince CASCADE ile gider.
- "Beni unut": Person + Voiceprint silinir, geçmiş segmentlerde konuşmacı adı
  "Anonim katılımcı" olur (person_id NULL). Toplantı kaydı bütün kalır.
- AuditLog kişisel veri taşımaz; silinen kişiye yalnızca tek yönlü özetle (hash) atıf yapar.
- Tipler Postgres ve SQLite'ta (testler) aynı çalışacak şekilde taşınabilir seçildi.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(UTC)


ANONYMOUS_SPEAKER = "Anonim katılımcı"
# Transkriptte Katip'in kendi cevaplarının konuşmacı adı.
KATIP_SPEAKER = "Katip"


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"
    GUEST = "guest"


class Base(DeclarativeBase):
    pass


class EncryptedContent:
    """İçerik alanlarının şifreli kopyası; eski sütunlar geçiş boyunca korunur."""

    encrypted_content: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)


class EncryptionInstallation(Base):
    __tablename__ = "encryption_installation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    installation_id: Mapped[str] = mapped_column(String(32))
    encryption_started: Mapped[bool] = mapped_column(default=False, server_default="false")
    migration_completed: Mapped[bool] = mapped_column(default=False, server_default="false")


class MeetingTombstone(Base):
    """Silinen kimlikler yeniden kullanılamaz; DEK ve içerik barındırmaz."""

    __tablename__ = "meeting_tombstones"
    meeting_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MeetingDataKey(Base):
    __tablename__ = "meeting_data_keys"

    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE", deferrable=True, initially="DEFERRED"),
        primary_key=True,
    )
    key_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)


class CalendarJoin(Base):
    """Graph oluşumuna özel kalıcı karar ve tek seferlik katılım kaydı."""

    __tablename__ = "calendar_joins"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="claimed")
    meeting_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approval_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approved_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("panel_accounts.id", ondelete="SET NULL"), nullable=True
    )
    auto_day: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CalendarJoinGuard(Base):
    """Kuyruk ve günlük kota işlemlerini süreçler arasında sıraya alır."""

    __tablename__ = "calendar_join_guard"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    queue_limited: Mapped[bool] = mapped_column(Boolean, default=False)
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_manual_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WorkerJob(EncryptedContent, Base):
    """Durable single-attempt job; losing a lease never auto-rejoins a meeting."""

    __tablename__ = "worker_jobs"

    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    backend: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    spec: Mapped[dict] = mapped_column(JSON)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), index=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True)
    role: Mapped[str] = mapped_column(String(16), default=Role.MEMBER)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    department: Mapped[str | None] = mapped_column(String(200), nullable=True)
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), nullable=True
    )
    # Toplantı arayüzlerinde görünebilecek diğer adlar ("Deniz", "deniz.s").
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Silme tamamlanana kadar yeniden deneme için kimlik bilgileri korunur.
    erasure_pending: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    voiceprint: Mapped[Voiceprint | None] = relationship(
        back_populates="person", cascade="all, delete-orphan", uselist=False
    )
    consents: Mapped[list[Consent]] = relationship(
        back_populates="person", cascade="all, delete-orphan"
    )

    def matches_name(self, name: str) -> bool:
        wanted = name.strip().casefold()
        return wanted in {n.casefold() for n in [self.display_name, *(self.aliases or [])]}


class Voiceprint(Base):
    __tablename__ = "voiceprints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), unique=True)
    # float32 vektör, L2-normalize edilmiş.
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    dim: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(100))
    speech_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    samples: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    person: Mapped[Person] = relationship(back_populates="voiceprint")


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))  # "voiceprint"
    meeting_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Rızanın nasıl alındığı: "sesli: 'onaylıyorum'" / "panel" / "chat: /sesimi-tani"
    evidence: Mapped[str] = mapped_column(Text)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    person: Mapped[Person] = relationship(back_populates="consents")


class Meeting(EncryptedContent, Base):
    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("panel_accounts.id", ondelete="SET NULL"), nullable=True
    )
    platform: Mapped[str | None] = mapped_column(String(16), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_lang: Mapped[str] = mapped_column(String(8), default="tr")
    target_lang: Mapped[str | None] = mapped_column(String(8), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="pending")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Claude'un ürettiği yapılandırılmış rapor (MeetingReport.model_dump()).
    report: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    # {"pdf": "data/reports/<id>/rapor.pdf", "docx": "..."}
    documents: Mapped[dict] = mapped_column(JSON, default=dict)

    segments: Mapped[list[MeetingSegment]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", order_by="MeetingSegment.start_s"
    )


class TeamsConversation(EncryptedContent, Base):
    __tablename__ = "teams_conversations"
    __table_args__ = (UniqueConstraint("meeting_id", "account_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("panel_accounts.id", ondelete="CASCADE"), nullable=True
    )
    reference: Mapped[dict] = mapped_column(JSON)
    report_notified: Mapped[bool] = mapped_column(default=False)
    card_version: Mapped[int] = mapped_column(default=0)


class MeetingMailDraft(EncryptedContent, Base):
    __tablename__ = "meeting_mail_drafts"
    __table_args__ = (
        CheckConstraint("status IN ('taslak', 'gönderiliyor', 'gönderildi', 'hata')"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), unique=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="taslak")
    payload: Mapped[dict] = mapped_column(JSON)
    approved_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("panel_accounts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    client_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    permission_error: Mapped[bool] = mapped_column(Boolean, default=False)


class MeetingGrant(Base):
    """Panel hesabının toplantıya özel okuma veya yönetme izni."""

    __tablename__ = "meeting_grants"
    __table_args__ = (CheckConstraint("permission IN ('read', 'manage')"),)

    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("panel_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    permission: Mapped[str] = mapped_column(String(16))


class PreparationDocument(EncryptedContent, Base):
    """Text explicitly shared with this meeting; private speaker notes are excluded."""

    __tablename__ = "preparation_documents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64))
    digest_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    presentation: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    pages: Mapped[list[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MeetingSegment(EncryptedContent, Base):
    __tablename__ = "meeting_segments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), nullable=True, index=True
    )
    speaker_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    speaker_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    speaker_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    translation: Mapped[str | None] = mapped_column(Text, nullable=True)
    lang: Mapped[str] = mapped_column(String(8))

    meeting: Mapped[Meeting] = relationship(back_populates="segments")


class MeetingNote(EncryptedContent, Base):
    """Toplantıda Katip'e aldırılan not / aksiyon ("Katip, not al: …")."""

    __tablename__ = "meeting_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # "note" | "action"
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MeetingParticipant(EncryptedContent, Base):
    """Toplantıda görünen katılımcı; şemadaki kişiyle eşleşirse e-postasıyla."""

    __tablename__ = "meeting_participants"
    __table_args__ = (UniqueConstraint("meeting_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), nullable=True
    )
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    platform_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    identity_source: Mapped[str] = mapped_column(String(32), default="unknown")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MeetingUsage(Base):
    """Toplantının kaynak kullanımı ve tahmini maliyeti (katip/usage.py fiyatlarıyla)."""

    __tablename__ = "meeting_usage"

    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_usd: Mapped[float] = mapped_column(Float, default=0.0)
    stt_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    stt_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tts_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    tts_usd: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    providers: Mapped[dict] = mapped_column(JSON, default=dict)


class MeetingIdentity(EncryptedContent, Base):
    """Davet/panel kaynaklı e-posta listesi; aynı isimli kişiler ayrı tutulur."""

    __tablename__ = "meeting_identities"
    __table_args__ = (UniqueConstraint("meeting_id", "email"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    platform_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(String(32))


class AdminSession(Base):
    """Yönetim paneli oturumu. Tarayıcıdaki çerez yalnızca rastgele anahtarı taşır; burada
    anahtarın ve CSRF anahtarının yalnızca özeti (SHA-256) tutulur."""

    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    client: Mapped[str | None] = mapped_column(String(64), nullable=True)  # IP özeti
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("panel_accounts.id", ondelete="CASCADE"), nullable=True
    )


class PanelAccount(Base):
    """Yönetici tarafından açılmış şirket hesabı; genel kayıt yoktur."""

    __tablename__ = "panel_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    role: Mapped[str] = mapped_column(String(16), default="operator", server_default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AppSetting(Base):
    """Panelden değiştirilen ayarlar (ortam değişkenlerinin üstüne yazılır)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MeetingAsset(EncryptedContent, Base):
    """Toplantıda alınan ekran görüntüsü vb. Görüntü dosyada, açıklaması (Claude) burada."""

    __tablename__ = "meeting_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # "screenshot"
    number: Mapped[int] = mapped_column(Integer)  # toplantı içindeki sıra (1, 2, …)
    path: Mapped[str] = mapped_column(Text)
    at_s: Mapped[float] = mapped_column(Float)  # toplantının kaçıncı saniyesi
    requested_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Directive(Base):
    """Yöneticinin onaylı kalıcı talimatı; agent ve rapor prompt'larına eklenir."""

    __tablename__ = "directives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    meeting_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(200))  # "system", "person:<hash>", "admin"
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

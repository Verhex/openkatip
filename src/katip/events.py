"""Bot ↔ API ↔ panel arasındaki tüm mesaj sözleşmeleri.

Ses verisi WebSocket üzerinden **binary** frame olarak akar (16 kHz mono int16 PCM,
başına 8 baytlık header: uint32 track_id + uint32 sequence). Tarayıcı tüm uzak ses
track'lerini tek akışta karıştırdığı için track_id şu an hep 0'dır. Geri kalan her
şey bu JSON modelleridir.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from katip.identity import ParticipantIdentity


def _now() -> datetime:
    return datetime.now(UTC)


class Platform(StrEnum):
    MEET = "meet"
    TEAMS = "teams"
    ZOOM = "zoom"


class SessionState(StrEnum):
    PENDING = "pending"
    JOINING = "joining"
    WAITING_ADMISSION = "waiting_admission"
    ACTIVE = "active"
    ENDED = "ended"
    FAILED = "failed"


# --- Bot → sunucu ---------------------------------------------------------


class BotEvent(BaseModel):
    kind: str
    at: datetime = Field(default_factory=_now)


class BotStatus(BotEvent):
    kind: Literal["status"] = "status"
    state: SessionState
    detail: str | None = None


class TrackOpened(BotEvent):
    """Sayfada yeni bir uzak ses track'i yakalandı."""

    kind: Literal["track_opened"] = "track_opened"
    track_id: int
    label: str | None = None
    stream_id: str | None = None


class TrackClosed(BotEvent):
    kind: Literal["track_closed"] = "track_closed"
    track_id: int


class RosterUpdate(BotEvent):
    """DOM'dan okunan katılımcı listesi ve o an konuşan kişi."""

    kind: Literal["roster"] = "roster"
    participants: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=200
    )
    active_speakers: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=200
    )
    identities: list[ParticipantIdentity] = Field(default_factory=list, max_length=200)


class ChatMessage(BotEvent):
    kind: Literal["chat"] = "chat"
    author: str = Field(max_length=200)
    text: str = Field(max_length=4000)


# --- Sunucu → panel -------------------------------------------------------


class Segment(BaseModel):
    """Bir konuşma parçası. Aynı `id` ile büyüyerek birden çok kez yayınlanır (upsert)."""

    id: str
    session_id: str
    speaker: str | None = None
    speaker_label: str | None = None
    speaker_email: str | None = None
    start_s: float
    end_s: float
    text: str
    lang: str
    is_final: bool = False
    translation: str | None = None


class Translation(BaseModel):
    """Bir segmentin çevirisi. Soniox'ta kelime kelime büyür; `segment_id` ile upsert edilir."""

    segment_id: str
    session_id: str
    text: str
    lang: str
    is_final: bool = False


class PanelEvent(BaseModel):
    kind: Literal[
        "segment",
        "translation",
        "status",
        "roster",
        "note",
        "approval",
        "mode",
        "paused",
        "screenshot",
        "error",
    ]
    at: datetime = Field(default_factory=_now)
    segment: Segment | None = None
    translation: Translation | None = None
    state: SessionState | None = None
    roster: RosterUpdate | None = None
    text: str | None = None
    approval_id: str | None = None
    url: str | None = None

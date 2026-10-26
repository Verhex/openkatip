"""Platform adaptörleri: her toplantı platformu için katılma akışı ve DOM seçicileri."""

from __future__ import annotations

import abc
import asyncio
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import Page

from katip.events import Platform


@dataclass(slots=True)
class Selectors:
    """capture.js'in DOM'dan katılımcı ve chat okumak/yazmak için kullandığı seçiciler.

    Chat seçicileri platform arayüzleri değiştikçe güncellenmeli; canlı toplantıda
    kalibre edilene kadar "en iyi tahmin" niteliğindedir.
    """

    participant: str | None = None
    name: str | None = None
    # Ad bir öznitelikteyse (Teams: kutucuğun data-tid'i kişinin adı).
    name_attr: str | None = None
    active_speaker: str | None = None
    # Konuşma göstergesi hep DOM'da duruyor, yalnızca görünümü değişiyorsa (Teams) aktiflik
    # öğenin varlığına değil hesaplanan stiline (kenarlık/gölge) bakılarak anlaşılır.
    active_speaker_by_style: bool = False
    # Göstergenin aranacağı kutucuk kökü (katılımcı öğesinin atası).
    tile_root: str | None = None
    chat_toggle: str | None = None
    chat_input: str | None = None
    chat_send: str | None = None
    chat_message: str | None = None
    chat_author: str | None = None
    chat_text: str | None = None
    chat_own_message: str | None = None
    # Mikrofon düğmeleri: mic_unmute = mikrofon kapalıyken görünen "aç" düğmesi.
    mic_unmute: str | None = None
    mic_mute: str | None = None

    def to_js(self) -> dict[str, str | None]:
        return {
            "participant": self.participant,
            "name": self.name,
            "nameAttr": self.name_attr,
            "activeSpeaker": self.active_speaker,
            "activeByStyle": self.active_speaker_by_style,
            "tileRoot": self.tile_root,
            "chatToggle": self.chat_toggle,
            "chatInput": self.chat_input,
            "chatSend": self.chat_send,
            "chatMessage": self.chat_message,
            "chatAuthor": self.chat_author,
            "chatText": self.chat_text,
            "chatOwnMessage": self.chat_own_message,
            "micUnmute": self.mic_unmute,
            "micMute": self.mic_mute,
        }


def host_allowed(host: str | None, suffixes: tuple[str, ...]) -> bool:
    """Host tam olarak izinli bir alan adı ya da onun alt alan adı mı?"""
    host = (host or "").lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def safe_meeting_url(url: str, suffixes: tuple[str, ...]) -> bool:
    """Toplantı linki güvenli mi: https, izinli alan adı, kullanıcı bilgisi/port/IP yok.

    Metin içinde arama yapılmaz: "https://kotu.site/?meet.google.com" gibi linkler reddedilir.
    Takvim ve e-postadan otomatik katılmada link dışarıdan gelir; bot da güvenilmeyen içerik
    çalıştıran bir tarayıcıdır.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return False
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return host_allowed(host, suffixes)


class MeetingPlatform(abc.ABC):
    platform: Platform
    selectors: Selectors = Selectors()
    # Toplantı linkinin ait olabileceği alan adları.
    link_domains: tuple[str, ...] = ()
    # Ana sayfanın gidebileceği alan adları (giriş/yönlendirme dahil); dışına gidilmez.
    navigation_domains: tuple[str, ...] = ()
    share_labels = r"^(share|share content|paylaş|içerik paylaş)(\b|$)"
    screen_labels = r"^(screen|screen, window or tab|ekran|entire screen|tüm ekran)(\b|$)"
    stop_share_labels = r"stop sharing|stop share|stop presenting|paylaşımı durdur|sunumu durdur"

    async def _share_click(self, page: Page, labels: str) -> bool:
        for frame in page.frames:
            from katip.bot.runner import _trusted_frame

            if not _trusted_frame(frame, self):
                continue
            for role in ("button", "menuitem", "radio"):
                loc = frame.get_by_role(role, name=re.compile(labels, re.I))
                for i in range(await loc.count()):
                    el = loc.nth(i)
                    if await el.is_visible() and await el.is_enabled():
                        await el.click(timeout=2000)
                        return True
        return False

    async def start_share(self, page: Page) -> None:
        denied = re.compile(
            r"not allowed to share|can't share|cannot share|only .* can (share|present)"
            r"|sharing.*disabled|paylaşım.*izin|paylaşamazsınız|yalnızca.*sunabilir",
            re.I,
        )
        clicked = await self._share_click(page, self.share_labels)
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            for frame in page.frames:
                from katip.bot.runner import _trusted_frame

                if not _trusted_frame(frame, self):
                    continue
                banners = frame.locator('[role="alert"], [role="status"], [role="dialog"]')
                for banner in await banners.all():
                    if await banner.is_visible() and denied.search(
                        await banner.inner_text(timeout=1000)
                    ):
                        raise ValueError(
                            "Toplantı ayarları paylaşıma izin vermiyor; toplantı sahibi "
                            "'Kim sunabilir' ayarını 'Herkes' yapmalı."
                        )
                if await frame.evaluate("() => Boolean(window.__katipScreen?.active)"):
                    return
            if clicked:
                await self._share_click(page, self.screen_labels)
            else:
                clicked = await self._share_click(page, self.share_labels)
            await asyncio.sleep(0.2)
        raise ValueError(
            "Paylaşım düğmesi veya ekran seçeneği bulunamadı; toplantı paylaşım izinlerini kontrol edin."
        )

    async def stop_share(self, page: Page) -> None:
        try:
            await self._share_click(page, self.stop_share_labels)
        except Exception:
            pass

    @classmethod
    def matches(cls, url: str) -> bool:
        return safe_meeting_url(url, cls.link_domains)

    @abc.abstractmethod
    async def join(self, page: Page, url: str, display_name: str) -> None:
        """Linke git, mikrofon/kamerayı kapat, isim gir ve katılma isteği gönder."""

    @abc.abstractmethod
    async def wait_until_admitted(self, page: Page, timeout_s: int) -> None:
        """Lobiden kabul edilene (veya toplantı arayüzü yüklenene) kadar bekle."""

    @abc.abstractmethod
    async def is_still_in_meeting(self, page: Page) -> bool:
        """Toplantı hâlâ sürüyor mu?"""

    async def after_join(self, page: Page) -> None:  # noqa: B027 — isteğe bağlı kanca
        """Toplantıya girildikten sonra (ör. chat panelini aç). Varsayılan: hiçbir şey."""

    async def leave(self, page: Page) -> None:
        if not page.is_closed():
            await page.close()


def platform_for(url: str) -> type[MeetingPlatform]:
    from katip.bot.platforms.meet import GoogleMeet
    from katip.bot.platforms.teams import MicrosoftTeams
    from katip.bot.platforms.zoom import Zoom

    for cls in (GoogleMeet, MicrosoftTeams, Zoom):
        if cls.matches(url):
            return cls
    raise ValueError(f"Desteklenmeyen toplantı linki: {url}")

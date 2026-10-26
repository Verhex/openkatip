"""Microsoft Teams (web istemcisi) adaptörü — anonim/misafir katılım akışı.

Teams'in katılma ekranları sürüme ve link biçimine göre değişiyor (klasik
`/l/meetup-join/…`, yeni `/meet/<id>?p=…`, teams.live.com) ve bir kısmı iframe içinde
açılıyor. Bu yüzden sabit bir adım sırası yerine her saniye **tüm çerçevelere** bakan
bir döngü kullanıyoruz: "tarayıcıda devam et" görünürse tıkla, ad alanı boşsa doldur,
"ses/görüntü olmadan devam" sorulursa geç, "Join now" etkinse tıkla ve çık.

Not: Kurumun toplantı ayarlarında "anonim kullanıcılar katılabilir" kapalıysa bot
lobide bekler ve bir katılımcının kabul etmesi gerekir.
"""

from __future__ import annotations

import asyncio
import re

from playwright.async_api import Frame, Page

from katip.bot.platforms.base import MeetingPlatform, Selectors
from katip.events import Platform
from katip.logging import get_logger

log = get_logger(__name__)

_CONTINUE_BROWSER = re.compile(
    r"continue on this browser|join on the web|use the web app|bu tarayıcıda devam|web'de katıl",
    re.I,
)
_NO_AV = re.compile(r"continue without audio or video|ses veya görüntü olmadan devam", re.I)
_JOIN_NOW = re.compile(r"^\s*(join now|join meeting|şimdi katıl|toplantıya katıl)\s*$", re.I)
_NAME_INPUT = (
    "input[data-tid='prejoin-display-name-input'], "
    "input[placeholder*='name' i], input[aria-label*='name' i], "
    "input[placeholder*='adınız' i], input[aria-label*='adınız' i]"
)
_IN_CALL = "[data-tid='hangup-button'], [data-tid='call-controls'], #hangup-button"


async def _click_if_visible(frame: Frame, name: re.Pattern[str]) -> bool:
    for role in ("button", "link"):
        loc = frame.get_by_role(role, name=name)
        try:
            count = await loc.count()
        except Exception:
            continue
        for i in range(count):
            el = loc.nth(i)
            try:
                if await el.is_visible() and await el.is_enabled():
                    await el.click(timeout=3000)
                    return True
            except Exception:
                continue
    return False


async def _fill_name(frame: Frame, display_name: str) -> bool:
    loc = frame.locator(_NAME_INPUT)
    try:
        count = await loc.count()
    except Exception:
        return False
    for i in range(count):
        el = loc.nth(i)
        try:
            if await el.is_visible() and not (await el.input_value()).strip():
                await el.fill(display_name, timeout=3000)
                return True
        except Exception:
            continue
    return False


async def _turn_off_devices(frame: Frame) -> None:
    for label in ("Microphone", "Mikrofon", "Camera", "Kamera", "Video"):
        try:
            toggle = frame.get_by_role("switch", name=re.compile(label, re.I)).first
            if await toggle.count() and await toggle.get_attribute("aria-checked") == "true":
                await toggle.click(timeout=2000)
        except Exception:
            continue


class MicrosoftTeams(MeetingPlatform):
    platform = Platform.TEAMS
    selectors = Selectors(
        # Canlı teşhis (30 Eylül): her video kutucuğu <div data-tid="Ad Soyad"
        # data-stream-type="Video">; konuşma çerçevesi aynı kutucuktaki
        # [data-tid='voice-level-stream-outline'] öğesi (hep var, konuşurken görünür olur).
        participant="[data-stream-type][data-tid]",
        name_attr="data-tid",
        active_speaker="[data-tid='voice-level-stream-outline']",
        active_speaker_by_style=True,
        tile_root="[role='menuitem']",
        chat_toggle="[data-tid='chat-button'], button[aria-label*='Chat'], button[aria-label*='Sohbet']",
        # Mesaj kutusu ckeditor öğesinin kendisi (içinde ayrı bir contenteditable yok).
        chat_input="div[data-tid='ckeditor'][contenteditable='true'], div[role='textbox'][contenteditable='true'], textarea[placeholder='Type a message']",
        chat_send="[data-tid='newMessageCommands-send'], button[aria-label*='Send'], button[aria-label*='Gönder']",
        chat_message="[data-tid='chat-pane-message']",
        chat_author="[data-tid='message-author-name']",
        chat_text="[data-tid='message-body'], [id^='content-']",
        # En iyi tahmin; canlı Teams DOM'unda henüz doğrulanmadı.
        chat_own_message=".fui-ChatMyMessage, [class*='ChatMyMessage']",
        mic_unmute="button[aria-label='Unmute mic'], #microphone-button[aria-label*='Unmute'], button[aria-label*='Sesi aç'], button[aria-label*='Unmute']",
        mic_mute="button[aria-label='Mute mic'], #microphone-button[aria-label*='Mute']:not([aria-label*='Unmute']), button[aria-label*='Sesi kapat']",
    )

    link_domains = ("teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft")
    navigation_domains = (
        *link_domains,
        "login.microsoftonline.com",
        "login.live.com",
        "microsoft.com",
    )

    async def join(self, page: Page, url: str, display_name: str, timeout_s: float = 120) -> None:
        await page.goto(url, wait_until="domcontentloaded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        name_filled = False
        continued = False

        while loop.time() < deadline:
            for frame in list(page.frames):
                if await _click_if_visible(frame, _CONTINUE_BROWSER):
                    if not continued:
                        log.info("teams: tarayıcıda devam seçildi", frame=frame.url[:80])
                        continued = True
                    await asyncio.sleep(2)
                    break
                if await _click_if_visible(frame, _NO_AV):
                    log.info("teams: ses/görüntü olmadan devam")
                    continue
                if not name_filled and await _fill_name(frame, display_name):
                    name_filled = True
                    log.info("teams: ad yazıldı", frame=frame.url[:80])
                    await _turn_off_devices(frame)
                if await _click_if_visible(frame, _JOIN_NOW):
                    log.info("teams: katıl düğmesine basıldı", frame=frame.url[:80])
                    return
                try:
                    if await frame.locator(_IN_CALL).count():
                        log.info("teams: doğrudan toplantıya girildi")
                        return
                except Exception:
                    continue
            await asyncio.sleep(1)
        raise RuntimeError("Teams: katılma düğmesi bulunamadı")

    async def after_join(self, page: Page) -> None:
        # Hafif istemcide chat paneli kapalı gelir; kapalıyken mesajlar sayfada yoktur, bot
        # ne okuyabilir ne yazabilir (canlı testte görüldü). Paneli bir kez aç.
        for frame in page.frames:
            if await _click_if_visible(frame, re.compile(r"^\s*(chat|sohbet)\s*$", re.I)):
                log.info("teams: chat paneli açıldı")
                return

    async def _in_call(self, page: Page) -> bool:
        for frame in page.frames:
            try:
                if await frame.locator(_IN_CALL).count():
                    return True
            except Exception:
                continue
        return False

    async def wait_until_admitted(self, page: Page, timeout_s: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if await self._in_call(page):
                return
            await asyncio.sleep(1)
        raise TimeoutError("Teams: lobiden kabul edilmedi")

    async def is_still_in_meeting(self, page: Page) -> bool:
        if page.is_closed():
            return False
        return await self._in_call(page)

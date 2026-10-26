"""Google Meet adaptörü.

Meet'in DOM'u sık değişiyor; bu yüzden metin tabanlı (TR + EN) esnek seçiciler
kullanıyoruz ve her adım "bulamazsan devam et" mantığında.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

from katip.bot.platforms.base import MeetingPlatform, Selectors
from katip.events import Platform

_JOIN_LABELS = ["Katılma isteği gönder", "Hemen katıl", "Ask to join", "Join now"]
_MIC_LABELS = ["Mikrofonu kapat", "Turn off microphone"]
_CAM_LABELS = ["Kamerayı kapat", "Turn off camera"]


async def _click_any(page: Page, labels: list[str], timeout_ms: int = 4000) -> bool:
    for label in labels:
        try:
            el = page.get_by_role("button", name=label, exact=False).first
            await el.wait_for(state="visible", timeout=timeout_ms)
            await el.click()
            return True
        except Exception:
            continue
    return False


class GoogleMeet(MeetingPlatform):
    share_labels = r"present now|şimdi sun|ekranı paylaş"
    screen_labels = r"your entire screen|entire screen|tüm ekran|ekranınızın tamamı"
    platform = Platform.MEET
    selectors = Selectors(
        # Meet her katılımcı kutucuğuna data-participant-id veriyor.
        participant="[data-participant-id]",
        name="[data-self-name], .notranslate",
        # Konuşurken kutucuğa eklenen ses dalgası göstergesi.
        active_speaker="[data-is-speaking='true'], [class*='IisKdb']",
        chat_toggle="button[aria-label*='Chat with everyone'], button[aria-label*='Herkesle sohbet']",
        chat_input="textarea[aria-label*='Send a message'], textarea[aria-label*='mesaj gönder' i]",
        chat_send="button[aria-label*='Send a message'], button[aria-label*='Mesaj gönder']",
        chat_message="[data-message-id]",
        chat_author="[data-sender-name]",
        chat_text="[data-message-text], [jsname='dTKtvb']",
        mic_unmute="button[aria-label*='Turn on microphone'], button[aria-label*='Mikrofonu aç']",
        mic_mute="button[aria-label*='Turn off microphone'], button[aria-label*='Mikrofonu kapat']",
    )

    link_domains = ("meet.google.com",)
    navigation_domains = ("meet.google.com", "accounts.google.com", "workspace.google.com")

    async def join(self, page: Page, url: str, display_name: str) -> None:
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # İsim alanı yalnızca oturum açılmamış (misafir) girişte çıkıyor.
        try:
            name_input = (
                page.get_by_placeholder("Adınız").or_(page.get_by_placeholder("Your name")).first
            )
            await name_input.wait_for(state="visible", timeout=8000)
            await name_input.fill(display_name)
        except Exception:
            pass

        await _click_any(page, _MIC_LABELS)
        await _click_any(page, _CAM_LABELS)

        if not await _click_any(page, _JOIN_LABELS, timeout_ms=15000):
            raise RuntimeError("Meet: katılma düğmesi bulunamadı")

    async def wait_until_admitted(self, page: Page, timeout_s: int) -> None:
        # Toplantı arayüzü yüklendiğinde ayrılma düğmesi / katılımcı kutucukları görünür.
        await page.wait_for_selector(
            "[aria-label*='ayrıl'], [aria-label*='Leave call'], [data-participant-id]",
            timeout=timeout_s * 1000,
        )

    async def is_still_in_meeting(self, page: Page) -> bool:
        if page.is_closed():
            return False
        try:
            return await page.locator("[data-participant-id]").count() > 0
        except Exception:
            return False

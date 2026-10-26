"""Zoom web istemcisi adaptörü.

Uyarı: Zoom web istemcisi sesi her zaman ayrı WebRTC track'i olarak vermiyor.
Bu platformda ses yakalama büyük olasılıkla PulseAudio monitor fallback'ine düşer
(bkz. docs/architecture.md, bölüm 2) — konuşmacı ayrımı tamamen DOM'a kalır.
"""

from __future__ import annotations

import asyncio
import re

from playwright.async_api import Page

from katip.bot.platforms.base import MeetingPlatform, Selectors
from katip.events import Platform


class Zoom(MeetingPlatform):
    share_labels = r"^(share screen|share|ekranı paylaş|ekran paylaş|paylaş)(\b|$)"
    platform = Platform.ZOOM
    selectors = Selectors(
        participant=".participants-item__item-layout, .participants-li",
        name=".participants-item__display-name",
        active_speaker=".participants-item__is-speaking, [class*='speaking']",
        chat_toggle="button[aria-label*='chat' i]",
        chat_input="div.tiptap[contenteditable='true'], textarea.chat-box__chat-textarea",
        chat_send="button.chat-rtf-box__send, button[aria-label*='send' i]",
        chat_message=".new-chat-message__container",
        chat_author=".chat-item__sender",
        chat_text=".new-chat-message__text-content",
        mic_unmute="button[aria-label*='unmute my microphone' i], button[aria-label*='sesi aç' i]",
        mic_mute="button[aria-label*='mute my microphone' i]:not([aria-label*='unmute' i]), button[aria-label*='sesi kapat' i]",
    )

    link_domains = ("zoom.us", "zoom.com")
    navigation_domains = link_domains

    @staticmethod
    def to_web_client_url(url: str) -> str:
        """`.../j/<id>?pwd=...` linkini web istemcisi (`/wc/<id>/join`) formuna çevirir."""
        m = re.search(r"/j/(\d+)", url)
        if not m:
            return url
        base = url.split("/j/")[0]
        query = url.split("?", 1)[1] if "?" in url else ""
        return f"{base}/wc/{m.group(1)}/join" + (f"?{query}" if query else "")

    async def join(self, page: Page, url: str, display_name: str) -> None:
        await page.goto(self.to_web_client_url(url), wait_until="domcontentloaded")
        await asyncio.sleep(4)

        try:
            await page.fill("#input-for-name", display_name, timeout=10000)
        except Exception:
            pass
        try:
            await page.click("button.preview-join-button, #joinBtn", timeout=10000)
        except Exception as exc:
            raise RuntimeError("Zoom: katılma düğmesi bulunamadı") from exc

    async def wait_until_admitted(self, page: Page, timeout_s: int) -> None:
        await page.wait_for_selector(
            "#wc-footer, .footer__leave-btn-container", timeout=timeout_s * 1000
        )

    async def is_still_in_meeting(self, page: Page) -> bool:
        if page.is_closed():
            return False
        try:
            return await page.locator("#wc-footer").count() > 0
        except Exception:
            return False

"""Toplantı botunu çalıştıran süreç.

Her toplantı için ayrı bir süreç (ideali: ayrı konteyner) olarak çalışır: Chromium'u
açar, capture.js'i enjekte eder, platforma katılır ve toplantı bitene kadar bekler.

Taşıma: sayfa sunucuya kendisi bağlanmaz. Teams/Meet'in içerik politikası (CSP) ve
Chrome'un yerel ağ erişimi kısıtlaması, sayfadan 127.0.0.1'e bağlantıyı engelliyor
(canlı Teams testinde görüldü). Bunun yerine:

    sayfa (capture.js) ──Playwright köprüsü──▶ bu süreç ──WebSocket──▶ API
    sayfa ◀──window.__katipDeliver── bu süreç ◀──────────────────────── API

Kullanım:
    python -m katip.bot.runner --url <toplantı-linki> --session-id <id>
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

import websockets
from playwright.async_api import Browser, BrowserContext, Frame, Page, async_playwright

from katip.bot.platforms.base import MeetingPlatform, host_allowed, platform_for
from katip.config import get_settings
from katip.logging import get_logger, setup_logging

log = get_logger(__name__)

_INJECT_DIR = Path(__file__).parent / "inject"

CHROMIUM_ARGS = [
    # Mikrofon/kamera izin diyaloglarını otomatik kabul et.
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-size=1280,720",
]
# Sayfa artık yerel sunucuya bağlanmıyor (Playwright köprüsü var): yerel ağ korumaları ve site
# yalıtımı açık kalır. Korumalı alan (sandbox) Playwright'ın chromium_sandbox parametresiyle.


def chromium_args() -> list[str]:
    args = list(CHROMIUM_ARGS)
    if not get_settings().bot_site_isolation:
        args.append("--disable-features=IsolateOrigins,site-per-process")
    return args


async def launch_browser(pw, *, headless: bool) -> Browser:
    """Chromium'u mümkünse korumalı alanda açar (bkz. KATIP_BOT_SANDBOX)."""
    mode = get_settings().bot_sandbox
    if mode != "off":
        try:
            browser = await pw.chromium.launch(
                headless=headless, args=chromium_args(), chromium_sandbox=True
            )
            log.info("chromium korumalı alanda açıldı")
            return browser
        except Exception as exc:
            if mode == "on":
                raise
            log.warning(
                "chromium korumalı alan kullanılamadı, korumasız açılıyor", error=str(exc)[:300]
            )
    return await pw.chromium.launch(headless=headless, args=chromium_args(), chromium_sandbox=False)


def _trusted_frame(frame: Frame | None, platform: MeetingPlatform) -> bool:
    while frame is not None:
        url = frame.url or ""
        if url.startswith(("about:", "blob:")) or not url:
            frame = frame.parent_frame
            continue
        return host_allowed(urlsplit(url).hostname, platform.navigation_domains)
    return False


def _bot_token() -> str:
    """Supervisor'ın bu toplantı için ürettiği anahtar (yönetici token'ı bota verilmez)."""
    return os.environ.get("KATIP_BOT_TOKEN", "")


# ------------------------------------------------------------------ sunucu bağlantısı
class ServerLink:
    """Bot süreci ↔ API WebSocket'i. Tarayıcı kurallarından bağımsız, kendini yeniden bağlar."""

    def __init__(self, url: str, on_message, on_dead=None) -> None:
        self._url = url
        self._on_message = on_message
        self._ws = None
        self._pending: deque[str] = deque(maxlen=300)
        self._task: asyncio.Task | None = None
        self._closed = False
        self._on_dead = on_dead
        self._last_ack = time.monotonic()
        self._health_task: asyncio.Task | None = None
        self._connected_once = False
        self._snapshot: dict[str, dict] = {}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        self._health_task = asyncio.create_task(self._health())

    async def _health(self) -> None:
        while not self._closed:
            if time.monotonic() - self._last_ack >= get_settings().bot_disconnect_timeout_s:
                await self._dead()
                return
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._ws.send('{"kind":"heartbeat"}'), timeout=2)
            await asyncio.sleep(5)

    async def _dead(self) -> None:
        self._closed = True
        if self._on_dead is not None:
            await self._on_dead()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    async def _run(self) -> None:
        delay = 0.5
        while not self._closed:
            try:
                async with websockets.connect(
                    self._url,
                    max_size=2**22,
                    additional_headers={"X-Katip-Bot-Token": _bot_token()},
                ) as ws:
                    self._ws = ws
                    if self._connected_once:
                        for snapshot in self._snapshot.values():
                            await ws.send(json.dumps(snapshot, ensure_ascii=False))
                    self._connected_once = True
                    delay = 0.5
                    log.info("sunucuya bağlanıldı")
                    while self._pending:
                        await ws.send(self._pending.popleft())
                    async for raw in ws:
                        if isinstance(raw, str):
                            with contextlib.suppress(json.JSONDecodeError):
                                message = json.loads(raw)
                                if message.get("kind") == "heartbeat_ack":
                                    self._last_ack = time.monotonic()
                                else:
                                    await self._on_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from websockets.exceptions import ConnectionClosed, InvalidStatus

                if (isinstance(exc, ConnectionClosed) and exc.rcvd and exc.rcvd.code == 1008) or (
                    isinstance(exc, InvalidStatus) and exc.response.status_code in (401, 403)
                ):
                    await self._dead()
                    return
                log.warning("sunucu bağlantısı koptu", error=type(exc).__name__)
            finally:
                self._ws = None
            if not self._closed:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10)

    async def send_json(self, obj: dict) -> None:
        if obj.get("kind") in ("status", "roster"):
            self._snapshot[obj["kind"]] = obj
        payload = json.dumps(obj, ensure_ascii=False)
        if self._ws is None:
            # Never replay meeting commands after a transport break.
            if not self._connected_once:
                self._pending.append(payload)
            return
        try:
            await self._ws.send(payload)
        except Exception:
            pass

    async def send_audio(self, frame: bytes) -> None:
        # Ses gerçek zamanlı: bağlantı yoksa düşer, birikmez.
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(frame)

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._health_task is not None:
            self._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._health_task


# ------------------------------------------------------------------------ yardımcılar
async def _prepare_context(
    browser: Browser, platform: MeetingPlatform, session_id: str
) -> BrowserContext:
    settings = get_settings()
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        permissions=["microphone", "camera"],
        # Arayüz İngilizce: Teams/Meet düğme etiketleri bu dilde kesin biliniyor
        # (Türkçe karşılıklar yedek olarak seçicilerde duruyor). STT dilini etkilemez.
        locale="en-US",
        # Sayfanın CSP'si enjekte ettiğimiz betiği ve blob/worklet kullanımını engellemesin.
        bypass_csp=True,
    )
    config = {
        "sessionId": session_id,
        "sampleRate": settings.sample_rate,
        "selectors": platform.selectors.to_js(),
    }

    async def guard(route) -> None:
        # Ana çerçeve yalnızca platformun alan adlarında gezinebilir: kötü niyetli bir link
        # ya da yönlendirme, güvenilmeyen bir siteyi bu tarayıcıda açamasın.
        request = route.request
        from katip.bot.network import public_url

        if settings.bot_block_private_network and not await public_url(request.url):
            await route.abort("blockedbyclient")
            return
        if (
            request.is_navigation_request()
            and request.frame.parent_frame is None
            and not host_allowed(urlsplit(request.url).hostname, platform.navigation_domains)
        ):
            log.warning("izinli olmayan adrese gidiş engellendi", url=request.url[:120])
            await route.abort("blockedbyclient")
            return
        await route.fallback()

    if platform.navigation_domains:
        await context.route("**/*", guard)
    if settings.bot_block_private_network:
        from katip.bot.network import public_url

        async def websocket_guard(route) -> None:
            if await public_url(route.url):
                route.connect_to_server()
            else:
                await route.close()

        await context.route_web_socket("**/*", websocket_guard)
    capture_js = (_INJECT_DIR / "capture.js").read_text(encoding="utf-8")
    # Config, capture.js'ten önce set edilmeli; ikisi de her frame'de document_start'ta çalışır.
    await context.add_init_script(f"window.__KATIP_CONFIG__ = {json.dumps(config)};")
    await context.add_init_script(capture_js)
    await context.add_init_script((_INJECT_DIR / "screen.js").read_text(encoding="utf-8"))
    return context


# Paylaşılan ekranı bul: görünür en büyük <video> ekranın dörtte birinden büyükse onu kırp.
_SHARE_RECT_JS = """() => {
  let best = null, area = 0;
  for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect();
    const a = r.width * r.height;
    if (v.videoWidth > 0 && r.width > 0 && r.height > 0 && a > area) {
      area = a;
      best = { x: Math.max(0, r.x), y: Math.max(0, r.y), width: r.width, height: r.height };
    }
  }
  return best && area > 0.25 * innerWidth * innerHeight ? best : null;
}"""


def _api_base() -> str:
    """ws://host:port/ws/ingest → http://host:port"""
    url = get_settings().ingest_ws_url
    url = url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    return url.split("/ws/", 1)[0]


async def _take_screenshot(page: Page, session_id: str, request_id: str) -> None:
    import httpx

    try:
        clip = await page.evaluate(_SHARE_RECT_JS)
        image = (
            await page.screenshot(type="jpeg", quality=85, clip=clip)
            if clip
            else (await page.screenshot(type="jpeg", quality=85))
        )
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{_api_base()}/api/internal/{session_id}/screenshots/{request_id}",
                content=image,
                headers={"content-type": "image/jpeg", "X-Katip-Bot-Token": _bot_token()},
            )
        log.info("ekran görüntüsü gönderildi", status=response.status_code, shared=bool(clip))
    except Exception as exc:
        log.warning("ekran görüntüsü alınamadı", error=str(exc))


# Teşhis: her çerçevedeki görünür düğme ve giriş alanlarını listeler.
_DIAG_JS = """() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const label = (el) => (el.getAttribute('aria-label') || el.innerText || el.value || '')
    .trim().replace(/\\s+/g, ' ').slice(0, 80);
  return {
    title: document.title,
    buttons: [...document.querySelectorAll('button, [role=button]')].filter(vis).map(label)
      .filter(Boolean).slice(0, 60),
    inputs: [...document.querySelectorAll('input, textarea, [contenteditable=true]')].filter(vis)
      .map((el) => el.getAttribute('placeholder') || el.getAttribute('aria-label')
        || el.getAttribute('data-tid') || el.name || el.tagName).slice(0, 30),
  };
}"""


async def diagnose(page: Page, session_id: str, reason: str) -> None:
    """Katılma başarısız olunca sayfanın durumunu kaydeder: loglara ve data/debug/'a.

    API aynı konteynerde olduğu için yönetici `GET /api/meetings/{id}/debug` ile bunları
    doğrudan görebilir; platform arayüzü değiştiğinde seçici tek seferde düzeltilir.
    """
    frames = []
    for frame in page.frames:
        info = {"url": frame.url[:200]}
        with contextlib.suppress(Exception):
            info.update(await frame.evaluate(_DIAG_JS))
        frames.append(info)
    log.warning("sayfa teşhisi", reason=reason, frames=frames)

    out = Path(get_settings().data_dir) / "debug"
    with contextlib.suppress(Exception):
        out.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(out / f"{session_id}.png"), full_page=True)
        parts = []
        for frame in page.frames:
            with contextlib.suppress(Exception):
                html = await frame.content()
                parts.append(f"<!-- ÇERÇEVE: {frame.url} -->\n{html[:3_000_000]}")
        (out / f"{session_id}.html").write_text("\n\n".join(parts), encoding="utf-8")
        (out / f"{session_id}.json").write_text(
            json.dumps(
                {"reason": reason, "at": time.time(), "url": page.url, "frames": frames},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


async def _find_visible(page: Page, selector: str | None):
    if not selector:
        return None
    for frame in page.frames:
        try:
            loc = frame.locator(selector)
            for i in range(await loc.count()):
                el = loc.nth(i)
                if await el.is_visible():
                    return el
        except Exception:
            continue
    return None


_URL = re.compile(r"(https?://\S+)")


async def type_chat(page: Page, platform, text: str) -> bool:
    """Toplantı chat'ine Playwright'ın gerçek klavyesiyle yazar ve gönderir."""
    sel = platform.selectors
    box = await _find_visible(page, sel.chat_input)
    if box is None and sel.chat_toggle:
        toggle = await _find_visible(page, sel.chat_toggle)
        if toggle is not None:
            with contextlib.suppress(Exception):
                await toggle.click(timeout=3000)
                await asyncio.sleep(1)
        box = await _find_visible(page, sel.chat_input)
    if box is None:
        return False
    try:
        await box.click(timeout=3000)
        lines = text.split("\n")
        for i, line in enumerate(lines):
            for part in _URL.split(line):
                if not part:
                    continue
                await page.keyboard.insert_text(part)
                if _URL.fullmatch(part):
                    # Teams'in editörü linki ancak ardından gerçek bir boşluk tuşuna basılınca
                    # bağlantıya çeviriyor (insert_text ile gelen link tıklanamıyordu, 7. test).
                    await page.keyboard.press("Space")
            if i < len(lines) - 1:
                await page.keyboard.press("Shift+Enter")
        await page.keyboard.press("Enter")
        return True
    except Exception as exc:
        log.warning("chat yazımı başarısız", error=str(exc))
        return False


# --------------------------------------------------------------------------- ana akış
async def run_bot(url: str, session_id: str, max_minutes: int | None = None) -> int:
    settings = get_settings()
    platform_cls = platform_for(url)
    platform = platform_cls()
    log.info("bot başlıyor", platform=platform.platform, session_id=session_id)

    async with async_playwright() as pw:
        browser = await launch_browser(pw, headless=settings.bot_headless)
        context = await _prepare_context(browser, platform, session_id)
        page: Page | None = None
        call_frame: Frame | None = None  # sesi gönderen çerçeve = toplantı çağrısının çerçevesi
        background: set[asyncio.Task] = set()
        presentation = None

        def spawn(coro) -> None:
            task = asyncio.create_task(coro)
            background.add(task)
            task.add_done_callback(background.discard)

        async def from_server(msg: dict) -> None:
            if page is None:
                return
            if msg.get("kind") == "presentation_data" and presentation:
                try:
                    presentation.receive(msg)
                except ValueError as exc:
                    await link.send_json(
                        {
                            "kind": "presentation_status",
                            "request_id": msg.get("request_id"),
                            "page": 0,
                            "pages": 0,
                            "error": str(exc),
                        }
                    )
                return
            if msg.get("kind") == "presentation" and presentation:
                spawn(presentation.handle(msg))
                return
            if msg.get("kind") == "screenshot":
                spawn(_take_screenshot(page, session_id, str(msg.get("request_id"))))
                return
            if msg.get("kind") == "diagnose":
                spawn(diagnose(page, session_id, "yönetici isteği (toplantı sırasında)"))
                return
            if msg.get("kind") == "chat_send" and msg.get("text"):
                spawn(send_chat(msg))
                return
            targets = [call_frame] if call_frame is not None else page.frames
            for frame in targets:
                with contextlib.suppress(Exception):
                    await frame.evaluate(
                        "m => window.__katipDeliver && window.__katipDeliver(m)", msg
                    )

        chat_lock = asyncio.Lock()

        async def deliver(msg: dict) -> None:
            targets = [call_frame] if call_frame is not None else page.frames
            for frame in targets:
                with contextlib.suppress(Exception):
                    await frame.evaluate(
                        "m => window.__katipDeliver && window.__katipDeliver(m)", msg
                    )

        async def send_chat(msg: dict) -> None:
            # Gerçek klavye olaylarıyla yaz: Teams'in ckeditor'ı sayfa içinden taklit edilen
            # olaylara güvenmiyor. Olmazsa sayfadaki yedek yönteme düş.
            text = str(msg["text"])
            async with chat_lock:
                await deliver({"kind": "chat_mark", "text": text})
                for attempt in range(4):
                    if await type_chat(page, platform, text):
                        return
                    # Chat paneli kapalı ya da henüz yüklenmedi: aç ve biraz bekle.
                    with contextlib.suppress(Exception):
                        await platform.after_join(page)
                    await asyncio.sleep(2 + 2 * attempt)
                log.warning("chat'e klavyeyle yazılamadı, sayfa yöntemi deneniyor")
                await deliver(msg)

        async def server_lost() -> None:
            log.warning("API erişimi kayboldu, tarayıcı kapanıyor", session_id=session_id)
            await browser.close()

        link = ServerLink(f"{settings.ingest_ws_url}/{session_id}", from_server, server_lost)
        await link.start()

        async def relay(source: dict, msg: dict) -> None:
            nonlocal call_frame
            # expose_binding her çerçeveye eklenir; yalnızca toplantı platformunun çerçeveleri
            # (ve onların boş/srcdoc alt çerçeveleri) ses ve olay gönderebilir.
            if not _trusted_frame(source.get("frame"), platform):
                return
            if msg.get("t") == "audio":
                call_frame = source.get("frame") or call_frame
                await link.send_audio(base64.b64decode(msg.get("d") or ""))
            elif msg.get("t") == "json" and isinstance(msg.get("d"), dict):
                if msg["d"].get("kind") == "presentation_status":
                    return
                await link.send_json(msg["d"])

        await context.expose_binding("__katipRelay", relay)
        page = await context.new_page()
        from katip.bot.presentation import Presentation

        presentation = Presentation(
            page,
            platform,
            link.send_json,
            lambda: diagnose(page, session_id, "ekran paylaşımı başarısız"),
        )
        page.on("console", lambda m: log.debug("sayfa", text=m.text))

        try:
            await link.send_json({"kind": "status", "state": "joining"})
            try:
                await platform.join(page, url, settings.bot_display_name)
            except Exception as exc:
                await diagnose(page, session_id, f"katılma: {exc}")
                raise

            await link.send_json({"kind": "status", "state": "waiting_admission"})
            try:
                await platform.wait_until_admitted(page, settings.bot_join_timeout_s)
            except Exception as exc:
                await diagnose(page, session_id, f"kabul bekleme: {exc}")
                raise

            await link.send_json({"kind": "status", "state": "active"})
            log.info("toplantıya girildi", session_id=session_id)
            with contextlib.suppress(Exception):
                await platform.after_join(page)

            # Toplantı bitene kadar bekle. Bitişi anlamanın tek yolu DOM yoklamak;
            # bildirim verecek bir olay kaynağı yok, o yüzden bilinçli polling.
            limit = max_minutes or settings.max_meeting_minutes
            deadline = asyncio.get_running_loop().time() + limit * 60
            while await platform.is_still_in_meeting(page):  # noqa: ASYNC110
                if asyncio.get_running_loop().time() > deadline:
                    log.warning("süre sınırı doldu, toplantıdan çıkılıyor", session_id=session_id)
                    await link.send_json(
                        {"kind": "status", "state": "ended", "detail": "max_meeting_minutes"}
                    )
                    return 0
                await asyncio.sleep(5)

            await link.send_json({"kind": "status", "state": "ended"})
            log.info("toplantı bitti", session_id=session_id)
            return 0
        except Exception as exc:
            log.exception("bot hatası", error=str(exc))
            await link.send_json({"kind": "status", "state": "failed", "detail": str(exc)})
            return 1
        finally:
            await asyncio.sleep(0.5)  # son durum mesajı gitsin
            await link.close()
            for task in list(background):
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            with contextlib.suppress(Exception):
                await presentation.close()
            with contextlib.suppress(Exception):
                await context.close()
            with contextlib.suppress(Exception):
                await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Katip toplantı botu")
    parser.add_argument("--url", required=True, help="Toplantı linki")
    parser.add_argument("--session-id", required=True, help="Oturum kimliği")
    parser.add_argument("--max-minutes", type=int, default=None, help="En uzun toplantı süresi")
    args = parser.parse_args()

    if os.environ.get("KATIP_BOT_TOKEN"):
        # Supervisor'dan başlatılan bot .env dosyasını okumaz: gizli anahtarlar ona hiç ulaşmaz.
        from katip.config import Settings

        Settings.model_config["env_file"] = None
        get_settings.cache_clear()
    setup_logging()
    sys.exit(asyncio.run(run_bot(args.url, args.session_id, args.max_minutes)))


if __name__ == "__main__":
    main()

"""Bot, gerçek Chromium ile: Playwright köprüsü, sunucu bağlantısı, Teams katılma akışı, teşhis.

Teams'in yeni `/meet/…` akışını taklit eden sahte sayfalar kullanılır: önce "Continue on
this browser", sonra iframe içinde ad alanı + "Join now", ardından lobi ve kabul.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import websockets

from katip.bot import runner
from katip.bot.platforms.teams import MicrosoftTeams
from katip.config import get_settings

MEET_URL = "https://teams.microsoft.com/meet/123456789?p=abc"


async def test_chat_block_echo_and_rerendered_message_identity():
    from playwright.async_api import async_playwright

    chats = []

    async def relay(source, message):
        if message.get("t") == "json" and message["d"].get("kind") == "chat":
            chats.append(message["d"])

    async with async_playwright() as pw:
        browser = await _browser_or_skip(pw)
        page = await browser.new_page()
        await page.expose_binding("__katipRelay", relay)
        await page.evaluate(
            "config => window.__KATIP_CONFIG__ = config",
            {"selectors": MicrosoftTeams.selectors.to_js()},
        )
        await page.evaluate(
            (Path(runner.__file__).parent / "inject" / "capture.js").read_text(encoding="utf-8")
        )
        await page.evaluate(
            "window.__katipDeliver({kind: 'chat_mark', text: 'Katip, yardım\\nKayıt dışı örneği.'})"
        )
        for _ in range(3):
            await page.evaluate("""() => {
                document.body.innerHTML = `<div data-tid="chat-pane-message">
                  <div id="content-own"><p>Katip, yardım</p><p>Kayıt dışı örneği.</p></div>
                </div>`;
            }""")
            await page.wait_for_timeout(30)
        assert chats == []
        for _ in range(3):
            await page.evaluate("""() => {
                document.body.innerHTML = `<div data-tid="chat-pane-message">
                  <span data-tid="message-author-name">Ayşe</span>
                  <div id="content-human"><p>Katip, sesli cevap ver.</p></div>
                </div>`;
            }""")
            await page.wait_for_timeout(30)
        assert chats == [{"kind": "chat", "author": "Ayşe", "text": "Katip, sesli cevap ver."}]
        await browser.close()


async def test_roster_heartbeat_continues_without_dom_changes():
    from playwright.async_api import async_playwright

    rosters = []

    async def relay(source, msg):
        if msg.get("t") == "json" and msg.get("d", {}).get("kind") == "roster":
            rosters.append(msg["d"])

    async with async_playwright() as pw:
        browser = await _browser_or_skip(pw)
        context = await browser.new_context()
        await context.expose_binding("__katipRelay", relay)
        await context.add_init_script(
            "window.__KATIP_CONFIG__ = "
            + json.dumps(
                {
                    "sessionId": "heartbeat",
                    "selectors": {
                        "participant": "[data-name]",
                        "nameAttr": "data-name",
                        "activeSpeaker": ".speaking",
                    },
                }
            )
        )
        await context.add_init_script(
            (Path(runner.__file__).parent / "inject" / "capture.js").read_text(encoding="utf-8")
        )
        page = await context.new_page()
        await page.goto("about:blank")
        await page.set_content('<div data-name="Deniz"><span class="speaking"></span></div>')
        deadline = asyncio.get_running_loop().time() + 4
        while len([r for r in rosters if r["active_speakers"] == ["Deniz"]]) < 2:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.1)
        await browser.close()
    matching = [r for r in rosters if r["active_speakers"] == ["Deniz"]]
    assert matching[0] == matching[1]


LAUNCHER = """<!doctype html><html><body>
<h1>How do you want to join your Teams meeting?</h1>
<button onclick="location.href='/prejoin'">Continue on this browser</button>
</body></html>"""

# Ön katılım ekranı bir iframe içinde (Teams'in hafif istemcisi gibi).
PREJOIN = """<!doctype html><html><body>
<iframe id="app" style="width:900px;height:600px" srcdoc="
  <input placeholder='Type your name'>
  <button role='switch' aria-label='Microphone' aria-checked='true'
          onclick='this.setAttribute(&quot;aria-checked&quot;,&quot;false&quot;)'>Mic</button>
  <button id='join' onclick='
      document.body.innerHTML = &quot;<p>Someone in the meeting should let you in soon</p>&quot;;
      setTimeout(() => { document.body.innerHTML +=
        &quot;<button data-tid=hangup-button>Leave</button>&quot;; }, 1500);
  '>Join now</button>
"></iframe>
</body></html>"""


async def _browser_or_skip(pw):
    try:
        return await pw.chromium.launch(headless=True)
    except Exception as exc:
        pytest.skip(f"Chromium yok: {exc}")


async def _fake_teams(page) -> dict:
    """Teams adreslerini sahte sayfalarla karşıla; iframe içindeki ad alanını kaydet."""

    async def handle(route):
        path = route.request.url.split("teams.microsoft.com", 1)[-1]
        body = PREJOIN if path.startswith("/prejoin") else LAUNCHER
        await route.fulfill(status=200, content_type="text/html", body=body)

    await page.route("https://teams.microsoft.com/**", handle)
    return {}


async def test_teams_join_flow_with_iframe_and_lobby() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await _browser_or_skip(pw)
        page = await browser.new_page()
        await _fake_teams(page)
        teams = MicrosoftTeams()

        await teams.join(page, MEET_URL, "Katip", timeout_s=20)
        frame = page.frame_locator("#app")
        # Ad yazıldı mı? (Katıl'a basınca iframe içeriği lobiye dönüştüğü için kontrol lobide)
        await frame.get_by_text("let you in soon").wait_for(timeout=5000)

        await teams.wait_until_admitted(page, timeout_s=10)
        assert await teams.is_still_in_meeting(page)
        await browser.close()


async def test_teams_join_fails_with_clear_error_when_no_join_button() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await _browser_or_skip(pw)
        page = await browser.new_page()

        async def blank(route):
            await route.fulfill(status=200, content_type="text/html", body="<p>Unsupported</p>")

        await page.route("https://teams.microsoft.com/**", blank)
        with pytest.raises(RuntimeError, match="katılma düğmesi bulunamadı"):
            await MicrosoftTeams().join(page, MEET_URL, "Katip", timeout_s=3)
        await browser.close()


async def test_relay_bridge_and_server_link_round_trip(monkeypatch) -> None:
    """capture.js → Playwright köprüsü → ServerLink → sahte API ve geri."""
    from playwright.async_api import async_playwright

    received: list = []
    deliver_seen = asyncio.Event()

    async def api(ws) -> None:
        async for msg in ws:
            received.append(msg)
            if isinstance(msg, str) and json.loads(msg).get("kind") == "hello":
                # Sunucudan sayfaya: chat_send → capture.js __katipDeliver
                await ws.send(json.dumps({"kind": "chat_send", "text": "Merhaba toplantı"}))

    async with websockets.serve(api, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        async def on_server_msg(msg):
            deliver_seen.set()
            await page.evaluate("m => window.__katipDeliver(m)", msg)

        link = runner.ServerLink(f"ws://127.0.0.1:{port}/ws/ingest/s1", on_server_msg)
        await link.start()

        async with async_playwright() as pw:
            browser = await _browser_or_skip(pw)
            context = await browser.new_context()
            await context.add_init_script(
                "window.__KATIP_CONFIG__ = {sessionId: 's1', selectors: {}};"
            )
            await context.add_init_script(
                (Path(runner.__file__).parent / "inject" / "capture.js").read_text(encoding="utf-8")
            )

            async def relay(source, msg):
                if msg["t"] == "json":
                    await link.send_json(msg["d"])

            await context.expose_binding("__katipRelay", relay)
            page = await context.new_page()
            await page.goto("about:blank")
            # Sayfa sunucuya kendisi bağlanmıyor; mesaj köprüden gider.
            await page.evaluate("window.__katipSendJson({kind: 'hello', text: 'çağrı'})")
            await asyncio.wait_for(deliver_seen.wait(), 5)
            await browser.close()
        await link.close()

    hello = [json.loads(m) for m in received if isinstance(m, str)]
    assert {"kind": "hello", "text": "çağrı"} in hello  # Türkçe karakterler bozulmadan


async def test_diagnose_saves_buttons_and_screenshot(monkeypatch) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await _browser_or_skip(pw)
        page = await browser.new_page()
        await page.set_content(
            "<button aria-label='Join with a code'>x</button><input placeholder='Meeting ID'>"
            '<iframe srcdoc="<button>Sign in</button>"></iframe>'
        )
        await runner.diagnose(page, "diag1", "test")
        await browser.close()

    out = Path(get_settings().data_dir) / "debug"
    data = json.loads((out / "diag1.json").read_text(encoding="utf-8"))
    buttons = [b for f in data["frames"] for b in f.get("buttons", [])]
    inputs = [i for f in data["frames"] for i in f.get("inputs", [])]
    assert "Join with a code" in buttons and "Sign in" in buttons  # iframe dahil
    assert "Meeting ID" in inputs
    assert (out / "diag1.png").stat().st_size > 1000


def test_debug_endpoints_require_admin_and_serve_files(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from katip.api.main import app

    out = Path(get_settings().data_dir) / "debug"
    out.mkdir(parents=True, exist_ok=True)
    (out / "abc123.json").write_text('{"reason": "test"}', encoding="utf-8")
    client = TestClient(app)
    with client:
        assert client.get("/api/meetings/abc123/debug").status_code == 401
        ok = client.get(
            "/api/meetings/abc123/debug", headers={"Authorization": "Bearer test-admin"}
        )
        assert ok.status_code == 200 and ok.json()["reason"] == "test"
        bad = client.get(
            "/api/meetings/..%2Fsecret/debug", headers={"Authorization": "Bearer test-admin"}
        )
        assert bad.status_code in (400, 404)


TEAMS_STAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>
  /* Gerçek Teams gibi: çerçeve ::before ile çizilir, konuşurken görünür olur. */
  #o1::before { content: ""; display: block; border: 4px solid rgba(0, 0, 0, 0); opacity: 0; }
  #o1.sp::before { border-color: rgb(91, 95, 199); opacity: 1; }
</style></head><body>
<div role="menuitem"><div data-tid="Deniz Kaya" data-stream-type="Video">A</div>
  <div data-tid="voice-level-stream-outline" id="o1"></div></div>
<div role="menuitem"><div data-tid="Selin Arslan" data-stream-type="Video">Ş</div>
  <div data-tid="voice-level-stream-outline" id="o2"></div></div>
<div role="menuitem"><div data-tid="Katip" data-stream-type="Video">K</div></div>
<div data-tid="ckeditor" role="textbox" contenteditable="true" aria-label="Type a message"
     style="width:300px;height:40px;border:1px solid #ccc"
     onkeydown="if(event.key==='Enter'&&!event.shiftKey){window.sent.push(this.innerText);
                this.innerText='';event.preventDefault();}"></div>
<script>window.sent = [];</script>
</body></html>"""


async def test_teams_roster_speaking_outline_and_keyboard_chat() -> None:
    """Teams kutucuklarından ad, konuşma çerçevesinden aktif konuşan; chat'e gerçek klavye."""
    from playwright.async_api import async_playwright

    got: list[dict] = []
    async with async_playwright() as pw:
        browser = await _browser_or_skip(pw)
        context = await browser.new_context()
        selectors = json.dumps(MicrosoftTeams.selectors.to_js())
        await context.add_init_script(
            f"window.__KATIP_CONFIG__ = {{sessionId: 's1', selectors: {selectors}}};"
        )
        await context.add_init_script(
            (Path(runner.__file__).parent / "inject" / "capture.js").read_text(encoding="utf-8")
        )

        async def relay(source, msg):
            if msg["t"] == "json":
                got.append(msg["d"])

        await context.expose_binding("__katipRelay", relay)
        page = await context.new_page()
        await page.route(
            "https://teams.microsoft.com/**",
            lambda route: route.fulfill(status=200, content_type="text/html", body=TEAMS_STAGE),
        )
        await page.goto("https://teams.microsoft.com/stage")
        await asyncio.sleep(0.6)
        # Selin konuşmaya başlıyor: çerçeve görünür bir kenarlık alıyor.
        await page.evaluate(
            "document.getElementById('o2').style.border = '2px solid rgb(98, 100, 167)'"
        )
        # Teams konuşanı öznitelikle de gösterebilir: teşhis bunu adlar maskeli bildirmeli.
        await page.evaluate(
            "document.querySelector('[data-tid=\"Selin Arslan\"]')"
            ".setAttribute('aria-label', 'Selin Arslan, speaking')"
        )
        await asyncio.sleep(0.6)

        await page.evaluate("document.getElementById('o2').style.border = ''")
        await page.evaluate("document.getElementById('o1').classList.add('sp')")
        await asyncio.sleep(0.6)
        ok = await runner.type_chat(page, MicrosoftTeams(), "Not alındı.\nİkinci satır")
        sent = await page.evaluate("window.sent")
        await browser.close()

    rosters = [m for m in got if m.get("kind") == "roster"]
    assert rosters[0]["participants"] == ["Deniz Kaya", "Selin Arslan", "Katip"]
    assert rosters[0]["active_speakers"] == []
    assert any(r["active_speakers"] == ["Selin Arslan"] for r in rosters)
    assert any(m.get("kind") == "speaker_style" and m["active"] for m in got)
    assert ok and sent == ["Not alındı.\nİkinci satır"]
    # ::before ile çizilen çerçeve (Teams'in gerçek yöntemi) konuşan olarak algılanır.
    assert any(r["active_speakers"] == ["Deniz Kaya"] for r in rosters)
    doms = [m for m in got if m.get("kind") == "speaker_dom"]
    assert any(m["el"] == "TILE" and m["detail"] == "<AD>, speaking" for m in doms)
    assert not any("Selin" in str(m) for m in doms)

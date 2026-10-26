from __future__ import annotations

import asyncio
import contextlib
import uuid

from katip import usage
from katip.agent import llm
from katip.config import get_settings
from katip.events import PanelEvent, SessionState
from katip.voice.expressive import display_text, tts_text
from katip.voice.tr_normalize import normalize_tr

READ_LIMIT = 1200
SLIDE_PAUSE = 1.5


def slide_parts(text: str) -> tuple[str, str]:
    lines = text.strip().splitlines()
    return (lines[0].strip(), "\n".join(lines[1:]).strip()) if lines else ("", "")


def read_slide(text: str) -> str:
    text = text.strip()
    if len(text) > READ_LIMIT:
        text = text[:READ_LIMIT].rsplit(" ", 1)[0].rstrip() + ". Devamı ekranda."
    return text


class PresentationPlayback:
    def __init__(self, host):
        self.host = host
        self.task: asyncio.Task | None = None
        self.cache: dict[tuple[str, int], asyncio.Task] = {}
        self.calls = 0
        self.completed: tuple[str, int] | None = None
        self.remainder: tuple[tuple[str, int], str] | None = None
        self.narrating = False
        self.lock = asyncio.Lock()

    @property
    def state(self):
        return self.host.presentation_state

    def position(self):
        return (self.host._presentation_document_id or "", self.state["page"])

    def pause(self, note: str | None = None):
        if self.state.get("mode") != "auto":
            return
        if self.state["state"] != "running":
            if note and self.state["state"] == "paused":
                self.state["error"] = note
                self.host.say(note, channel="chat")
            return
        self.state["state"] = "paused"
        speaking = getattr(self.host, "_speak_task", None) is self.task
        if self.task and self.task is not asyncio.current_task() and not speaking:
            self.task.cancel()
        self.host._barge_in()
        if note:
            self.state["error"] = note
            self.host.say(note, channel="chat")

    async def settle(self):
        if self.task and self.task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    def narration(self, page: int, previous_title: str = "") -> asyncio.Task:
        key = (self.host._presentation_document_id or "", page)
        cached = self.cache.get(key)
        if cached and not cached.cancelled() and (not cached.done() or cached.exception() is None):
            return cached

        async def generate():
            text = await self.host.presentation_text(page)
            if not text.strip():
                raise ValueError("Bu slaytta okunabilir metin yok; sunum duraklatıldı.")
            title, body = slide_parts(text)
            hint = previous_title
            if not hint and page > 1:
                hint, _ = slide_parts(await self.host.presentation_text(page - 1))
            if error := self.unavailable():
                raise ValueError(error)
            if self.calls >= get_settings().presentation_narrations_per_meeting:
                raise ValueError("Bu toplantı için sunum anlatım limitine ulaşıldı.")
            self.calls += 1
            with usage.meeting(self.host.session_id):
                result = await llm.narrate_slide(body, title=title, previous_title=hint)
            if not result.strip():
                raise ValueError("Slayt anlatımı hazırlanamadı; sunum duraklatıldı.")
            return result

        task = asyncio.create_task(generate())
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        self.cache[key] = task
        return task

    def unavailable(self):
        if self.host.paused:
            return "Kayıt dışıyken sunum anlatılamaz; sunum duraklatıldı."
        if self.host.mode == "kayit":
            return "Sesli cevap kapalı; sunum duraklatıldı."
        return None

    async def control(self, action: str, page: int | None = None):
        # Durdurma, başlatma ve PDF aktarımı kilidini beklemeden sesi keser.
        self.pause()
        async with self.lock:
            await self.settle()
            if action in {"pause", "resume"} and not self.state["page"]:
                return {**self.state, "error": "Önce sunumu başlatın."}
            if action == "pause":
                return dict(self.state)
            if action in {"auto", "resume"}:
                if action == "auto":
                    result = await self.host._presentation_action("start")
                    self.state["error"] = result.get("error")
                    if result.get("error"):
                        return result
                    self.completed = None
                    self.remainder = None
                self.state.update(mode="auto", state="running", error=None)
                if error := self.unavailable():
                    self.pause(error)
                else:
                    self.task = asyncio.create_task(self.run(start=action == "auto"))
                return dict(self.state)
            result = await self.host._presentation_action(action, page)
            self.state["error"] = result.get("error")
            if not result.get("error"):
                self.remainder = None
                if action in {"start", "stop"}:
                    self.state.update(mode="manual", state="off" if action == "stop" else "running")
                    self.completed = None
                elif self.state["mode"] == "auto":
                    self.state["state"] = "paused"
                    self.completed = None
            return {**self.state, "error": result.get("error")}

    async def speak(self, text):
        if error := self.unavailable():
            raise ValueError(error)
        if not await self.host.presentation_speak(text):
            if getattr(self.host, "_speech_interrupted", False):
                raise ValueError("Sunum duraklatıldı; devam etmek için 'Katip, devam et' deyin.")
            raise ValueError("Sesli anlatım tamamlanamadı; sunum duraklatıldı.")

    async def run(self, *, start=False):
        try:
            if start:
                await self.speak(
                    normalize_tr(f"Sunumu başlatıyorum; {self.state['pages']} slayt var.")
                )
            elif self.completed != self.position() and not (
                self.remainder and self.remainder[0] == self.position()
            ):
                await self.speak("Kaldığımız yerden devam ediyorum.")
            while self.state["state"] == "running":
                if error := self.unavailable():
                    raise ValueError(error)
                if self.completed != self.position():
                    number = self.state["page"]
                    text = await asyncio.shield(self.narration(number))
                    title, _ = slide_parts(await self.host.presentation_text(number))
                    if number < self.state["pages"]:
                        self.narration(number + 1, title)
                    if self.remainder and self.remainder[0] == self.position():
                        _, text = self.remainder
                        self.remainder = None
                    if text:
                        self.narrating = True
                        try:
                            await self.speak(text)
                        finally:
                            self.narrating = False
                    self.completed = self.position()
                if self.state["page"] == self.state["pages"]:
                    await self.speak("Sunumum bu kadar; sorularınızı alabilirim.")
                    self.state["state"] = "finished"
                    return
                await asyncio.sleep(SLIDE_PAUSE)
                result = await self.host._presentation_action("next")
                if result.get("error"):
                    raise ValueError(result["error"])
        except asyncio.CancelledError:
            self.pause(
                "Sunum duraklatıldı; devam etmek için 'Katip, devam et' deyin."
                if self.state["state"] == "running"
                else None
            )
        except Exception as exc:
            if self.state["state"] == "paused":
                return  # Açık duraklatma zaten durumu ve varsa notunu belirledi.
            self.pause(
                str(exc)
                if isinstance(exc, ValueError)
                else "Sunum anlatımı hazırlanamadı; sunum duraklatıldı."
            )

    async def close(self):
        self.pause()
        await self.settle()
        tasks = list(self.cache.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.cache.clear()


class PresentationHost:
    async def presentation_action(self, action: str, page: int | None = None) -> dict:
        return await self.presentation.control(action, page)

    async def presentation_speak(self, text: str) -> bool:
        if self.paused or self.mode == "kayit":
            return False
        audible = tts_text(text)
        text = display_text(text)
        self._publish(PanelEvent(kind="note", text=text))
        self._record_reply(text)
        return await self._speak(audible, "tr", strict=True)

    async def _presentation_action(self, action: str, page: int | None = None) -> dict:
        from katip.bot.presentation import transfer_messages
        from katip.documents import service
        from katip.store import crypto

        async with self._presentation_lock:
            if self.state is not SessionState.ACTIVE:
                return {
                    **self.presentation_state,
                    "error": "Sunum için Katip aktif toplantıda olmalı.",
                }
            request_id = uuid.uuid4().hex
            message = {
                "kind": "presentation",
                "request_id": request_id,
                "action": action,
                "page": page,
            }
            messages = []
            if action == "start":
                doc = await service.presentation_document(self.session_id)
                if doc is None:
                    return {
                        **self.presentation_state,
                        "error": "Panelde toplantı belgelerine PDF ekleyip "
                        "'Sunum olarak kullan' seçin.",
                    }
                try:
                    data = await crypto.read_file(
                        self.session_id, service.presentation_path(self.session_id, doc.id)
                    )
                except (OSError, ValueError):
                    return {
                        **self.presentation_state,
                        "error": "Sunum PDF'si okunamadı; yeniden yükleyin.",
                    }
                messages = transfer_messages(data, request_id)
                message["size"] = len(data)
            if self.outbox.qsize() + len(messages) + 1 > self.outbox.maxsize:
                return {**self.presentation_state, "error": "Bot kanalı meşgul; tekrar deneyin."}
            future = asyncio.get_running_loop().create_future()
            self._presentation_waiters[request_id] = future
            if action == "start":
                self._presentation_pending_document = (request_id, doc.id)
            try:
                for chunk in messages:
                    self.outbox.put_nowait(chunk)
                self.outbox.put_nowait(message)
                return await asyncio.wait_for(future, 25)
            except TimeoutError:
                self.outbox.put_nowait(
                    {"kind": "presentation", "action": "stop", "request_id": uuid.uuid4().hex}
                )
                return {
                    **self.presentation_state,
                    "error": "Sunum yanıtı gecikti; paylaşım durduruluyor.",
                }
            finally:
                self._presentation_waiters.pop(request_id, None)
                if (
                    self._presentation_pending_document
                    and self._presentation_pending_document[0] == request_id
                ):
                    self._presentation_pending_document = None

    def on_presentation_status(self, msg: dict) -> None:
        self.presentation_state.update({k: msg.get(k) for k in ("page", "pages", "error")})
        if not msg.get("page"):
            self.presentation.pause()
            self.presentation_state.update(mode="manual", state="off")
        if not msg.get("page"):
            self._presentation_document_id = None
        elif (
            not msg.get("error")
            and self._presentation_pending_document
            and self._presentation_pending_document[0] == msg.get("request_id")
        ):
            self._presentation_document_id = self._presentation_pending_document[1]
        if future := self._presentation_waiters.get(msg.get("request_id")):
            if not future.done():
                future.set_result(dict(self.presentation_state))

    async def presentation_text(self, page: int | None = None) -> str:
        from katip.documents.service import list_documents

        number = page if page is not None else self.presentation_state["page"]
        if not number:
            return ""
        docs = await list_documents(self.session_id)
        doc = next((d for d in docs if d.id == self._presentation_document_id), None)
        return (
            next((p["text"] for p in doc.pages if p["location"] == f"sayfa {number}"), "")
            if doc
            else ""
        )

import asyncio

from katip.async_utils import wait_tasks
from katip.logging import get_logger
from katip.teams import store
from katip.teams.client import connector
from katip.teams.config import credentials

log = get_logger(__name__)


class ChatRouter:
    def __init__(self, meeting_id, browser_send, spawn):
        self.meeting_id = meeting_id
        self.browser_send = browser_send
        self.spawn = spawn
        self.lock = asyncio.Lock()
        self.pending = set()

    async def reference(self):
        if not credentials():
            return None
        rows = await store.references(self.meeting_id)
        return next((r.reference for r in rows if r.account_id is None), None)

    async def accepts_browser_input(self) -> bool:
        try:
            return await self.reference() is None
        except Exception as exc:
            log.warning("Sohbet sahipliği okunamadı", error_type=type(exc).__name__)
            return False

    def receive(self, author, text, enqueue):
        if not credentials():
            enqueue(author, text)
        else:
            self.spawn(self._receive(author, text, enqueue))

    async def _receive(self, author, text, enqueue):
        if await self.accepts_browser_input():
            enqueue(author, text)

    def send(self, text):
        if not credentials():
            self.browser_send(text)
        else:
            task = self.spawn(self._send(text))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def _send(self, text):
        async with self.lock:
            try:
                reference = await self.reference()
                if reference:
                    await connector.get().send(reference, text=text)
                    return
            except Exception as exc:
                log.warning("Teams sohbet iletisi gönderilemedi", error_type=type(exc).__name__)
            self.browser_send(text)

    async def flush(self):
        # Kapanışta sıraya alınmış sohbet cevapları iptal edilmeden tamamlanır.
        await wait_tasks(self.pending, budget_s=5, label="sohbet")

"""Döngüye ait durum ve süre sınırı olan görev kapanışı."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable

import anyio

from katip.logging import get_logger

log = get_logger(__name__)
_CANCEL_GRACE_S = 1.0


class LoopLocal[T]:
    """Durum yalnızca çalışan döngüde oluşturulur; kapalı döngüyü önbellekte tutmaz.

    Durum döngü nesnesinin üzerine yazılmaz: uvloop (Linux'ta uvicorn varsayılanı)
    döngüsünde `__dict__` ve zayıf referans desteği yoktur. Döngü kimliğiyle saklanır;
    kimlik yeniden kullanılabileceği için döngünün kendisi de karşılaştırılır.
    """

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._states: dict[int, tuple[asyncio.AbstractEventLoop, T]] = {}

    def _current(self) -> tuple[asyncio.AbstractEventLoop, tuple | None]:
        loop = asyncio.get_running_loop()
        entry = self._states.get(id(loop))
        if entry is None or entry[0] is not loop:
            # Kapanmış döngülerin durumu bırakılır; bellekte birikmez.
            for key, (old, _) in list(self._states.items()):
                if old is not loop and old.is_closed():
                    self._states.pop(key, None)
            entry = None
        return loop, entry

    def get(self) -> T:
        loop, entry = self._current()
        if entry is None:
            value = self._factory()
            self._states[id(loop)] = (loop, value)
            return value
        return entry[1]

    def set(self, value: T) -> None:
        loop, _ = self._current()
        self._states[id(loop)] = (loop, value)


def raise_if_cancelling() -> None:
    # Sürücü temizliği iptali başka bir hatayla örtebilir; işçi yeniden beklememeli.
    if (task := asyncio.current_task()) is not None and task.cancelling():
        raise asyncio.CancelledError


def _observe(task: asyncio.Task) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.warning(
            "arka plan görevi başarısız", task=task.get_name(), error_type=type(exc).__name__
        )


async def wait_tasks(
    tasks: Iterable[asyncio.Task], *, budget_s: float, label: str, cancel: bool = False
) -> set[asyncio.Task]:
    """wait_for iptal temizliğini sınırsız bekler; wait ile gerçek süre sınırı koy."""
    tasks = set(tasks)
    if not tasks:
        return set()
    if any(task.get_loop() is not asyncio.get_running_loop() for task in tasks):
        raise RuntimeError("Başka döngünün görevi beklenemez")
    for task in tasks:
        task.add_done_callback(_observe)
        if cancel:
            task.cancel()
    # WebSocket istemcisi AnyIO kapsamını iptal etse de kapanış sınırlı süre tamamlanır.
    with anyio.CancelScope(shield=True):
        try:
            _, pending = await asyncio.wait(tasks, timeout=budget_s)
            if pending and not cancel:
                log.warning("görev bekleme süresi doldu", work=label)
                for task in pending:
                    task.cancel()
                _, pending = await asyncio.wait(pending, timeout=_CANCEL_GRACE_S)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise
    if pending:
        log.warning(
            "görev kapanışı zaman aşımı", work=label, tasks=sorted(t.get_name() for t in pending)
        )
        for task in pending:
            task.cancel()
    return pending

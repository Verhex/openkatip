"""Yalnız kesintilerde geçiş; ret, yetki ve şema hataları tekrar denenmez."""

import errno
import socket
import time
from collections import deque
from collections.abc import Callable

import anthropic
import httpx
import httpx2
from websockets.exceptions import ConnectionClosedError, InvalidStatus


def transient(exc: Exception) -> bool:
    # SSE hatasının HTTP yanıtı 200 olabilir; kesinti gövdesi önceliklidir.
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else body
    error_type = error.get("type") if isinstance(error, dict) else getattr(error, "type", None)
    if error_type in ("overloaded_error", "api_error", "rate_limit_error"):
        return True
    if isinstance(exc, ConnectionClosedError):
        return exc.rcvd is None or exc.rcvd.code in (1006, 1011, 1012, 1013)
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(exc, InvalidStatus):
        status = exc.response.status_code
    if status is not None:
        return str(status) == "429" or (str(status).isdigit() and 500 <= int(status) <= 599)
    if isinstance(exc, OSError) and exc.errno in (
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
    ):
        return True
    return isinstance(
        exc,
        (
            ConnectionError,
            socket.gaierror,
            TimeoutError,
            anthropic.APIConnectionError,
            httpx.TransportError,
            httpx2.TransportError,
        ),
    )


class ProviderOutage(RuntimeError):
    """HTTP dışındaki batch sonucu da aynı geçici hata sınıflamasına katılır."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__("Sağlayıcı kesintisi")


class CircuitBreaker:
    """30 saniyede üç hata: 60 saniye açık, ardından tek deneme."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.errors: deque[float] = deque()
        self.open_until = 0.0
        self.probing = False

    def acquire(self) -> bool:
        now = self.clock()
        if self.probing or now < self.open_until:
            return False
        if self.open_until:
            self.probing = True
        return True

    def success(self) -> None:
        if self.probing or self.open_until:
            self.errors.clear()
        self.open_until = 0.0
        self.probing = False

    def failure(self) -> None:
        now = self.clock()
        while self.errors and self.errors[0] < now - 30:
            self.errors.popleft()
        self.errors.append(now)
        if self.probing or len(self.errors) >= 3:
            self.open_until = now + 60
        self.probing = False

    def release(self) -> None:
        self.probing = False

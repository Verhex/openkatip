"""Bot süreçlerini başlatıp izler.

Yerel alt süreç veya toplantı başına Docker konteyneri çalıştırır.
Docker backend ayrı bir Docker host/daemon gerektirir.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys

from katip.config import get_settings
from katip.logging import get_logger

log = get_logger(__name__)


# Bot sürecine geçmeyecek ortam değişkenleri: API anahtarları, yönetici token'ı, veritabanı
# ve şifreleme anahtarları. Tarayıcı güvenilmeyen web içeriği çalıştırır; bir açık, bu
# değerlere erişim demek olmasın.
_WORKER_ENV = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "LANG",
        "LC_ALL",
        "DISPLAY",
        "XAUTHORITY",
        "PULSE_SERVER",
        "XDG_RUNTIME_DIR",
        "XDG_CACHE_HOME",
        "LD_LIBRARY_PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)


def bot_environment(bot_token: str) -> dict[str, str]:
    # An allowlist also excludes credentials with unexpected names (e.g. AUTH).
    env = {k: v for k, v in os.environ.items() if k.upper() in _WORKER_ENV}
    env["KATIP_BOT_TOKEN"] = bot_token
    settings = get_settings()
    for field in (
        "bot_display_name",
        "bot_sandbox",
        "bot_site_isolation",
        "bot_block_private_network",
        "bot_headless",
        "bot_join_timeout_s",
        "sample_rate",
        "ingest_ws_url",
        "data_dir",
        "max_meeting_minutes",
        "bot_disconnect_timeout_s",
    ):
        value = getattr(settings, field)
        env["KATIP_" + field.upper()] = (
            str(value).lower() if isinstance(value, bool) else str(value)
        )
    return env


class LimitExceeded(RuntimeError):
    """Harcama sigortası: yeni bot başlatılamaz."""


class BotSupervisor:
    def __init__(self) -> None:
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._watchdogs: dict[str, asyncio.Task] = {}
        self._containers: dict[str, str] = {}

    def check_capacity(self) -> None:
        limit = get_settings().max_concurrent_bots
        if len(self._procs) >= limit:
            raise LimitExceeded(f"aynı anda en fazla {limit} bot çalışabilir")

    async def start(self, session_id: str, url: str, max_minutes: int | None = None) -> None:
        if session_id in self._procs:
            raise RuntimeError(f"oturum zaten çalışıyor: {session_id}")
        self.check_capacity()
        from katip.api.security import issue_bot_token

        settings = get_settings()
        if settings.bot_backend == "remote":
            return  # Durable queue is claimed by a separately authenticated manager.
        token = issue_bot_token(session_id)
        minutes = max_minutes or settings.max_meeting_minutes
        from katip.workers import jobs

        await jobs.started_local(session_id, minutes)
        env = bot_environment(token)
        args = ["--url", url, "--session-id", session_id, "--max-minutes", str(minutes)]
        try:
            if settings.bot_backend == "docker":
                name = container_name(session_id)
                worker_env = docker_environment(token)
                command = docker_command(name, args, worker_env)
                from katip.workers.runtime import docker_call

                self._containers[session_id] = name
                code, _, _ = await docker_call(*command[1:], env={**os.environ, **worker_env})
                if code != 0:
                    raise RuntimeError("Docker worker oluşturulamadı")
                proc = await asyncio.create_subprocess_exec("docker", "start", "--attach", name)
            else:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "katip.bot.runner", *args, env=env
                )
        except BaseException:
            from katip.api.security import revoke_bot_token

            revoke_bot_token(session_id)
            if name := self._containers.get(session_id):
                from katip.workers.runtime import remove_container

                try:
                    await remove_container(name)
                    self._containers.pop(session_id, None)
                except Exception:
                    pass  # Keep ownership for the health loop to retry removal.
            raise
        self._procs[session_id] = proc
        log.info("bot süreci başladı", session_id=session_id, pid=proc.pid)
        asyncio.create_task(self._reap(session_id, proc))
        self._watchdogs[session_id] = asyncio.create_task(
            self._watchdog(session_id, max_minutes or get_settings().max_meeting_minutes)
        )

    async def _reap(self, session_id: str, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        if name := self._containers.get(session_id):
            from katip.workers import jobs
            from katip.workers.runtime import remove_container

            try:
                await remove_container(name)
            except Exception:
                await jobs.request_cancel(session_id, "container_stop_unconfirmed")
                return
        was_active = self._procs.get(session_id) is proc
        self._procs.pop(session_id, None)
        from katip.api.security import revoke_bot_token

        revoke_bot_token(session_id)
        self._containers.pop(session_id, None)
        if (dog := self._watchdogs.pop(session_id, None)) is not None:
            dog.cancel()
        log.info("bot süreci bitti", session_id=session_id, exit_code=code)
        from katip.workers import jobs

        await jobs.finish_local(session_id, failed=code != 0)
        if was_active:
            from katip.events import SessionState
            from katip.pipeline.session import registry
            from katip.store import repo

            if session := registry.get(session_id):
                session.set_state(SessionState.FAILED if code != 0 else SessionState.ENDED)
                await registry.drop(session_id)
                await repo.finish_meeting(session_id, "failed" if code != 0 else "ended")

    async def _watchdog(self, session_id: str, minutes: int) -> None:
        """Bot kendi süre sınırını uygulayamazsa (takılırsa) süreci dışarıdan sonlandır."""
        await asyncio.sleep(minutes * 60 + 120)
        if session_id in self._procs:
            log.warning("süre sınırı: bot zorla durduruluyor", session_id=session_id)
            await self.stop(session_id)

    async def stop(self, session_id: str) -> bool:
        from katip.workers import jobs

        row = await jobs.get(session_id)
        if row:
            await jobs.request_cancel(session_id)
        if row and row.backend == "remote":
            return row.status in ("cancelled", "completed", "failed")
        if (
            dog := self._watchdogs.pop(session_id, None)
        ) is not None and dog is not asyncio.current_task():
            dog.cancel()
        proc = self._procs.pop(session_id, None)
        if proc is None:
            # Recover a deterministic Docker container after an API crash.
            if row and row.backend == "docker" and row.status in ("running", "stopping"):
                from katip.workers.runtime import remove_container

                await remove_container(container_name(session_id))
                await jobs.finish_local(session_id)
                return True
            if row and row.backend == "local" and row.status == "stopping":
                # Local orphan ownership cannot be proven; don't claim successful removal.
                from katip.workers.runtime import mark_local_lost

                await mark_local_lost(session_id)
            return False
        from katip.api.security import revoke_bot_token

        revoke_bot_token(session_id)
        # Kill the owned container as well as its docker CLI. Terminating the CLI
        # alone does not guarantee that Chromium has left the meeting.
        if name := self._containers.pop(session_id, None):
            from katip.workers.runtime import remove_container

            try:
                await remove_container(name)
            except Exception:
                self._procs[session_id] = proc
                self._containers[session_id] = name
                raise
        if proc.returncode is not None:
            await jobs.finish_local(session_id)
            return True
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        await jobs.finish_local(session_id)
        return True

    def running(self) -> list[str]:
        return list(self._procs)


supervisor = BotSupervisor()


def container_name(session_id: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", session_id):
        raise ValueError("Geçersiz toplantı kimliği")
    return f"katip-meeting-{session_id}"


def docker_environment(token: str) -> dict[str, str]:
    settings = get_settings()
    if settings.bot_sandbox != "on" or not settings.bot_site_isolation:
        raise RuntimeError("Docker worker sandbox ve site yalıtımı gerektirir")
    env = {k: v for k, v in bot_environment(token).items() if k.startswith("KATIP_")}
    env["KATIP_INGEST_WS_URL"] = settings.bot_worker_ingest_url
    env["KATIP_DATA_DIR"] = "/tmp/katip"
    return env


def worker_image() -> str:
    settings = get_settings()
    image, digest = settings.bot_image, settings.bot_image_digest
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]*", image):
        raise ValueError("Worker imajı geçersiz")
    if digest:
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError("Worker imaj özeti sha256 olmalı")
        if "@" in image and image.split("@", 1)[1] != digest:
            raise ValueError("Worker imajı yapılandırılan özetle eşleşmiyor")
        return image.split("@", 1)[0] + "@" + digest
    return image


def docker_command(name: str, args: list[str], env: dict[str, str]) -> list[str]:
    settings = get_settings()
    return [
        "docker",
        "create",
        "--rm",
        "--init",
        "--pull=never",
        "--name",
        name,
        "--network",
        settings.bot_network,
        "--sysctl=net.ipv6.conf.all.disable_ipv6=1",
        "--user",
        "1000:1000",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--security-opt=seccomp=docker/worker-seccomp.json",
        "--pids-limit=256",
        f"--memory={settings.bot_memory_mb}m",
        f"--memory-swap={settings.bot_memory_mb}m",
        f"--cpus={settings.bot_cpus}",
        "--shm-size=256m",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        "--tmpfs=/home/katip/.cache:rw,nosuid,nodev,size=128m,uid=1000,gid=1000",
        *[item for key in sorted(env) for item in ("--env", key)],
        worker_image(),
        *args,
    ]

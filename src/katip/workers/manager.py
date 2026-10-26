"""Linux worker host: Docker only, no provider credentials or database access.

python -m katip.workers.manager
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from urllib.parse import urlsplit

import httpx

from katip.bot.platforms.base import platform_for
from katip.bot.supervisor import container_name, docker_command, docker_environment, worker_image
from katip.config import Settings, get_settings
from katip.logging import get_logger, setup_logging
from katip.workers.runtime import docker_call, remove_container

log = get_logger(__name__)
_ID = re.compile(r"^[a-f0-9]{12}$")
_LEASE = re.compile(r"^[a-f0-9]{48}$")
_JOB_FIELDS = frozenset({"session_id", "lease_id", "url", "max_minutes", "lease_s", "environment"})
_ENV = frozenset(
    {
        "KATIP_BOT_TOKEN",
        "KATIP_BOT_DISPLAY_NAME",
        "KATIP_BOT_SANDBOX",
        "KATIP_BOT_SITE_ISOLATION",
        "KATIP_BOT_BLOCK_PRIVATE_NETWORK",
        "KATIP_BOT_HEADLESS",
        "KATIP_BOT_JOIN_TIMEOUT_S",
        "KATIP_SAMPLE_RATE",
        "KATIP_MAX_MEETING_MINUTES",
        "KATIP_BOT_DISCONNECT_TIMEOUT_S",
    }
)


def api_url(value: str, internal_hostname: str = "") -> str:
    if internal_hostname and not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,62}", internal_hostname):
        raise ValueError("İç worker API adresi tek bir servis adı olmalı")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Worker API adresi yalnızca sunucu kökü olmalı")
    allowed_http = {"127.0.0.1", "localhost", "::1"}
    if internal_hostname:
        allowed_http.add(internal_hostname.lower())
    if parsed.scheme != "https" and parsed.hostname not in allowed_http:
        raise ValueError("Uzak worker API bağlantısı HTTPS gerektirir")
    return value.rstrip("/")


def identity(job: dict) -> tuple[str, str]:
    session_id, lease_id = job.get("session_id", ""), job.get("lease_id", "")
    if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
        raise ValueError("Geçersiz worker toplantı kimliği")
    if not isinstance(lease_id, str) or not _LEASE.fullmatch(lease_id):
        raise ValueError("Geçersiz worker lease kimliği")
    return session_id, lease_id


def environment(job: dict, base: str) -> dict[str, str]:
    # İmaj, mount, cihaz, ağ ve Docker bayrakları yalnız host ayarlarından gelir.
    if set(job) - _JOB_FIELDS:
        raise ValueError("Worker işi izin listesini aşıyor")
    platform_for(job["url"])
    if type(job["max_minutes"]) is not int or not 1 <= job["max_minutes"] <= 1440:
        raise ValueError("Geçersiz worker süre sınırı")
    supplied = job["environment"]
    if not isinstance(supplied, dict) or set(supplied) - _ENV:
        raise ValueError("Worker ortamı izin listesini aşıyor")
    if any(not isinstance(v, str) or len(v) > 1000 for v in supplied.values()):
        raise ValueError("Geçersiz worker ortamı")
    token = supplied.get("KATIP_BOT_TOKEN", "")
    if len(token) < 32:
        raise ValueError("Worker token eksik")
    env = docker_environment(token)
    env.update(supplied)
    env.update(
        {
            "KATIP_BOT_SANDBOX": "on",
            "KATIP_BOT_SITE_ISOLATION": "true",
            "KATIP_BOT_BLOCK_PRIVATE_NETWORK": "true",
            "KATIP_DATA_DIR": "/tmp/katip",
            "KATIP_INGEST_WS_URL": base.replace("https://", "wss://", 1).replace(
                "http://", "ws://", 1
            )
            + "/ws/ingest",
        }
    )
    return env


class Manager:
    def __init__(self, client: httpx.AsyncClient, base: str) -> None:
        self.client, self.base = client, base

    async def request(self, path: str, payload: dict | None = None):
        response = await self.client.request(
            "GET" if payload is None else "POST", "/api/workers/" + path, json=payload
        )
        response.raise_for_status()
        return response.json()

    async def acknowledge(self, session_id: str, lease_id: str, code: int) -> None:
        await self.request(
            "finish",
            {
                "session_id": session_id,
                "lease_id": lease_id,
                "exit_code": max(-255, min(255, code)),
                "stop_confirmed": True,
            },
        )

    async def reconcile(self) -> None:
        # A single manager service per ID is required. Never attach to old browsers.
        for job in await self.request("owned"):
            session_id, lease_id = identity(job)
            await remove_container(container_name(session_id))
            await self.acknowledge(session_id, lease_id, 1)

    async def execute(self, job: dict, claimed_at: float) -> None:
        session_id, lease_id = identity(job)
        env = environment(job, self.base)
        name = container_name(session_id)
        proc = None
        code = 1
        # Account for request/response time; never extend the lease locally.
        lease_s = int(job["lease_s"])
        if not 30 <= lease_s <= 300:
            raise ValueError("Geçersiz worker lease süresi")
        expires = claimed_at + lease_s
        try:
            command = docker_command(
                name,
                [
                    "--url",
                    job["url"],
                    "--session-id",
                    session_id,
                    "--max-minutes",
                    str(job["max_minutes"]),
                ],
                env,
            )
            result, _, _ = await docker_call(*command[1:], env={**os.environ, **env})
            if result != 0:
                raise RuntimeError("Worker oluşturulamadı")
            if time.monotonic() >= expires:
                return
            proc = await asyncio.create_subprocess_exec("docker", "start", "--attach", name)
            while proc.returncode is None:
                if time.monotonic() >= expires:
                    log.warning("worker lease doldu", session_id=session_id)
                    break
                try:
                    await asyncio.wait_for(
                        proc.wait(), min(10, max(0.1, expires - time.monotonic()))
                    )
                    code = proc.returncode
                    break
                except TimeoutError:
                    pass
                sent_at = time.monotonic()
                try:
                    response = await self.request(
                        "heartbeat",
                        {
                            "session_id": session_id,
                            "lease_id": lease_id,
                        },
                    )
                except (httpx.HTTPError, ValueError):
                    continue  # Original deadline remains in force.
                if not response.get("continue"):
                    break
                # Keep original negotiated lease length; server configuration changes
                # cannot cause a client to outlive an earlier shorter lease.
                expires = sent_at + min(lease_s, int(response["lease_s"]))
        finally:
            # Removal failure is fatal: don't acknowledge stop or start another call.
            await remove_container(name)
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
            # If the HTTP response is lost, reconcile /owned before claiming again.
            await self.acknowledge(session_id, lease_id, code)

    async def run(self) -> None:
        while True:
            try:
                await self.reconcile()
                claimed_at = time.monotonic()
                result = await self.request("claim", {})
                if result.get("job"):
                    await self.execute(result["job"], claimed_at)
                else:
                    await asyncio.sleep(2)
            except httpx.HTTPError:
                # No response/body/credential logging; authentication failures fail closed.
                log.warning("worker API erişilemiyor")
                await asyncio.sleep(5)


async def run_manager() -> None:
    settings = get_settings()
    base = api_url(settings.worker_api_url, settings.worker_api_internal_hostname)
    worker_image()  # Geçersiz/sabitlemeyle çelişen imajla iş talep etme.
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", settings.worker_manager_id):
        raise ValueError("Worker manager kimliği geçersiz")
    if len(settings.worker_manager_key) < 32:
        raise ValueError("Worker manager anahtarı en az 32 karakter olmalı")
    async with httpx.AsyncClient(
        base_url=base,
        timeout=5,
        follow_redirects=False,
        trust_env=False,
        headers={
            "X-Katip-Worker-Id": settings.worker_manager_id,
            "Authorization": "Bearer " + settings.worker_manager_key,
        },
    ) as client:
        await Manager(client, base).run()


def main() -> None:
    # Remote manager configuration is explicit; never load a customer's API .env.
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    setup_logging()
    asyncio.run(run_manager())


if __name__ == "__main__":
    main()

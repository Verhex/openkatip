from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
import sys
import uuid

from katip.config import Settings


async def parse_subprocess(
    data: bytes, kind: str, settings: Settings, *, docker: bool = False
) -> list[dict]:
    payload = json.dumps({"kind": kind, "data": base64.b64encode(data).decode()}).encode()
    name = "katip-parser-" + uuid.uuid4().hex
    if docker:
        command = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--interactive",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--user=1000:1000",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=32",
            "--memory=512m",
            "--cpus=1",
            "--entrypoint=python",
            settings.bot_image,
            "-m",
            "katip.documents.parser",
        ]
    else:
        command = [sys.executable, "-m", "katip.documents.parser"]
    # No API secrets or .env access is required by the parser.
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONPATH"}
    }
    env["PYTHONIOENCODING"] = "utf-8"
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        start_new_session=not docker and sys.platform != "win32",
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(payload), settings.document_parser_timeout_s
        )
        if proc.returncode != 0:
            raise ValueError(
                "Belge okunamadı; metinli, makrosuz ve harici bağlantısız sürüm yükleyin"
            )
        result = json.loads(stdout)
        if not isinstance(result.get("pages"), list):
            raise ValueError("Belge işleme sonucu geçersiz")
        return result["pages"]
    finally:
        if not docker and sys.platform != "win32":
            # İstek bittiğinde olası torun süreçler de servis içinde kalmasın.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
            if docker:
                cleanup = await asyncio.create_subprocess_exec(
                    "docker",
                    "rm",
                    "--force",
                    name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(cleanup.wait(), 10)
                except TimeoutError:
                    cleanup.kill()
                    await cleanup.wait()

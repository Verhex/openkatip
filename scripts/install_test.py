"""Anahtarsız kurulum kontrolü: gerçek HTTP, SQLite, parser ve süreç kapanışı."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "SONIOX_API_KEY",
    "ASSEMBLYAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "MONDAY_API_TOKEN",
)
HTTP = build_opener(ProxyHandler({}))


def request(url: str, *, token: str = "", data: bytes | None = None) -> tuple[int, object]:
    headers = {"Authorization": "Bearer " + token} if token else {}
    try:
        with HTTP.open(Request(url, data=data, headers=headers), timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, None


def check_http(base: str, token: str, process: subprocess.Popen | None = None) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("API sağlık kontrolünden önce sonlandı; api.log dosyasına bakın")
        try:
            status, body = request(base + "/health")
            if status == 200:
                assert body == {"status": "ok"}, body
                break
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(1)
    else:
        raise TimeoutError("API 60 saniyede hazır olmadı")

    # /health DB sorgulamaz; /people iki gerçek DB sorgusu çalıştırır.
    status, body = request(base + "/api/people", token=token)
    assert status == 200 and body == [], (status, body)
    status, body = request(base + "/api/meetings", token=token)
    assert status == 200 and body == [], (status, body)
    status, _ = request(base + "/api/people")
    assert status == 401, status
    print("HTTP 200, JSON, kimlik doğrulama ve veritabanı kontrolü geçti")


def stop(process: subprocess.Popen) -> None:
    assert process.poll() is None, "Servis kontrol sırasında beklenmedik şekilde sonlandı"
    started = time.monotonic()
    # POSIX: SIGTERM. Windows: TerminateProcess; düzgün kapanış kanıtı değildir.
    process.terminate()
    try:
        code = process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise AssertionError("Servis 15 saniyede kapanmadı") from None
    if os.name != "nt":
        assert code in (0, -15), code
    print(f"Süreç {time.monotonic() - started:.2f} saniyede sonlandı")


def native(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    work = log_dir / ("native-" + uuid.uuid4().hex)
    work.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("KATIP_") and k.upper() not in PROVIDER_KEYS
    }
    env.pop("PYTHONPATH", None)
    env.update(dict.fromkeys(PROVIDER_KEYS, ""))
    parser_key = secrets.token_urlsafe(48)
    key_file = work / "parser.key"
    key_file.write_text(parser_key, encoding="utf-8")
    if os.name != "nt":
        key_file.chmod(0o600)
    env.update(
        KATIP_ADMIN_TOKEN=secrets.token_urlsafe(48),
        KATIP_SECRET_KEY=secrets.token_urlsafe(48),
        KATIP_DATA_KEY=base64.b64encode(secrets.token_bytes(32)).decode(),
        KATIP_REQUIRE_ENCRYPTION="true",
        KATIP_DATA_DIR=str(work / "data"),
        KATIP_DATABASE_URL=f"sqlite+aiosqlite:///{(work / 'katip.db').as_posix()}",
        KATIP_PANEL_DIR=str(ROOT / "web" / "panel"),
        KATIP_PARSER_KEY_FILE=str(key_file),
        KATIP_AUTO_REPORT="false",
        KATIP_CALENDAR_AUTOJOIN="false",
        KATIP_VOICE_ENABLED="false",
        PYTHONUNBUFFERED="1",
        PYTHONIOENCODING="utf-8",
    )
    processes = []
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        api_command = [
            sys.executable,
            "-m",
            "uvicorn",
            "katip.api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ]
        if os.name == "nt":
            api_command += ["--loop", "asyncio"]
        else:
            # Linux kilidi ve uvicorn'un varsayılan auto seçimi uvloop'u kullanmalı.
            import uvloop  # noqa: F401

        with (log_dir / "api.log").open("w", encoding="utf-8") as api_log:
            api = subprocess.Popen(
                api_command,
                cwd=work,
                env=env,
                stdout=api_log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            processes.append(api)
            check_http("http://127.0.0.1:8000", env["KATIP_ADMIN_TOKEN"], api)
            stop(api)
        if os.name != "nt":
            assert "Application shutdown complete." in (log_dir / "api.log").read_text()

        with (log_dir / "parser.log").open("w", encoding="utf-8") as parser_log:
            parser = subprocess.Popen(
                [sys.executable, "-m", "katip.documents.server"],
                cwd=work,
                env=env,
                stdout=parser_log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            processes.append(parser)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert parser.poll() is None, "Parser sonlandı; parser.log dosyasına bakın"
                try:
                    status, body = request(
                        "http://127.0.0.1:8001/parse?kind=txt",
                        token=parser_key,
                        data="Kurulum testi: Türkçe belge.".encode(),
                    )
                    assert status == 200, status
                    assert body == {
                        "pages": [{"location": "bölüm 1", "text": "Kurulum testi: Türkçe belge."}]
                    }, body
                    break
                except (URLError, TimeoutError, ConnectionError):
                    time.sleep(1)
            else:
                raise TimeoutError("Parser 60 saniyede hazır olmadı")
            status, _ = request("http://127.0.0.1:8001/parse?kind=txt", data=b"test")
            assert status == 401, status
            print("Parser kimliği ve gerçek belge alt süreci kontrolü geçti")
            stop(parser)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        shutil.rmtree(work)


def write_env(path: Path, values: dict[str, str], template: bool = False) -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8") if template else ""
    lines = [line for line in text.splitlines() if line.split("=", 1)[0] not in values]
    lines += [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def prepare(mode: str, log_dir: Path, env_file: Path) -> None:
    # Gerçek kurulumun .env dosyası test anahtarlarıyla ezilmesin; CI temiz kopyada çalışır.
    if env_file.exists() and not os.environ.get("GITHUB_ACTIONS"):
        raise SystemExit(f"{env_file} zaten var; üzerine yazılmaz. Boş bir test kopyası kullanın.")
    log_dir.mkdir(parents=True, exist_ok=True)
    values = dict.fromkeys(PROVIDER_KEYS, "")
    values.update(
        KATIP_ADMIN_TOKEN=secrets.token_urlsafe(48),
        KATIP_SECRET_KEY=secrets.token_urlsafe(48),
        KATIP_AUTO_REPORT="false",
        KATIP_CALENDAR_AUTOJOIN="false",
        KATIP_VOICE_ENABLED="false",
        KATIP_REQUIRE_ENCRYPTION="true",
    )
    data_key = base64.b64encode(secrets.token_bytes(32)).decode()
    if mode == "compose-env":
        for name, value in (("data.key", data_key), ("parser.key", secrets.token_urlsafe(48))):
            path = log_dir / name
            path.write_text(value + "\n", encoding="utf-8")
            path.chmod(0o600)
        password = secrets.token_urlsafe(48)
        values.update(
            POSTGRES_PASSWORD=password,
            KATIP_DATABASE_URL=f"postgresql+asyncpg://katip:{password}@postgres:5432/katip",
            KATIP_WORKER_MANAGER_KEY=secrets.token_urlsafe(48),
            KATIP_DOCKER_GID=str(Path("/var/run/docker.sock").stat().st_gid),
            KATIP_DATA_KEY_FILE=str(log_dir / "data.key"),
            KATIP_PARSER_KEY_FILE=str(log_dir / "parser.key"),
        )
    else:
        values.update(KATIP_DATA_KEY=data_key)
    write_env(env_file, values, template=mode == "compose-env")
    print("Geçici kurulum ayarları üretildi")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("native", "http", "compose-env", "railway-env"))
    parser.add_argument("--log-dir", type=Path, default=ROOT / "output" / "install-test")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    if args.mode == "native":
        native(args.log_dir.resolve())
    elif args.mode == "http":
        values = dict(
            line.split("=", 1)
            for line in args.env_file.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.startswith("#")
        )
        check_http(args.url, values["KATIP_ADMIN_TOKEN"])
    else:
        prepare(args.mode, args.log_dir.resolve(), args.env_file.resolve())


if __name__ == "__main__":
    main()

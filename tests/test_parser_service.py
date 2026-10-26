from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from katip.config import Settings, get_settings
from katip.documents import parser, process
from katip.documents.server import create_app
from katip.documents.service import parse_document


def test_compose_api_has_no_socket_and_parser_has_only_internal_network():
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.isolated.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    api, manager, parser = (services[name] for name in ("api", "manager", "parser"))
    assert api["environment"]["KATIP_BOT_BACKEND"] == "remote"
    assert "group_add" not in api
    assert api["volumes"] == ["server-data:/data"]
    assert [name for name, cfg in services.items() if "docker.sock" in str(cfg)] == ["manager"]
    assert manager["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock"]
    assert manager["networks"] == ["management"]
    assert compose["networks"]["management"]["internal"] is True
    assert "env_file" not in manager and "env_file" not in parser
    assert "ports" not in parser and "volumes" not in parser
    assert parser["networks"] == ["parsing"]
    assert compose["networks"]["parsing"]["internal"] is True
    assert [n for n, cfg in services.items() if "parsing" in cfg.get("networks", [])] == [
        "api",
        "parser",
    ]
    assert parser["secrets"] == ["parser_auth"]
    assert set(parser["environment"]) == {
        "KATIP_PARSER_KEY_FILE",
        "KATIP_DOCUMENT_MAX_BYTES",
        "KATIP_DOCUMENT_PARSER_TIMEOUT_S",
    }
    for cfg in (manager, parser):
        assert cfg["user"] == "1000:1000" and cfg["read_only"] is True
        assert cfg["cap_drop"] == ["ALL"] and "no-new-privileges:true" in cfg["security_opt"]
        assert cfg["tmpfs"] and cfg["cpus"] and cfg["mem_limit"] and cfg["pids_limit"]


@pytest.mark.parametrize("key", ["", "short", "ğ" * 40, "x" * 32 + "\n"])
def test_parser_refuses_invalid_auth_configuration(key):
    with pytest.raises(ValueError, match="Parser anahtarı"):
        create_app(Settings(_env_file=None, parser_key=key))


async def test_parser_client_and_server_reject_wrong_auth(parser_service):
    get_settings().parser_key = "wrong-" + "x" * 40
    with pytest.raises(ValueError, match="İzole belge"):
        await parse_document(b"hello", "txt")
    async with parser_service.client(base_url="http://parser:8001") as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = await client.post("/parse?kind=txt", content=b"hello", headers=headers)
            assert response.status_code == 401


async def test_parser_uses_secret_file_and_returns_turkish_text(parser_service, tmp_path):
    path = tmp_path / "auth.key"
    path.write_text(get_settings().parser_key + "\n", encoding="utf-8")
    get_settings().parser_key_file = str(path)
    get_settings().parser_key = ""
    pages = await parse_document("Ğ Ü Ş ı İ Ö Ç belge".encode(), "txt")
    assert pages == [{"location": "bölüm 1", "text": "Ğ Ü Ş ı İ Ö Ç belge"}]


@pytest.mark.parametrize("chunked", [False, True])
async def test_parser_server_bounds_size_before_spawning(parser_service, monkeypatch, chunked):
    parser_service.settings.document_max_bytes = 1024

    async def never_spawn(*args, **kwargs):
        pytest.fail("Boyut sınırını aşan belge için süreç başlatıldı")

    monkeypatch.setattr("katip.documents.server.parse_subprocess", never_spawn)

    async def chunks():
        yield b"x" * 600
        yield b"x" * 600

    async with parser_service.client(base_url="http://parser:8001") as client:
        response = await client.post(
            "/parse?kind=txt",
            content=chunks() if chunked else b"x" * 1200,
            headers={"Authorization": "Bearer " + get_settings().parser_key},
        )
    assert response.status_code == 413


async def test_parser_timeout_kills_and_reaps_request_process(parser_service, monkeypatch):
    parser_service.settings.document_parser_timeout_s = 0.01

    class Stalled:
        pid = 12345
        returncode = None
        killed = False
        reaped = False

        async def communicate(self, payload):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.reaped = True

    child = Stalled()

    async def spawn(*args, **kwargs):
        assert args[1:] == ("-m", "katip.documents.parser")
        assert not any("KEY" in key or "TOKEN" in key for key in kwargs["env"])
        return child

    monkeypatch.setattr(process.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(process.os, "killpg", lambda *args: None, raising=False)
    async with parser_service.client(base_url="http://parser:8001") as client:
        response = await client.post(
            "/parse?kind=txt",
            content=b"hello",
            headers={"Authorization": "Bearer " + get_settings().parser_key},
        )
    assert response.status_code == 504
    assert child.killed and child.reaped


async def test_parser_rejects_concurrent_requests(parser_service, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def wait(*args, **kwargs):
        started.set()
        await release.wait()
        return [{"location": "bölüm 1", "text": "hello"}]

    monkeypatch.setattr("katip.documents.server.parse_subprocess", wait)
    async with parser_service.client(base_url="http://parser:8001") as client:

        async def post():
            return await client.post(
                "/parse?kind=txt",
                content=b"hello",
                headers={"Authorization": "Bearer " + get_settings().parser_key},
            )

        first = asyncio.create_task(post())
        try:
            await started.wait()
            assert (await post()).status_code == 429
        finally:
            release.set()
            assert (await first).status_code == 200


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {}),
        (503, {}),
        (200, {"pages": [{"location": "x", "text": "x", "command": "evil"}]}),
        (200, {"pages": [{"location": "x", "text": "x" * (parser.MAX_TEXT + 1)}]}),
    ],
)
async def test_parser_failure_never_falls_back_to_api_process(monkeypatch, status, body):
    settings = get_settings()
    settings.parser_url = "http://parser:8001"
    settings.parser_key = "s" * 40
    real_client = httpx.AsyncClient

    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda req: httpx.Response(status, json=body))
        return real_client(**kwargs)

    async def never_spawn(*args, **kwargs):
        pytest.fail("İzole parser hatasında API yerel süreç başlattı")

    monkeypatch.setattr("katip.documents.client.httpx.AsyncClient", client)
    monkeypatch.setattr(process.asyncio, "create_subprocess_exec", never_spawn)
    with pytest.raises(ValueError):
        await parse_document(b"hello", "txt")


def test_parser_child_applies_linux_resource_limits(monkeypatch, capsys):
    limits = []
    resource = SimpleNamespace(
        RLIMIT_AS="memory",
        RLIMIT_CPU="cpu",
        RLIMIT_NOFILE="files",
        RLIMIT_FSIZE="writes",
        RLIMIT_CORE="core",
        setrlimit=lambda kind, value: limits.append((kind, value)),
    )
    payload = json.dumps({"kind": "txt", "data": base64.b64encode(b"hello").decode()}).encode()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    parser.main()
    assert limits == [
        ("memory", (512 * 1024 * 1024, 512 * 1024 * 1024)),
        ("cpu", (15, 15)),
        ("files", (64, 64)),
        ("writes", (0, 0)),
        ("core", (0, 0)),
    ]
    assert json.loads(capsys.readouterr().out)["pages"][0]["text"] == "hello"


async def test_completed_parser_request_also_cleans_its_linux_process_group(monkeypatch):
    groups = []
    child = SimpleNamespace(pid=12345, returncode=0)

    async def communicate(payload):
        return b'{"pages": [{"location": "section 1", "text": "hello"}]}', None

    child.communicate = communicate

    async def spawn(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        return child

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(process.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(process.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(process.os, "killpg", lambda pid, sig: groups.append(pid), raising=False)
    assert await process.parse_subprocess(b"hello", "txt", get_settings())
    assert groups == [child.pid]

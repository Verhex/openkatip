from __future__ import annotations

import socket

import pytest

from katip.bot.network import public_url
from katip.bot.supervisor import container_name, docker_command, docker_environment, worker_image
from katip.config import get_settings


def test_worker_has_no_server_mounts_or_secret_arguments(monkeypatch):
    monkeypatch.setenv("SONIOX_API_KEY", "must-not-reach-worker")
    monkeypatch.setenv("KATIP_BOT_SANDBOX", "on")
    get_settings.cache_clear()
    env = docker_environment("meeting-token")
    command = docker_command(container_name("abc"), ["--session-id", "abc"], env)
    assert env["KATIP_BOT_TOKEN"] == "meeting-token"
    assert env["KATIP_DATA_DIR"] == "/tmp/katip"
    assert "SONIOX_API_KEY" not in env and "KATIP_ADMIN_TOKEN" not in env
    assert "meeting-token" not in " ".join(command)
    assert "--read-only" in command and "--cap-drop=ALL" in command
    assert "--volume" not in command and "--mount" not in command
    assert "--network" in command and "--user" in command


def test_docker_requires_sandbox_and_rejects_unsafe_session_id():
    get_settings().bot_sandbox = "off"
    with pytest.raises(RuntimeError):
        docker_environment("token")
    with pytest.raises(ValueError):
        container_name("../../somebody-else")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api",
        "http://169.254.169.254/latest/meta-data",
        "http://10.1.2.3/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://localhost/",
        "http://api.internal/",
        "file:///data/katip.db",
        "http://224.0.0.1/",
    ],
)
async def test_browser_denies_internal_and_non_web_targets(url):
    assert not await public_url(url)


@pytest.mark.asyncio
async def test_dns_resolving_to_private_address_is_blocked(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80)),
        ],
    )
    assert not await public_url("https://apparently-public.example")


@pytest.mark.asyncio
async def test_public_http_and_websocket_addresses_are_allowed():
    assert await public_url("https://8.8.8.8/")
    assert await public_url("wss://8.8.8.8/socket")


def test_configured_digest_is_the_only_image_used_with_hardening():
    settings = get_settings()
    settings.bot_image = "registry.example/katip-worker:approved"
    settings.bot_image_digest = "sha256:" + "a" * 64
    env = docker_environment("t" * 40)
    command = docker_command(container_name("abc"), ["--session-id", "abc"], env)
    assert command[-3] == settings.bot_image + "@" + settings.bot_image_digest
    for flag in (
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--security-opt=seccomp=docker/worker-seccomp.json",
        "--pids-limit=256",
        "--memory=1024m",
        "--cpus=1.0",
        "--pull=never",
    ):
        assert flag in command
    assert command[command.index("--user") + 1] == "1000:1000"
    assert command[command.index("--network") + 1] == settings.bot_network
    assert not any(
        x.startswith(("--mount", "--volume", "--privileged", "--device")) for x in command
    )


@pytest.mark.parametrize(
    ("image", "digest"),
    [
        ("--privileged", ""),
        ("worker image", ""),
        ("katip-worker:local", "sha256:bad"),
        ("katip-worker@sha256:" + "a" * 64, "sha256:" + "b" * 64),
    ],
)
def test_configured_image_must_match_digest(image, digest):
    get_settings().bot_image = image
    get_settings().bot_image_digest = digest
    with pytest.raises(ValueError):
        worker_image()

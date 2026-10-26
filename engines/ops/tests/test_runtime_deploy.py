import asyncio
import importlib.metadata
import inspect
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import anthropic
import httpx
import openai
import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient
from test_jobs import runtime as runtime_fixture

from katip_ops.__main__ import check_config
from katip_ops.config import Schedule
from katip_ops.runtime_files import Documents, WorkFiles
from katip_ops.scheduler import build_scheduler
from katip_ops.slack.bridge import SlackBridge
from katip_ops.tools.github import GitHubReader

ROOT = Path(__file__).resolve().parents[1]
runtime = runtime_fixture


def test_scheduler_registration(runtime):
    runner, *_ = runtime
    scheduler = build_scheduler(runner.config.settings, runner)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"pipeline", "icerik", "geo", "cuma"}
    for name, day in [("pipeline", "tue"), ("icerik", "wed"), ("geo", "thu"), ("cuma", "fri")]:
        job = jobs[name]
        assert job.kwargs == {"job": name, "source": "schedule"}
        assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time == 3600
        assert str(job.trigger.timezone) == "Europe/Istanbul"
        next_run = job.trigger.get_next_fire_time(None, datetime(2026, 10, 26, tzinfo=UTC))
        assert next_run.strftime("%a").lower() == day
        assert next_run.hour == (16 if name == "cuma" else 10)
    config = runner.config.settings.model_copy(
        update={
            "schedules": {
                "pipeline": Schedule(day_of_week="mon", hour=8, minute=23),
                "cuma": Schedule(day_of_week="fri", enabled=False),
            }
        }
    )
    custom = build_scheduler(config, runner).get_jobs()
    assert [job.id for job in custom] == ["pipeline"]
    assert "minute='23'" in str(custom[0].trigger)


async def test_scheduler_runs_on_current_event_loop(runtime):
    runner, *_ = runtime
    scheduler = build_scheduler(runner.config.settings, runner)
    done = asyncio.Event()
    scheduler.add_job(done.set, "date", id="test")
    scheduler.start()
    await asyncio.wait_for(done.wait(), 3)
    assert scheduler._eventloop is asyncio.get_running_loop()
    scheduler.shutdown(wait=False)
    await asyncio.sleep(0)


@pytest.mark.parametrize("source", ["environment", "settings"])
async def test_real_slack_sdk_explicit_proxy_wiring(runtime, monkeypatch, source):
    runner, *_ = runtime
    monkeypatch.setattr("katip_ops.slack.bridge.read_secret", lambda path: "fake-token")
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy:3128")
    slack_settings = runner.config.settings.slack
    expected = "http://environment-proxy:3128"
    if source == "settings":
        expected = "http://settings-proxy:3128"
        slack_settings = slack_settings.model_copy(update={"proxy": expected})
    settings = runner.config.settings.model_copy(update={"slack": slack_settings})
    bridge = SlackBridge.from_settings(
        settings, runner.approvals, runner.slack.commands, runner.slack.applier
    )
    try:
        assert isinstance(bridge.handler, AsyncSocketModeHandler)
        assert isinstance(bridge.app.client, AsyncWebClient)
        assert bridge.handler.client.proxy == bridge.app.client.proxy == expected
        assert bridge.handler.client.web_client is bridge.app.client
        assert "proxy=self.proxy" in inspect.getsource(type(bridge.handler.client).connect)
    finally:
        await bridge.close()


@pytest.mark.parametrize("sdk", [anthropic, openai])
async def test_provider_httpx2_env_proxy_support(sdk, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "odoo-test")
    client = sdk.DefaultAsyncHttpxClient()
    try:
        import httpx2

        assert isinstance(client, httpx2.AsyncClient) and client.trust_env
        proxy = client._transport_for_url(httpx2.URL("https://api.example.test"))
        direct = client._transport_for_url(httpx2.URL("http://odoo-test:8069"))
        assert proxy is not direct
        assert proxy._pool._proxy_url.host == b"proxy.example"
    finally:
        await client.aclose()


async def test_github_scoped_collection_and_exclusions(runtime, tmp_path):
    runner, *_ = runtime
    calls = []
    token = tmp_path / "github-token"
    token.write_text("fake-read-token", encoding="utf-8")
    settings = runner.config.settings.github.model_copy(update={"token_file": token})

    def respond(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer fake-read-token"
        assert request.url.host == "api.github.com" and request.method == "GET"
        path = request.url.path
        if path == "/orgs/ExampleOrg/repos":
            data = [{"name": "included"}, {"name": "excluded"}]
        elif path.endswith("/pulls"):
            data = [
                {
                    "title": "Merged",
                    "body": "Untrusted PR",
                    "merged_at": "2026-10-27T10:00:00Z",
                    "updated_at": "2026-10-27T10:00:00Z",
                    "html_url": "https://github.com/pr/1",
                },
                {"merged_at": "2026-01-01T00:00:00Z", "updated_at": "2026-10-27T10:00:00Z"},
                {"merged_at": None, "updated_at": "2026-10-27T10:00:00Z"},
            ]
        elif path.endswith("/commits/abc"):
            data = {"files": [{"filename": "nested/DECISIONS.md", "patch": "+new decision"}]}
        elif path.endswith("/commits"):
            data = [
                {
                    "sha": "abc",
                    "html_url": "https://github.com/commit/abc",
                    "commit": {"message": "Untrusted commit"},
                }
            ]
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        reader = GitHubReader(settings, client=client)
        evidence = await reader.collect(datetime(2026, 10, 21, tzinfo=UTC), ["ExampleOrg/excluded"])
    assert len(evidence) == 3
    assert all("excluded" not in request.url.path for request in calls)
    assert any(request.url.path.endswith("/commits/abc") for request in calls)
    assert any(item["text"] == "+new decision" for item in evidence)


async def test_github_repo_pagination_is_complete(runtime, tmp_path):
    runner, *_ = runtime
    token = tmp_path / "token"
    token.write_text("fake", encoding="utf-8")
    settings = runner.config.settings.github.model_copy(update={"token_file": token})
    pages = []

    def respond(request):
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(200, json=[{"name": "excluded"}] * (100 if page == 1 else 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await GitHubReader(settings, client=client).collect(
            datetime.now(UTC), ["excluded"]
        )
    assert result == [] and pages == [1, 2]


async def test_github_empty_or_unavailable_repos_do_not_cancel_the_pack(runtime, tmp_path):
    runner, *_ = runtime
    token = tmp_path / "token"
    token.write_text("fake", encoding="utf-8")
    settings = runner.config.settings.github.model_copy(update={"token_file": token})

    def respond(request):
        path = request.url.path
        if path.endswith("/repos"):
            return httpx.Response(
                200,
                json=[{"name": "empty"}, {"name": "nopulls"}, {"name": "broken"}, {"name": "ok"}],
            )
        if "/empty/commits" in path:
            return httpx.Response(409, json={"message": "Git Repository is empty."})
        if "/nopulls/pulls" in path:
            return httpx.Response(404, json={"message": "Not Found"})
        if "/broken/" in path:
            return httpx.Response(500, json={})
        if path.endswith("/commits"):
            return httpx.Response(200, json=[])
        if path.endswith("/pulls"):
            return httpx.Response(
                200,
                json=[
                    {
                        "html_url": "https://github.com/pr/1",
                        "title": "Merged work",
                        "body": "",
                        "merged_at": "2026-10-25T10:00:00+00:00",
                        "updated_at": "2026-10-25T10:00:00+00:00",
                    }
                ],
            )
        raise AssertionError(path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await GitHubReader(settings, client=client).collect(
            datetime(2026, 10, 20, tzinfo=UTC), []
        )
    # empty (409) and nopulls (404) contribute nothing, broken (500) is skipped, ok is kept.
    assert [item["source"] for item in result].count("https://github.com/pr/1") == 2


def test_work_file_boundaries_and_decisions_diffs(tmp_path):
    files = WorkFiles(tmp_path)
    for value in ["../escape.md", "content/../../escape.md", "secrets/key", str(tmp_path / "abs")]:
        with pytest.raises(ValueError):
            files.write(value, "No")
    files.write("content/.decisions-baseline.md", "old\n")
    diff = files.decisions_diff("old\nnew\n")
    assert "+new" in diff and "-old" not in diff


def test_documents_require_explicit_directory(runtime, monkeypatch):
    runner, *_ = runtime
    monkeypatch.delenv("KATIP_CONFIG_DIR", raising=False)
    with pytest.raises(ValueError):
        Documents()
    with pytest.raises(ValueError):
        runner.documents.read("../escape.md")
    runner.documents.check()
    check_config(runner.config, runner.documents)


def test_compose_no_published_ports_and_internal_agent_networks():
    compose = yaml.safe_load((ROOT / "compose.ops.yml").read_text(encoding="utf-8"))
    assert compose["name"] == "katip-ops"
    for service in compose["services"].values():
        assert "ports" not in service
    agent = compose["services"]["ops-agent"]
    assert set(agent["networks"]) == {"ops-internal", "ops-odoo"}
    assert all(compose["networks"][name]["internal"] for name in agent["networks"])
    assert agent["read_only"] and agent["cap_drop"] == ["ALL"]
    assert agent["user"] == "10001:10001" and agent["cpus"] == 1 and agent["mem_limit"] == "1g"
    assert agent["security_opt"] == ["no-new-privileges:true"]
    assert agent["environment"]["HTTPS_PROXY"] == "http://ops-egress:3128"
    secret_mounts = [v for v in agent["volumes"] if "secrets" in v["source"]]
    assert len(secret_mounts) == 5 and all(v["read_only"] for v in secret_mounts)
    assert all(v["source"].split("/")[-1] != "odoo_test.conf" for v in secret_mounts)
    assert next(v for v in agent["volumes"] if v["source"].endswith("/config"))["read_only"]
    assert compose["services"]["odoo-test"]["volumes"][0]["read_only"] is True
    odoo = compose["services"]["odoo-test"]
    assert set(odoo["networks"]) == {"ops-odoo", "odoo-db"}
    # Odoo 19 rejects --dbfilter; db, dbfilter, list_db, workers and cron live in the
    # server-side config secret (see RUNBOOK.md), so the command only points at it.
    assert odoo["command"] == ["--config=/run/secrets/odoo_test.conf"]
    assert "ODOO_DB_NETWORK" in compose["networks"]["odoo-db"]["name"]


def test_squid_allowlist_and_connect_only():
    active = [
        line.strip()
        for line in (ROOT / "squid/allowlist.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert set(active) == {
        "api.anthropic.com",
        # ".slack.com" also matches slack.com; Squid refuses both in one dstdomain ACL.
        ".slack.com",
        ".slack-edge.com",
        "api.github.com",
    }
    assert all(item not in {"*", ".", "all", "0.0.0.0/0"} and "*" not in item for item in active)
    squid = (ROOT / "squid/squid.conf").read_text()
    assert "http_access deny !CONNECT" in squid and "http_access deny !TLS_port" in squid
    assert "acl TLS_port port 443" in squid and "http_access deny all" in squid
    assert "cache deny all" in squid
    log = next(line for line in squid.splitlines() if line.startswith("logformat"))
    assert "%ru" not in log and "%rp" not in log
    assert squid.index("http_access deny private_dst") < squid.index("http_access allow")


def test_runtime_lock_pins_installed_production_closure():
    requirements = [
        Requirement(line)
        for line in (ROOT / "requirements.lock").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    locked = {canonicalize_name(req.name): req for req in requirements}
    assert not set(locked) & {"pytest", "ruff", "mypy", "hatchling", "librt", "ast-serialize"}
    assert {"aiohttp", "apscheduler", "slack-sdk", "httpx2", "cryptography"} <= set(locked)
    for req in locked.values():
        assert str(req.specifier) == "==" + importlib.metadata.version(req.name)
    image = (ROOT / "docker/Dockerfile").read_text()
    assert "FROM python:3.12-slim" in image and "USER 10001:10001" in image
    assert "--requirement /app/requirements.lock" in image


def test_metadata_logs_omit_provider_messages_and_tracebacks():
    import logging

    from katip_ops.logging_config import MetadataFormatter

    record = logging.LogRecord("anthropic", logging.ERROR, "", 0, "SECRET_PROVIDER_BODY", (), None)
    output = MetadataFormatter().format(record)
    assert "SECRET_PROVIDER_BODY" not in output
    assert json.loads(output)["event"] == "dependency_log"


async def test_scheduler_failure_does_not_log_remote_exception(runtime, caplog):
    runner, *_ = runtime
    runner.execute = AsyncMock(side_effect=RuntimeError("SECRET_PROVIDER_BODY"))
    scheduler = build_scheduler(runner.config.settings, runner)
    job = scheduler.get_job("pipeline")
    await job.func(**job.kwargs)
    assert "scheduled_job_failed" in caplog.text and "SECRET_PROVIDER_BODY" not in caplog.text


@pytest.mark.parametrize(
    "job", ["icerik", "pipeline", "geo", "cuma", "content", "growth", "friday"]
)
def test_fake_dry_run_cli_is_fully_offline(runtime, tmp_path, job):
    runner, *_ = runtime
    directory = tmp_path / "cli"
    directory.mkdir()
    settings = (ROOT / "config/settings.example.toml").read_text(encoding="utf-8")
    settings = settings.replace('"../state/ops.sqlite3"', json.dumps(str(tmp_path / "cli.db")))
    settings = settings.replace('"../work"', json.dumps(str(tmp_path / "work")))
    (directory / "settings.toml").write_text(settings, encoding="utf-8")
    (directory / "executors.yaml").write_text(
        (ROOT / "config/executors.example.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "katip_ops", "--config", str(directory), "dry-run", job, "--fake"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
            "KATIP_CONFIG_DIR": str(runner.documents.directory),
        },
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert '"channel":' in result.stdout and '"run_id":' in result.stdout

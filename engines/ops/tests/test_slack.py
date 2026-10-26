import json
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sdk_sources import SITE_PACKAGES, signature, web_signature
from slack_sdk.models.blocks import Block
from slack_sdk.models.views import View

from katip_ops.config import Settings, SlackSettings
from katip_ops.core.approvals import Approvals
from katip_ops.core.contract import Cost, ProposedAction, Task
from katip_ops.core.ledger import Ledger
from katip_ops.slack import blocks
from katip_ops.slack.bridge import SlackBridge
from katip_ops.slack.commands import HELP, REFUSAL, Commands


class FakeSlack:
    def __init__(self):
        self.calls = []

    async def _call(self, name, kwargs):
        web_signature(name).bind(None, **kwargs)
        self.calls.append((name, kwargs))
        if name == "conversations_open":
            assert kwargs == {"users": "UOWNER"}
            return {"channel": {"id": "DOWNER"}}
        if name == "chat_postMessage":
            return {"channel": kwargs["channel"], "ts": f"{len(self.calls)}.001"}
        return {"ok": True}

    async def chat_postMessage(self, **kwargs):
        return await self._call("chat_postMessage", kwargs)

    async def chat_update(self, **kwargs):
        return await self._call("chat_update", kwargs)

    async def conversations_open(self, **kwargs):
        return await self._call("conversations_open", kwargs)

    async def views_open(self, **kwargs):
        View(**kwargs["view"]).validate_json()
        return await self._call("views_open", kwargs)


class FakeRunner:
    def __init__(self):
        self.calls = []

    async def enqueue(self, job, *, requested_by):
        self.calls.append((job, requested_by))
        return "queued-run"


class FakeApplier:
    def __init__(self):
        self.calls = []
        self.dry_run = False
        self.error = None

    async def apply(self, action, *, approval_id):
        self.calls.append((action, approval_id))
        if self.error:
            raise self.error
        return {"dry_run": self.dry_run}


@pytest.fixture
def bridge(tmp_path):
    clock = [datetime(2026, 10, 26, 21, 30, tzinfo=UTC).timestamp()]
    settings = Settings(
        owner_slack_id="UOWNER",
        ops_dir=tmp_path,
        slack=SlackSettings(
            slack_bot_token=tmp_path / "bot",
            slack_app_token=tmp_path / "app",
            channels={
                "ops": "COPS",
                "ops_approvals": "CAPPROVALS",
                "pipeline": "CPIPELINE",
                "social": "CSOCIAL",
            },
        ),
    )
    ledger = Ledger(tmp_path / "ops.sqlite3", clock=lambda: clock[0])
    approvals = Approvals(tmp_path / "ops.sqlite3", "UOWNER", clock=lambda: clock[0])
    runner = FakeRunner()
    commands = Commands(
        settings, ledger, approvals, runner, clock=lambda: datetime.fromtimestamp(clock[0], UTC)
    )
    client, applier = FakeSlack(), FakeApplier()
    instance = SlackBridge(settings, approvals, commands, applier, client=client)
    yield instance, client, applier, runner, clock
    approvals.close()
    ledger.close()


def callbacks():
    async def ack(**kwargs):
        signature("slack_bolt/context/ack/async_ack.py", "AsyncAck", "__call__").bind(
            None, **kwargs
        )

    async def respond(**kwargs):
        signature("slack_bolt/context/respond/async_respond.py", "AsyncRespond", "__call__").bind(
            None, **kwargs
        )

    return AsyncMock(side_effect=ack), AsyncMock(side_effect=respond)


async def send_card(bridge, *, opinion_check=False, action=None):
    instance = bridge[0]
    action = action or ProposedAction(
        kind="odoo.create_task",
        target="project.task",
        reason="Önemli bir iş",
        payload={
            "values": {"name": "İlk metin", "project_id": 2},
            "reason": "Önemli bir iş",
            "run_id": "run",
        },
    )
    identifier = instance.approvals.create(action, "task", "run")
    response = await instance.send_approval(
        identifier,
        title="Görev önerisi",
        body="Birinci satır\nİkinci satır\nÜçüncü satır",
        opinion_check=opinion_check,
    )
    return identifier, response["ts"]


def action_body(identifier, ts, action="approve", user="UOWNER", channel="CAPPROVALS"):
    return {
        "user": {"id": user},
        "channel": {"id": channel},
        "message": {"ts": ts},
        "trigger_id": "trigger",
        "actions": [{"action_id": f"katip_{action}", "value": identifier}],
    }


@pytest.mark.parametrize("channel", ["COPS", "CAPPROVALS", "CPIPELINE", "CSOCIAL", "DOWNER"])
async def test_channel_allowlist(bridge, channel):
    instance, client, *_ = bridge
    await instance.post(channel, {"text": "Deneme"})
    assert client.calls[-1][0] == "chat_postMessage"
    assert client.calls[-1][1]["channel"] == channel


@pytest.mark.parametrize("channel", ["COTHER", "DOTHER", "UOWNER", "#ops", "", "https://evil"])
async def test_posting_elsewhere_raises(bridge, channel):
    instance, client, *_ = bridge
    with pytest.raises(PermissionError):
        await instance.post(channel, {"text": "Deneme"})
    assert all(name != "chat_postMessage" for name, _ in client.calls)


async def test_message_kwargs_cannot_override_posting_gate(bridge):
    instance, client, *_ = bridge
    with pytest.raises(ValueError):
        await instance.post("COPS", {"text": "x", "channel": "COTHER"})
    assert not client.calls


def test_block_kit_builders_are_mobile_friendly_and_turkish():
    message = blocks.report_message("Cuma raporu", ["İşler tamamlandı", "İkinci bölüm"], None)
    assert "Maliyet: bilinmiyor" in json.dumps(message, ensure_ascii=False)
    assert "0.1250 USD" in blocks.report_message("Rapor", [], 0.125)["text"]
    card = blocks.approval_card(
        "approval-id", "Başlık", "Satır 1\nSatır 2\nSatır 3", "Gerekçe", opinion_check=True
    )
    serialized = json.dumps(card, ensure_ascii=False)
    assert "Neden önemli:" in serialized
    assert "Görüş kontrolü: Bu, içerik sahibinin gerçekten düşündüğü bir şey mi?" in serialized
    buttons = card["blocks"][-1]["elements"]
    assert [button["text"]["text"] for button in buttons] == ["Onayla", "Düzenle", "Reddet"]
    assert all(button["value"] == "approval-id" for button in buttons)
    for rendered in (message, card):
        for block in Block.parse_all(rendered["blocks"]):
            block.validate_json()
        assert "table" not in json.dumps(rendered)
    changed = blocks.decision_message(card["blocks"], "Reddedildi", "UOWNER")
    assert all(block["type"] != "actions" for block in changed["blocks"])
    assert "UOWNER" in changed["text"]
    assert card["blocks"][-1]["type"] == "actions"


@pytest.mark.parametrize("body", ["one", "one\ntwo", "\n".join(["a"] * 7), "one\n\nthree"])
def test_card_body_line_count(body):
    with pytest.raises(ValueError):
        blocks.approval_card("id", "Başlık", body, "Gerekçe")


async def test_approval_calls_applier_updates_same_card_and_is_single_use(bridge):
    instance, client, applier, *_ = bridge
    identifier, ts = await send_card(bridge, opinion_check=True)
    ack, respond = callbacks()
    await instance.on_action(ack, action_body(identifier, ts), respond)
    ack.assert_awaited_once_with()
    respond.assert_not_awaited()
    assert len(applier.calls) == 1
    assert instance.approvals.get(identifier).status == "applied"
    name, update = client.calls[-1]
    assert name == "chat_update" and update["channel"] == "CAPPROVALS" and update["ts"] == ts
    assert "Onaylandı ve uygulandı" in update["text"]
    assert "Görüş kontrolü" in json.dumps(update["blocks"], ensure_ascii=False)
    assert all(block["type"] != "actions" for block in update["blocks"])
    await instance.on_action(ack, action_body(identifier, ts), respond)
    assert "daha önce kullanıldı" in respond.call_args.kwargs["text"]
    assert len(applier.calls) == 1


async def test_reject_never_applies(bridge):
    instance, client, applier, *_ = bridge
    identifier, ts = await send_card(bridge)
    ack, respond = callbacks()
    await instance.on_action(ack, action_body(identifier, ts, "reject"), respond)
    assert not applier.calls
    assert instance.approvals.get(identifier).status == "rejected"
    assert "Reddedildi" in client.calls[-1][1]["text"]


async def test_expired_card_reports_turkish_error(bridge):
    instance, client, applier, _, clock = bridge
    identifier, ts = await send_card(bridge)
    clock[0] += 72 * 3600
    ack, respond = callbacks()
    await instance.on_action(ack, action_body(identifier, ts), respond)
    assert "süresi doldu" in respond.call_args.kwargs["text"]
    assert not applier.calls and client.calls[-1][0] == "chat_postMessage"
    assert instance.approvals.pending_count() == 0


@pytest.mark.parametrize(
    "dry_run,error,expected",
    [
        (True, None, "Deneme modu"),
        (False, RuntimeError("secret-value"), "Uygulama tamamlanamadı"),
    ],
)
async def test_dry_run_and_failed_application_do_not_mark_applied(
    bridge, dry_run, error, expected, caplog
):
    instance, client, applier, *_ = bridge
    applier.dry_run, applier.error = dry_run, error
    identifier, ts = await send_card(bridge)
    ack, respond = callbacks()
    await instance.on_action(ack, action_body(identifier, ts), respond)
    assert instance.approvals.get(identifier).status == "approved"
    assert expected in client.calls[-1][1]["text"]
    assert "secret-value" not in caplog.text + json.dumps(client.calls)
    assert all(b["type"] != "actions" for b in client.calls[-1][1]["blocks"])


@pytest.mark.parametrize("entry", ["command", "message", "approve", "reject", "edit", "modal"])
async def test_every_entry_checks_owner_before_any_side_effect(bridge, entry, caplog):
    instance, client, applier, runner, _ = bridge
    caplog.set_level(logging.WARNING)
    ack, respond = callbacks()
    if entry == "command":
        await instance.on_command(
            ack, {"user_id": "UOTHER", "channel_id": "COPS", "text": "çalıştır pipeline"}, respond
        )
        assert ack.call_args.kwargs["text"] == REFUSAL
    elif entry == "message":
        await instance.on_message(
            {"user": "UOTHER", "channel": "DOTHER", "channel_type": "im", "text": "fikir poison"}
        )
    elif entry == "modal":
        await instance.on_edit_submit(ack, {"user": {"id": "UOTHER"}})
        assert ack.call_args.kwargs["errors"]["edit"] == REFUSAL
    else:
        await instance.on_action(ack, action_body("missing", "1", entry, "UOTHER"), respond)
        assert respond.call_args.kwargs["text"] == REFUSAL
    assert "slack_owner_refused" in caplog.text
    assert not client.calls and not applier.calls and not runner.calls


async def test_command_and_owner_dm_use_same_commands(bridge):
    instance, client, _, runner, _ = bridge
    ack, respond = callbacks()
    await instance.on_command(
        ack, {"user_id": "UOWNER", "channel_id": "COPS", "text": "çalıştır geo"}, respond
    )
    assert runner.calls == [("geo", "UOWNER")]
    assert "kuyruğa alındı" in respond.call_args.kwargs["text"]
    await instance.on_message(
        {"user": "UOWNER", "channel": "DOWNER", "channel_type": "im", "text": "çalıştır cuma"}
    )
    assert runner.calls[-1] == ("cuma", "UOWNER")
    assert client.calls[-1][1]["channel"] == "DOWNER"
    assert "kuyruğa alındı" in client.calls[-1][1]["text"]


@pytest.mark.parametrize(
    "text",
    [
        "merhaba",
        "",
        "run x",
        "çalıştır",
        "çalıştır ../bad",
        "çalıştır pipeline extra",
        "fikir",
        "yardım extra",
    ],
)
async def test_unknown_commands_never_become_chat_or_jobs(bridge, text):
    instance, _, _, runner, _ = bridge
    assert await instance.commands.execute("UOWNER", text) == HELP
    assert not runner.calls


@pytest.mark.parametrize("job", ["pipeline", "icerik", "geo", "cuma"])
async def test_all_job_words(bridge, job):
    instance, _, _, runner, _ = bridge
    assert "kuyruğa alındı" in await instance.commands.execute("UOWNER", f"çalıştır {job}")
    assert runner.calls == [(job, "UOWNER")]


async def test_status_includes_pending_runs_and_unknown_monthly_cost(bridge):
    instance, _, _, _, clock = bridge
    ledger = instance.commands.ledger
    await send_card(bridge)
    assert "Bekleyen onay: 1" in await instance.commands.execute("UOWNER", "durum")
    task = Task(type="pipeline", source="slack", requested_by="UOWNER")
    ledger.record_start("one", task, "fake", "not persisted")
    ledger.record_end("one", "done", Cost(usd=1.25))
    status = await instance.commands.execute("UOWNER", "durum")
    assert "pipeline: tamamlandı" in status and "1.2500 USD" in status
    ledger.record_start("unknown", task, "fake", "private")
    status = await instance.commands.execute("UOWNER", "durum")
    assert "maliyeti: bilinmiyor" in status
    ledger.record_end("unknown", "done", Cost(usd=None))
    assert "maliyeti: bilinmiyor" in await instance.commands.execute("UOWNER", "durum")
    clock[0] = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
    assert "maliyeti: 0.0000 USD" in await instance.commands.execute("UOWNER", "durum")


async def test_idea_timestamp_append_and_path_traversal_is_data(bridge, tmp_path):
    instance, _, _, runner, _ = bridge
    malicious = "../../escape.md\nInjected line C:\\evil\\file.md"
    assert await instance.commands.execute("UOWNER", f"fikir {malicious}") == "Fikir kaydedildi."
    await instance.commands.execute("UOWNER", "fikir İkinci görüş")
    path = tmp_path / "content" / "ideas" / "2026-10-27.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and "../../escape.md Injected line" in lines[0]
    assert lines[0].startswith("- 2026-10-27T00:30:00+03:00 ")
    assert "İkinci görüş" in lines[1]
    assert list((tmp_path / "content" / "ideas").iterdir()) == [path]
    assert not runner.calls
    assert await instance.commands.execute("UOTHER", "fikir no") == REFUSAL
    assert path.read_text(encoding="utf-8").splitlines() == lines


async def test_idea_symlink_escape_rejected(bridge, tmp_path, monkeypatch):
    # Windows symlink creation needs privileges; simulate Path.resolve's junction result.
    from pathlib import Path

    instance = bridge[0]
    real_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path.name.endswith(".md"):
            return tmp_path.parent / "escape.md"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="dışında"):
        await instance.commands.execute("UOWNER", "fikir secret")
    assert not (tmp_path / "content").exists()


async def test_edit_modal_prefills_text_and_round_trips_edited_approval(bridge):
    instance, client, applier, *_ = bridge
    identifier, ts = await send_card(bridge)
    original = instance.approvals.get(identifier)
    ack, respond = callbacks()
    await instance.on_action(ack, action_body(identifier, ts, "edit"), respond)
    name, kwargs = client.calls[-1]
    assert name == "views_open" and kwargs["trigger_id"] == "trigger"
    view = kwargs["view"]
    assert view["blocks"][0]["element"]["initial_value"] == "İlk metin"
    view["state"] = {"values": {"edit": {"text": {"value": "Düzenlenmiş metin"}}}}
    submit_ack, _ = callbacks()
    await instance.on_edit_submit(submit_ack, {"user": {"id": "UOWNER"}, "view": view})
    submit_ack.assert_awaited_once_with()
    decision = instance.approvals.get(identifier)
    assert decision.edited and decision.digest != original.digest
    assert decision.original_digest == original.digest
    assert applier.calls[0][0].payload["values"]["name"] == "Düzenlenmiş metin"
    assert client.calls[-1][0] == "chat_update" and client.calls[-1][1]["ts"] == ts
    await instance.on_action(ack, action_body(identifier, ts), respond)
    assert "daha önce kullanıldı" in respond.call_args.kwargs["text"]


async def test_json_payload_edit_and_modal_digest_tampering(bridge):
    instance, client, applier, *_ = bridge
    action = ProposedAction(
        kind="odoo.move_lead_stage",
        target="crm.lead",
        reason="Gerekçe",
        payload={"values": {"stage_id": 2}, "reason": "Gerekçe", "run_id": "run", "record_id": 1},
    )
    identifier, ts = await send_card(bridge, action=action)
    ack, respond = callbacks()
    await instance.on_action(ack, action_body(identifier, ts, "edit"), respond)
    view = client.calls[-1][1]["view"]
    text = json.loads(view["blocks"][0]["element"]["initial_value"])
    text["values"]["stage_id"] = 3
    view["state"] = {"values": {"edit": {"text": {"value": json.dumps(text)}}}}
    metadata = json.loads(view["private_metadata"])
    correct = view["private_metadata"]
    metadata["digest"] = "forged"
    view["private_metadata"] = json.dumps(metadata)
    submit, _ = callbacks()
    await instance.on_edit_submit(submit, {"user": {"id": "UOWNER"}, "view": view})
    assert submit.call_args.kwargs["response_action"] == "errors" and not applier.calls
    assert instance.approvals.get(identifier).status == "pending"
    view["private_metadata"] = correct
    await instance.on_edit_submit(submit, {"user": {"id": "UOWNER"}, "view": view})
    assert applier.calls[0][0].payload["values"]["stage_id"] == 3


async def test_forged_card_location_never_applies(bridge):
    instance, _, applier, *_ = bridge
    identifier, ts = await send_card(bridge)
    ack, respond = callbacks()
    await instance.on_action(ack, action_body(identifier, "wrong-ts"), respond)
    assert "doğrulanamadı" in respond.call_args.kwargs["text"]
    await instance.on_action(ack, action_body(identifier, ts, channel="COTHER"), respond)
    assert not applier.calls


@pytest.mark.parametrize(
    "event",
    [
        {"channel_type": "channel", "text": "çalıştır pipeline"},
        {"channel_type": "im", "bot_id": "B1", "text": "çalıştır pipeline"},
        {"channel_type": "im", "app_id": "A08SKDT6QUW", "text": "çalıştır pipeline"},
        {"channel_type": "im", "subtype": "message_changed", "text": "çalıştır pipeline"},
        {"channel_type": "mpim", "text": "çalıştır pipeline"},
    ],
)
async def test_bot_edits_and_channel_chatter_are_ignored(bridge, event):
    instance, client, _, runner, _ = bridge
    await instance.on_message({"user": "UOWNER", **event})
    assert not runner.calls and not client.calls


async def test_missing_async_runtime_is_reported_without_network(bridge, monkeypatch):
    import builtins

    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "slack_bolt.adapter.socket_mode.async_handler":
            raise ModuleNotFoundError("aiohttp")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    instance = bridge[0]
    settings = instance.settings
    settings.slack.slack_bot_token.write_text("xoxb-offline-test", encoding="utf-8")
    settings.slack.slack_app_token.write_text("xapp-offline-test", encoding="utf-8")
    with pytest.raises(RuntimeError, match="aiohttp"):
        SlackBridge.from_settings(settings, instance.approvals, instance.commands, instance.applier)


def test_bolt_initialization_and_listener_signatures_in_installed_sources():
    signature("slack_bolt/app/async_app.py", "AsyncApp", "__init__").bind(
        None, client=object(), user_facing_authorize_error_message="Slack yetkisi doğrulanamadı."
    )
    for method, name in (
        ("command", "/katip"),
        ("event", "message"),
        ("action", "katip_approve"),
        ("view", "katip_edit_submit"),
    ):
        signature("slack_bolt/app/async_app.py", "AsyncApp", method).bind(None, name)
    signature(
        "slack_bolt/adapter/socket_mode/aiohttp/__init__.py", "AsyncSocketModeHandler", "__init__"
    ).bind(None, object(), app_token="xapp-fake")
    signature(
        "slack_bolt/adapter/socket_mode/async_base_handler.py",
        "AsyncBaseSocketModeHandler",
        "start_async",
    ).bind(None)
    exported = (SITE_PACKAGES / "slack_bolt/adapter/socket_mode/async_handler.py").read_text()
    assert "from .aiohttp import AsyncSocketModeHandler" in exported


@pytest.mark.parametrize("flow", ["approve", "edit", "reject", "dry_run", "invalid_edit"])
async def test_slack_to_real_odoo_applier_with_only_fake_transports(bridge, tmp_path, flow):
    import httpx

    from katip_ops.config import ApprovalRules, OdooSettings, PolicySettings
    from katip_ops.tools.odoo.client import OdooClient
    from katip_ops.tools.odoo.tools import OdooTools

    instance, client, _, _, _ = bridge
    key = tmp_path / "odoo-key"
    key.write_text("never-log-this-key", encoding="utf-8")
    settings = instance.settings.model_copy(
        update={
            "dry_run": flow == "dry_run",
            "odoo": OdooSettings(key_file=key),
            "policy": PolicySettings(
                allowed_kinds=["odoo.create_task"],
                approval_rules=ApprovalRules(stage1={"odoo.create_task": True}),
            ),
        }
    )
    requests = []

    def handle(request):
        method = request.url.path.split("/")[-1]
        body = json.loads(request.content)
        requests.append((method, body))
        if method == "search_read":
            return httpx.Response(200, json=[{"id": 1, "name": "agent-created"}])
        if method == "create":
            return httpx.Response(200, json=[55])
        return httpx.Response(200, json=1)

    odoo = OdooClient(settings.odoo, transport=httpx.MockTransport(handle))
    try:
        instance.applier = OdooTools(odoo, settings, instance.approvals)
        action = instance.applier.create_task(
            project_id=2, name="İlk görev", reason="Gerekçe", run_id="run"
        )
        identifier, ts = await send_card(bridge, action=action)
        ack, respond = callbacks()
        if flow in {"edit", "invalid_edit"}:
            await instance.on_action(ack, action_body(identifier, ts, "edit"), respond)
            view = client.calls[-1][1]["view"]
            view["state"] = {
                "values": {
                    "edit": {
                        "text": {"value": "" if flow == "invalid_edit" else "Düzenlenen görev"}
                    }
                }
            }
            await instance.on_edit_submit(ack, {"user": {"id": "UOWNER"}, "view": view})
        else:
            await instance.on_action(
                ack,
                action_body(identifier, ts, "reject" if flow == "reject" else "approve"),
                respond,
            )
        approval = instance.approvals.get(identifier)
        if flow in {"reject", "dry_run", "invalid_edit"}:
            assert not requests
            assert (
                approval.status
                == {"reject": "rejected", "dry_run": "approved", "invalid_edit": "pending"}[flow]
            )
        else:
            assert approval.status == "applied"
            assert [method for method, _ in requests] == ["search_read", "create", "message_post"]
            assert requests[1][1]["vals_list"][0]["name"] == (
                "Düzenlenen görev" if flow == "edit" else "İlk görev"
            )
            assert requests[-1][1]["body"] == "[Agent] Gerekçe (run run)"
    finally:
        await odoo.close()


def test_settings_reject_channel_names_and_unknown_roles():
    with pytest.raises(ValidationError):
        SlackSettings(channels={"ops": "#ops"})
    with pytest.raises(ValidationError):
        SlackSettings(channels={"customer": "CCUSTOMER"})

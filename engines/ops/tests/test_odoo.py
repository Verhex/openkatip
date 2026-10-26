import json
import logging

import httpx
import pytest

from katip_ops.config import ApprovalRules, OdooSettings, PolicySettings, Settings
from katip_ops.core.approvals import Approvals
from katip_ops.core.contract import ProposedAction
from katip_ops.core.policy import PolicyRejection
from katip_ops.tools.odoo.client import (
    OdooAccessError,
    OdooAuthenticationError,
    OdooClient,
    OdooConnectionError,
    OdooError,
    OdooNotFoundError,
    OdooValidationError,
)
from katip_ops.tools.odoo.tools import FIELDS, TAGS, OdooTools


class FakeOdoo:
    def __init__(self):
        self.requests = []
        self.reply = None

    def __call__(self, request):
        model, method = request.url.path.split("/")[-2:]
        body = json.loads(request.content)
        self.requests.append((model, method, body, request))
        if self.reply:
            return self.reply(model, method, body)
        if model in {"crm.tag", "project.tags"}:
            if method == "search_read":
                return httpx.Response(
                    200, json=[{"id": i + 1, "name": tag} for i, tag in enumerate(TAGS)]
                )
        if method == "create":
            return httpx.Response(200, json=[42])
        if method == "read":
            return httpx.Response(200, json=[{"description": "<p>Existing</p>"}])
        if method == "search_read":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=True)


@pytest.fixture
async def odoo(tmp_path):
    key = tmp_path / "odoo-key"
    key.write_text("private-odoo-key", encoding="utf-8")
    settings = Settings(
        owner_slack_id="UOWNER",
        dry_run=False,
        odoo=OdooSettings(key_file=key),
        policy=PolicySettings(
            allowed_kinds=[f"odoo.{name}" for name in FIELDS],
            approval_rules=ApprovalRules(
                stage1={
                    f"odoo.{name}": name in {"create_lead", "move_lead_stage", "create_task"}
                    for name in FIELDS
                }
            ),
        ),
    )
    server = FakeOdoo()
    client = OdooClient(settings.odoo, transport=httpx.MockTransport(server))
    approvals = Approvals(tmp_path / "approvals.sqlite3", "UOWNER")
    tools = OdooTools(client, settings, approvals)
    yield tools, server, approvals
    await client.close()
    approvals.close()


def make_write(tools, name, model=None):
    metadata = {"reason": "Deneme gerekçesi <safe>", "run_id": "run-1"}
    if name == "create_lead":
        return tools.create_lead(
            name="Aday", tag_ids=["icp-erp"], source="Public research", **metadata
        )
    if name == "move_lead_stage":
        return tools.move_lead_stage(9, stage_id=3, **metadata)
    if name == "create_task":
        return tools.create_task(project_id=2, name="Görev", **metadata)
    if name == "update_task":
        return tools.update_task(9, description="<p>Added</p>", **metadata)
    if name == "log_activity":
        return tools.log_activity(
            model or "crm.lead",
            9,
            activity_type="call",
            summary="Ara",
            note="Not",
            date_deadline="2026-11-01",
            **metadata,
        )
    return tools.add_note(model or "project.task", 9, body="İç not", **metadata)


def approve(store, action):
    identifier = store.create(action, "task-1", "run-1")
    store.decision(identifier, "approved", "UOWNER")
    return identifier


@pytest.mark.parametrize("name", list(FIELDS))
async def test_proposals_then_every_write_has_internal_audit_note(odoo, name):
    tools, server, store = odoo
    action = make_write(tools, name)
    assert isinstance(action, ProposedAction)
    assert not server.requests
    result = await tools.apply(action, approval_id=approve(store, action))
    assert result["dry_run"] is False
    model, method, body, request = server.requests[-1]
    assert model == action.target and method == "message_post"
    assert body["subtype_xmlid"] == "mail.mt_note"
    assert body["message_type"] == "comment"
    assert "[Agent] Deneme gerekçesi <safe> (run run-1)" in body["body"]
    assert request.headers["authorization"] == "bearer private-odoo-key"
    assert request.headers["X-Odoo-Database"] == "katip_agent_test"
    if name.startswith("create_"):
        created = next(row[2]["vals_list"][0] for row in server.requests if row[1] == "create")
        assert created["tag_ids"][0][:2] == [6, 0]
        assert 1 in created["tag_ids"][0][2]
        assert "source" not in created
    if name == "add_note":
        assert "İç not" in body["body"]
        assert len(server.requests) == 1
    if name == "update_task":
        write = next(row[2] for row in server.requests if row[1] == "write")
        assert write["vals"]["description"] == "<p>Existing</p>\n<p>Added</p>"


@pytest.mark.parametrize("name", list(FIELDS))
async def test_unknown_fields_and_edited_payloads_rejected_before_requests(odoo, name):
    tools, server, store = odoo
    action = make_write(tools, name)
    edited = json.loads(json.dumps(action.payload))
    edited["values"]["forbidden_field"] = "bad"
    identifier = store.create(action, "task-1", "run-1")
    changed = store.decision(identifier, "approved", "UOWNER", edited)
    with pytest.raises(OdooValidationError, match="izin"):
        await tools.apply(changed.action, approval_id=identifier)
    assert not server.requests


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "A", "user_id": 1},
        {"name": "A", "tag_ids": [[6, 0, [1]]]},
        {"name": "A", "tag_ids": [1]},
        {"name": "A", "tag_ids": ["arbitrary"]},
        {"name": ""},
        {"name": True},
        {"name": "A", "description": {"x": "y"}},
    ],
)
async def test_create_field_validation(odoo, fields):
    tools, server, _ = odoo
    with pytest.raises(OdooValidationError):
        tools.create_lead(reason="reason", run_id="run", **fields)
    assert not server.requests


@pytest.mark.parametrize(
    "name,values",
    [
        ("move_lead_stage", {"stage_id": True}),
        ("update_task", {"name": "rename"}),
        ("update_task", {"date_deadline": "tomorrow"}),
        ("update_task", {"date_deadline": False}),
        ("log_activity", {"activity_type": "send-email"}),
        ("add_note", {"body": ""}),
    ],
)
async def test_other_field_validation(odoo, name, values):
    tools, _, store = odoo
    action = make_write(tools, name)
    action.payload["values"].update(values)
    with pytest.raises(OdooValidationError):
        await tools.apply(action, approval_id=approve(store, action))


@pytest.mark.parametrize("reason,run_id", [("", "run"), ("reason", ""), (None, "run")])
async def test_every_write_requires_audit_metadata(odoo, reason, run_id):
    tools, _, _ = odoo
    with pytest.raises(OdooValidationError):
        tools.create_lead(name="A", reason=reason, run_id=run_id)


@pytest.mark.parametrize("method", ["unlink", "execute_kw", "sudo", "search", "copy"])
async def test_no_generic_rpc_or_unlink(odoo, method):
    tools, server, _ = odoo
    with pytest.raises(OdooAccessError):
        await tools.client._call("crm.lead", method, {})
    assert not hasattr(tools, method)
    assert not server.requests


@pytest.mark.parametrize(
    "model", ["res.users", "res.partner", "ir.config_parameter", "account.move"]
)
async def test_forbidden_models(odoo, model):
    tools, server, _ = odoo
    with pytest.raises(OdooAccessError):
        await tools.client._call(model, "create", {})
    with pytest.raises(OdooValidationError):
        tools.add_note(model, 1, body="x", reason="r", run_id="run")
    assert not server.requests


async def test_action_cannot_change_target_kind_reason_or_envelope(odoo):
    tools, server, _ = odoo
    original = make_write(tools, "update_task")
    for change in (
        {"target": "crm.lead"},
        {"kind": "odoo.unlink"},
        {"reason": "changed"},
        {"payload": {**original.payload, "method": "unlink"}},
    ):
        with pytest.raises(OdooValidationError):
            await tools.apply(original.model_copy(update=change))
    assert not server.requests


@pytest.mark.parametrize("name", list(FIELDS))
async def test_dry_run_never_sends_or_reads_a_key(odoo, name, caplog):
    tools, server, store = odoo
    tools.settings = tools.settings.model_copy(update={"dry_run": True})
    tools.client.settings.key_file.unlink()
    action = make_write(tools, name)
    caplog.set_level(logging.DEBUG)
    result = await tools.apply(action, approval_id=approve(store, action))
    assert result["dry_run"]
    assert result["would_send"] == action.model_dump(mode="json")
    assert not server.requests
    assert "private-odoo-key" not in caplog.text


async def test_apply_rechecks_policy_and_approval_digest_expiry(odoo):
    tools, server, store = odoo
    action = make_write(tools, "create_lead")
    with pytest.raises(PermissionError):
        await tools.apply(action)
    identifier = approve(store, action)
    action.payload["values"]["name"] = "changed"
    with pytest.raises(PermissionError):
        await tools.apply(action, approval_id=identifier)
    action = store.get(identifier).action
    store.clock = lambda: store.get(identifier).expires_at
    with pytest.raises(PermissionError):
        await tools.apply(action, approval_id=identifier)
    tools.settings = tools.settings.model_copy(update={"policy": PolicySettings()})
    with pytest.raises(PolicyRejection):
        await tools.apply(action, approval_id=identifier)
    assert not server.requests


async def test_ensure_tags_is_explicit_and_idempotent(odoo):
    tools, server, _ = odoo
    existing = {"crm.tag": {}, "project.tags": {}}

    def reply(model, method, body):
        if method == "search_read":
            return httpx.Response(
                200, json=[{"id": id, "name": name} for name, id in existing[model].items()]
            )
        assert model in existing and method == "create"
        name = body["vals_list"][0]["name"]
        id = len(existing[model]) + 1
        existing[model][name] = id
        return httpx.Response(200, json=[id])

    server.reply = reply
    result = await tools.ensure_tags(reason="Bootstrap", run_id="setup")
    assert len(result["crm.tag"]) == 7
    creates = [r for r in server.requests if r[1] == "create"]
    assert len(creates) == 14
    server.requests.clear()
    await tools.ensure_tags(reason="Bootstrap", run_id="setup-2")
    assert all(r[1] == "search_read" for r in server.requests)


async def test_missing_tags_never_created_by_apply(odoo):
    tools, server, store = odoo
    server.reply = lambda *_: httpx.Response(200, json=[])
    action = make_write(tools, "create_lead")
    with pytest.raises(OdooValidationError, match="ensure_tags"):
        await tools.apply(action, approval_id=approve(store, action))
    assert all(r[1] == "search_read" for r in server.requests)
    tools.settings = tools.settings.model_copy(update={"dry_run": True})
    server.requests.clear()
    assert (await tools.ensure_tags(reason="r", run_id="run"))["dry_run"]
    assert not server.requests


@pytest.mark.parametrize(
    "status,name,error",
    [
        (403, "odoo.exceptions.AccessError", OdooAccessError),
        (401, "", OdooAuthenticationError),
        (404, "", OdooNotFoundError),
        (400, "odoo.exceptions.ValidationError", OdooValidationError),
        (500, "odoo.exceptions.AccessError", OdooAccessError),
        (500, "odoo.exceptions.MissingError", OdooNotFoundError),
        (500, "odoo.exceptions.UserError", OdooValidationError),
        (500, "", OdooError),
        (429, "", OdooError),
        (302, "", OdooError),
    ],
)
async def test_error_mapping_and_no_key_in_logs(odoo, status, name, error, caplog):
    tools, server, _ = odoo
    caplog.set_level(logging.DEBUG)
    server.reply = lambda *_: httpx.Response(
        status, json={"name": name, "message": "private-odoo-key", "debug": "private-odoo-key"}
    )
    with pytest.raises(error) as caught:
        await tools.list_leads()
    assert "private-odoo-key" not in str(caught.value) + caplog.text
    assert len(server.requests) == 1


async def test_wrapped_error_invalid_json_and_timeout(odoo):
    tools, server, _ = odoo
    server.reply = lambda *_: httpx.Response(
        200, json={"error": {"data": {"name": "odoo.exceptions.AccessError"}}}
    )
    with pytest.raises(OdooAccessError):
        await tools.list_leads()
    server.reply = lambda *_: httpx.Response(200, content=b"not json")
    with pytest.raises(OdooError):
        await tools.list_leads()

    def timeout(*_):
        raise httpx.ReadTimeout("private-odoo-key")

    server.reply = timeout
    with pytest.raises(OdooConnectionError) as caught:
        await tools.list_leads()
    assert "private-odoo-key" not in str(caught.value)


async def test_note_failure_is_explicit_partial_application(odoo):
    tools, server, store = odoo
    server.reply = lambda model, method, body: httpx.Response(
        403 if method == "message_post" else 200, json=True
    )
    action = make_write(tools, "move_lead_stage")
    with pytest.raises(OdooError, match="yeniden denemeden"):
        await tools.apply(action, approval_id=approve(store, action))
    assert [r[1] for r in server.requests] == ["write", "message_post"]


async def test_read_filters_and_chatter_limit(odoo):
    tools, server, _ = odoo
    await tools.list_leads(stage=3, tag="icp-erp", limit=12)
    assert server.requests[-1][2]["domain"] == [
        ["stage_id", "=", 3],
        ["tag_ids.name", "=", "icp-erp"],
    ]
    await tools.list_tasks(project_id=2, stage=4, assignee=5, limit=8)
    assert server.requests[-1][2]["domain"] == [
        ["project_id", "=", 2],
        ["stage_id", "=", 4],
        ["user_ids", "=", 5],
    ]

    def reply(model, method, body):
        if model == "crm.lead":
            return httpx.Response(
                200, json=[{"id": 9, "message_ids": list(range(1, 31)), "activity_ids": [2]}]
            )
        return httpx.Response(200, json=[{"id": id} for id in body["ids"]])

    server.reply = reply
    lead = await tools.get_lead(9)
    assert [r["id"] for r in lead["messages"]] == list(range(30, 20, -1))
    assert lead["activities"] == [{"id": 2}]
    assert all(r[1] in {"read", "search_read"} for r in server.requests)


async def test_read_summaries(odoo):
    tools, server, _ = odoo

    def reply(model, method, body):
        if model == "project.project":
            data = [{"id": 1, "name": "Project"}]
        elif model == "project.task":
            data = [{"project_id": [1, "Project"], "stage_id": [2, "New"]}] * 2
        elif model == "account.analytic.line":
            data = [{"project_id": [1, "Project"], "unit_amount": 2.5}] * 2
        elif "context" in body:
            assert body["context"]["active_test"] is False
            data = [{"id": 9}]
        elif "stage_id.is_won" in json.dumps(body["domain"]):
            data = [{"id": 7}, {"id": 8}]
        else:
            data = [{"stage_id": [2, "New"], "expected_revenue": 100}] * 2
        return httpx.Response(200, json=data)

    server.reply = reply
    assert (await tools.list_projects())[0]["task_counts_by_stage"] == {"New": 2}
    assert await tools.timesheet_summary("2026-10-01", "2026-10-31") == {"Project": 5}
    result = await tools.pipeline_summary()
    assert result == {
        "stages": {"New": {"count": 2, "expected_revenue": 200}},
        "won_this_week": 2,
        "lost_this_week": 1,
    }
    with pytest.raises(OdooValidationError):
        await tools.timesheet_summary("2026-11-01", "2026-10-01")


@pytest.mark.parametrize("limit", [0, -1, 501, True])
async def test_bounded_reads(odoo, limit):
    tools, server, _ = odoo
    with pytest.raises(OdooValidationError):
        await tools.list_leads(limit=limit)
    assert not server.requests


async def test_phase_two_timesheets_are_not_implemented(odoo):
    tools, server, _ = odoo
    with pytest.raises(NotImplementedError):
        tools.propose_timesheet(reason="r", run_id="run", name="x")
    assert not server.requests

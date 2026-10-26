"""Typed, scoped tools. All business writes are proposals until explicitly applied.

JSON-2 calls are separate transactions: a successful write followed by a failed
note is a partial application and must be reconciled, never retried blindly.
"""

import html
import logging
from datetime import UTC, date, datetime, timedelta
from typing import cast

from pydantic import JsonValue

from katip_ops.config import Settings
from katip_ops.core import policy
from katip_ops.core.approvals import Approvals
from katip_ops.core.contract import ProposedAction
from katip_ops.tools.odoo.client import OdooClient, OdooError, OdooValidationError

logger = logging.getLogger(__name__)
TAGS = (
    "agent-created",
    "icp-erp",
    "icp-hr",
    "icp-ai",
    "source-linkedin",
    "source-inbound",
    "source-research",
)
FIELDS = {
    "create_lead": {
        "name",
        "partner_name",
        "website",
        "contact_name",
        "email_from",
        "function",
        "description",
        "tag_ids",
        "source",
    },
    "move_lead_stage": {"stage_id"},
    "log_activity": {"activity_type", "summary", "note", "date_deadline"},
    "add_note": {"body"},
    "create_task": {"project_id", "name", "description", "date_deadline", "tag_ids"},
    "update_task": {"stage_id", "date_deadline", "description"},
}
REQUIRED = {
    "create_lead": {"name"},
    "move_lead_stage": {"stage_id"},
    "log_activity": {"activity_type", "summary", "date_deadline"},
    "add_note": {"body"},
    "create_task": {"project_id", "name"},
    "update_task": set(),
}
TARGETS = {
    "create_lead": {"crm.lead"},
    "move_lead_stage": {"crm.lead"},
    "log_activity": {"crm.lead", "project.task"},
    "add_note": {"crm.lead", "project.task"},
    "create_task": {"project.task"},
    "update_task": {"project.task"},
}
LEAD_FIELDS = ["id", "name", "partner_id", "stage_id", "expected_revenue", "tag_ids", "write_date"]
TASK_FIELDS = ["id", "name", "project_id", "stage_id", "date_deadline", "tag_ids"]


def _positive_id(value: JsonValue) -> int:
    if type(value) is not int or value <= 0:
        raise OdooValidationError("Geçerli bir kayıt kimliği gerekli.")
    return value


def _text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OdooValidationError("Boş olmayan bir metin gerekli.")
    return value


def _rows(value: JsonValue) -> list[dict[str, JsonValue]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise OdooError("Odoo yanıtının biçimi geçersiz.")
    return cast(list[dict[str, JsonValue]], value)


def _relation(value: JsonValue) -> str:
    if isinstance(value, list) and len(value) == 2:
        return str(value[1])
    return str(value)


class OdooTools:
    def __init__(
        self, client: OdooClient, settings: Settings, approvals: Approvals | None = None
    ) -> None:
        self.client = client
        self.settings = settings
        self.approvals = approvals

    async def _search(
        self, model: str, domain: list[JsonValue], fields: list[str], limit: int | None = None
    ) -> list[dict[str, JsonValue]]:
        body: dict[str, JsonValue] = {"domain": domain, "fields": list(fields)}
        if limit is not None:
            if type(limit) is not int or not 1 <= limit <= 500:
                raise OdooValidationError("Kayıt sınırı 1–500 arasında olmalı.")
            body["limit"] = limit
        return _rows(await self.client._call(model, "search_read", body))

    async def _read(
        self, model: str, ids: list[int], fields: list[str]
    ) -> list[dict[str, JsonValue]]:
        return _rows(
            await self.client._call(model, "read", {"ids": list(ids), "fields": list(fields)})
        )

    async def list_leads(
        self, stage: int | None = None, tag: str | None = None, limit: int = 100
    ) -> list[dict[str, JsonValue]]:
        domain: list[JsonValue] = []
        if stage is not None:
            domain.append(["stage_id", "=", _positive_id(stage)])
        if tag is not None:
            domain.append(["tag_ids.name", "=", _text(tag)])
        return await self._search("crm.lead", domain, LEAD_FIELDS, limit)

    async def get_lead(self, id: int) -> dict[str, JsonValue]:
        rows = await self._read(
            "crm.lead",
            [_positive_id(id)],
            [*LEAD_FIELDS, "description", "activity_ids", "message_ids"],
        )
        if not rows:
            raise OdooError("Odoo kaydı bulunamadı.")
        lead = rows[0]
        message_ids = lead.pop("message_ids", [])
        activity_ids = lead.pop("activity_ids", [])
        messages = (
            sorted((_positive_id(v) for v in message_ids), reverse=True)[:10]
            if isinstance(message_ids, list)
            else []
        )
        activities = (
            [_positive_id(v) for v in activity_ids] if isinstance(activity_ids, list) else []
        )
        lead["messages"] = (
            cast(
                JsonValue,
                await self._read("mail.message", messages, ["id", "body", "date", "author_id"]),
            )
            if messages
            else []
        )
        lead["activities"] = (
            cast(
                JsonValue,
                await self._read(
                    "mail.activity",
                    activities,
                    ["id", "activity_type_id", "summary", "note", "date_deadline"],
                ),
            )
            if activities
            else []
        )
        return lead

    async def lead_identities(self) -> list[dict[str, JsonValue]]:
        """Complete, minimal identity projection for code-only deduplication."""
        return _rows(
            await self.client._call(
                "crm.lead",
                "search_read",
                {
                    "domain": [],
                    "fields": ["id", "name", "partner_name", "website"],
                    "context": {"active_test": False},
                },
            )
        )

    async def list_projects(self) -> list[dict[str, JsonValue]]:
        projects = await self._search("project.project", [], ["id", "name"])
        tasks = await self._search("project.task", [], ["project_id", "stage_id"])
        for project in projects:
            counts: dict[str, JsonValue] = {}
            for task in tasks:
                relation = task.get("project_id")
                if isinstance(relation, list) and relation[0] == project["id"]:
                    stage = _relation(task.get("stage_id"))
                    counts[stage] = int(cast(int, counts.get(stage, 0))) + 1
            project["task_counts_by_stage"] = counts
        return projects

    async def list_tasks(
        self,
        project_id: int | None = None,
        stage: int | None = None,
        assignee: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, JsonValue]]:
        domain: list[JsonValue] = []
        for field, value in (
            ("project_id", project_id),
            ("stage_id", stage),
            ("user_ids", assignee),
        ):
            if value is not None:
                domain.append([field, "=", _positive_id(value)])
        return await self._search("project.task", domain, TASK_FIELDS, limit)

    async def timesheet_summary(self, date_from: str, date_to: str) -> dict[str, JsonValue]:
        if date.fromisoformat(date_from) > date.fromisoformat(date_to):
            raise OdooValidationError("Tarih aralığı geçersiz.")
        rows = await self._search(
            "account.analytic.line",
            [["date", ">=", date_from], ["date", "<=", date_to], ["project_id", "!=", False]],
            ["project_id", "unit_amount"],
        )
        totals: dict[str, JsonValue] = {}
        for row in rows:
            project = _relation(row["project_id"])
            totals[project] = float(cast(float, totals.get(project, 0))) + float(
                cast(float, row["unit_amount"])
            )
        return totals

    async def pipeline_summary(self) -> dict[str, JsonValue]:
        rows = await self._search("crm.lead", [], ["stage_id", "expected_revenue"])
        stages: dict[str, JsonValue] = {}
        for row in rows:
            stage = _relation(row["stage_id"])
            bucket = cast(
                dict[str, JsonValue], stages.setdefault(stage, {"count": 0, "expected_revenue": 0})
            )
            bucket["count"] = cast(int, bucket["count"]) + 1
            bucket["expected_revenue"] = cast(float, bucket["expected_revenue"]) + float(
                cast(float, row["expected_revenue"])
            )
        now = datetime.now(UTC)
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        dates: list[JsonValue] = [
            ["date_closed", ">=", monday.strftime("%Y-%m-%d %H:%M:%S")],
            ["date_closed", "<=", now.strftime("%Y-%m-%d %H:%M:%S")],
        ]
        won = await self._search("crm.lead", [*dates, ["stage_id.is_won", "=", True]], ["id"])
        # Lost leads are archived; active_test=False is required in JSON-2 context.
        lost = _rows(
            await self.client._call(
                "crm.lead",
                "search_read",
                {
                    "domain": [*dates, ["active", "=", False], ["probability", "=", 0]],
                    "fields": ["id"],
                    "context": {"active_test": False},
                },
            )
        )
        return {"stages": stages, "won_this_week": len(won), "lost_this_week": len(lost)}

    def _validate(self, action: ProposedAction) -> tuple[str, dict[str, JsonValue], int | None]:
        name = action.kind.removeprefix("odoo.")
        if (
            action.kind != f"odoo.{name}"
            or name not in FIELDS
            or action.target not in TARGETS[name]
        ):
            raise OdooValidationError("Bu Odoo aracına veya modeline izin verilmiyor.")
        payload = action.payload
        create = name in {"create_lead", "create_task"}
        expected = {"values", "reason", "run_id"} | (set() if create else {"record_id"})
        if set(payload) != expected:
            raise OdooValidationError("İşlem alanları izin listesiyle eşleşmiyor.")
        _text(payload["reason"])
        _text(payload["run_id"])
        if payload["reason"] != action.reason:
            raise OdooValidationError("İşlem gerekçesi onayla eşleşmiyor.")
        values = payload["values"]
        if (
            not isinstance(values, dict)
            or not values
            or set(values) - FIELDS[name]
            or REQUIRED[name] - set(values)
        ):
            raise OdooValidationError("Alanlar izin listesiyle eşleşmiyor veya zorunlu alan eksik.")
        for field, value in values.items():
            if field in {"project_id", "stage_id"}:
                _positive_id(value)
            elif field == "tag_ids":
                if not isinstance(value, list) or any(
                    not isinstance(v, str) or v not in TAGS for v in value
                ):
                    raise OdooValidationError("Etiketler izin verilen adlardan seçilmeli.")
            elif field == "activity_type":
                if value not in ("call", "meeting", "to-do"):
                    raise OdooValidationError("Etkinlik türü geçersiz.")
            elif field == "date_deadline":
                try:
                    date.fromisoformat(_text(value))
                except ValueError:
                    raise OdooValidationError("Son tarih geçersiz.") from None
            else:
                _text(value)
        record_id = None if create else _positive_id(payload["record_id"])
        return name, dict(values), record_id

    def _propose(
        self,
        name: str,
        target: str,
        values: dict[str, JsonValue],
        reason: str,
        run_id: str,
        record_id: int | None = None,
    ) -> ProposedAction:
        _text(reason)
        _text(run_id)
        payload: dict[str, JsonValue] = {"values": dict(values), "reason": reason, "run_id": run_id}
        if record_id is not None:
            payload["record_id"] = record_id
        action = ProposedAction(
            kind=f"odoo.{name}",
            target=target,
            payload=payload,
            reason=reason,
            requires_approval=False,
        )
        self._validate(action)
        required = policy.check_action(
            action, self.settings.policy.allowed_kinds, self.settings.policy.approval_rules
        )
        return action.model_copy(update={"requires_approval": required})

    def create_lead(self, *, reason: str, run_id: str, **values: JsonValue) -> ProposedAction:
        return self._propose("create_lead", "crm.lead", values, reason, run_id)

    def move_lead_stage(
        self, id: int, *, reason: str, run_id: str, **values: JsonValue
    ) -> ProposedAction:
        return self._propose("move_lead_stage", "crm.lead", values, reason, run_id, id)

    def log_activity(
        self, model: str, id: int, *, reason: str, run_id: str, **values: JsonValue
    ) -> ProposedAction:
        return self._propose("log_activity", model, values, reason, run_id, id)

    def add_note(
        self, model: str, id: int, *, reason: str, run_id: str, **values: JsonValue
    ) -> ProposedAction:
        return self._propose("add_note", model, values, reason, run_id, id)

    def create_task(self, *, reason: str, run_id: str, **values: JsonValue) -> ProposedAction:
        return self._propose("create_task", "project.task", values, reason, run_id)

    def update_task(
        self, id: int, *, reason: str, run_id: str, **values: JsonValue
    ) -> ProposedAction:
        return self._propose("update_task", "project.task", values, reason, run_id, id)

    def propose_timesheet(self, *, reason: str, run_id: str, **values: JsonValue) -> ProposedAction:
        raise NotImplementedError("Zaman çizelgesi önerileri Faz 2 kapsamındadır.")

    async def _tag_ids(self, model: str, names: list[str]) -> list[int]:
        rows = await self._search(model, [["name", "in", list(names)]], ["id", "name"])
        mapping = {str(row["name"]): _positive_id(row["id"]) for row in rows}
        if set(names) - mapping.keys():
            raise OdooValidationError("Gerekli etiket eksik; önce ensure_tags çalıştırılmalı.")
        return [mapping[name] for name in names]

    async def ensure_tags(self, *, reason: str, run_id: str) -> dict[str, JsonValue]:
        """Explicit bootstrap only; tag models have no chatter. Never run from a job."""
        _text(reason)
        _text(run_id)
        if self.settings.dry_run:
            logger.info("odoo_tag_bootstrap_dry_run")
            return {"dry_run": True, "tags": list(TAGS)}
        result: dict[str, JsonValue] = {}
        for model in ("crm.tag", "project.tags"):
            existing = await self._search(model, [["name", "in", list(TAGS)]], ["id", "name"])
            mapping = {str(row["name"]): _positive_id(row["id"]) for row in existing}
            for tag in TAGS:
                if tag not in mapping:
                    created = await self.client._call(
                        model, "create", {"vals_list": [{"name": tag}]}
                    )
                    if isinstance(created, list) and len(created) == 1:
                        created = created[0]
                    mapping[tag] = _positive_id(created)
            result[model] = dict(mapping)
        logger.info("odoo_tag_bootstrap_completed")
        return result

    async def apply(
        self, action: ProposedAction, *, approval_id: str | None = None
    ) -> dict[str, JsonValue]:
        # Snapshot nested dictionaries before the first await, then recheck all boundaries.
        action = ProposedAction.model_validate_json(action.model_dump_json())
        name, values, record_id = self._validate(action)
        required = policy.check_action(
            action, self.settings.policy.allowed_kinds, self.settings.policy.approval_rules
        )
        if required or approval_id is not None:
            if self.approvals is None or approval_id is None:
                raise PermissionError("Bu işlem için geçerli bir onay gerekli.")
            approval = self.approvals.get(approval_id)
            if (
                approval.status != "approved"
                or approval.digest != action.digest
                or self.approvals.clock() >= approval.expires_at
            ):
                raise PermissionError("İşlem onayla eşleşmiyor veya onay geçersiz.")
        if self.settings.dry_run:
            logger.info("odoo_apply_dry_run", extra={"kind": action.kind, "digest": action.digest})
            return {"dry_run": True, "would_send": action.model_dump(mode="json")}
        audit = f"[Agent] {action.payload['reason']} (run {action.payload['run_id']})"
        note = audit
        if name in {"create_lead", "create_task"}:
            tags = cast(list[str], values.pop("tag_ids", []))
            ids = await self._tag_ids(
                "crm.tag" if name == "create_lead" else "project.tags",
                list(dict.fromkeys([*tags, "agent-created"])),
            )
            values["tag_ids"] = [[6, 0, list(ids)]]
            # 'source' is a tool-level provenance string, not an Odoo crm.lead field.
            source = values.pop("source", None)
            if source is not None:
                values["description"] = (
                    str(values.get("description", ""))
                    + f"\n<p>Source: {html.escape(str(source))}</p>"
                )
            created = await self.client._call(action.target, "create", {"vals_list": [values]})
            if isinstance(created, list) and len(created) == 1:
                created = created[0]
            record_id = _positive_id(created)
        elif name == "add_note":
            note = f"{values['body']}\n\n{audit}"
        elif name == "log_activity":
            activity = {
                "call": "mail.mail_activity_data_call",
                "meeting": "mail.mail_activity_data_meeting",
                "to-do": "mail.mail_activity_data_todo",
            }[str(values.pop("activity_type"))]
            await self.client._call(
                action.target,
                "activity_schedule",
                {"ids": [record_id], "act_type_xmlid": activity, **values},
            )
        else:
            if name == "update_task" and "description" in values:
                rows = await self._read("project.task", [cast(int, record_id)], ["description"])
                if not rows:
                    raise OdooError("Odoo kaydı bulunamadı.")
                previous = rows[0].get("description") or ""
                values["description"] = f"{previous}\n{values['description']}"
            await self.client._call(action.target, "write", {"ids": [record_id], "vals": values})
        try:
            await self.client._call(
                action.target,
                "message_post",
                {
                    "ids": [record_id],
                    # A JSON string is plain text; mail.thread escapes it. Do not
                    # double-escape it or request trusted HTML via body_is_html.
                    "body": note,
                    "message_type": "comment",
                    "subtype_xmlid": "mail.mt_note",
                },
            )
        except OdooError:
            raise OdooError(
                "Kayıt işlemi yapıldı ancak denetim notu eklenemedi; "
                "yeniden denemeden kontrol edin."
            ) from None
        return {"dry_run": False, "record_id": record_id, "kind": action.kind}

"""Job orchestration, provider audit, bounded read tools and approval-only writes."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from katip_ops.config import Config
from katip_ops.core import policy
from katip_ops.core.approvals import Approvals
from katip_ops.core.contract import Cost, DataClass, ProposedAction, Status, Task, TaskResult
from katip_ops.core.ledger import Ledger
from katip_ops.core.router import NoEligibleExecutor, Router
from katip_ops.executors.base import ExecutorResponse, Message, Tool
from katip_ops.runtime_files import JOB_ALIASES, SOPS, Documents, WorkFiles
from katip_ops.slack import blocks

logger = logging.getLogger(__name__)
# Explicit grants, never discovered from methods on an injected service.
JOB_TOOL_GRANTS = {
    "icerik": set(),
    "pipeline": set(),
    "geo": set(),
    "cuma": {"odoo.pipeline_summary", "odoo.list_tasks"},
}


@dataclass(frozen=True)
class ReadTool:
    schema: Tool
    handler: Callable[..., Awaitable[JsonValue]]


def total_cost(costs: list[Cost]) -> Cost:
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "web_search_requests",
    )
    values = {name: sum(getattr(cost, name) for cost in costs) for name in fields}
    return Cost(
        **values,
        usd=sum(cost.usd for cost in costs if cost.usd is not None)
        if all(cost.usd is not None for cost in costs)
        else None,
    )


@dataclass
class JobContext:
    runner: "Runner"
    job: str
    task: Task
    run_id: str
    costs: list[Cost] = field(default_factory=list)
    actions: list[ProposedAction] = field(default_factory=list)
    rejected: bool = False

    @property
    def week(self) -> str:
        return self.runner.now().strftime("%G-W%V")

    async def generate(
        self,
        task_type: str,
        data_class: DataClass,
        evidence: str,
        instruction: str,
        *,
        allow_web: bool = False,
        measurement: bool = False,
        executor_name: str | None = None,
        tools: dict[str, ReadTool] | None = None,
        trusted_input: bool = False,
    ) -> ExecutorResponse:
        return await self.runner.generate(
            self,
            task_type,
            data_class,
            evidence,
            instruction,
            allow_web=allow_web,
            measurement=measurement,
            executor_name=executor_name,
            tools=tools or {},
            trusted_input=trusted_input,
        )

    def check_draft(self, text: str, *, claims_required: bool = True) -> list[str]:
        settings = self.runner.config.settings.policy
        claim_report = policy.check_claims(text, self.runner.documents.read("claims.md"))
        problems = list(policy.check_pii(text, settings.personal_email_domains))
        if policy.check_banned_terms(text, [*settings.banned_terms, *settings.customer_names]):
            problems.append("Yasaklı ifade veya müşteri adı içeriyor.")
        if claims_required and not claim_report.allowed:
            problems.append("NEEDS-CLAIM: İddia kaynağı eksik veya kullanıma kapalı.")
        if problems:
            self.rejected = True
        return problems

    async def propose(
        self,
        action: ProposedAction,
        *,
        title: str,
        body: str,
        personal: bool = False,
        opinion: bool = False,
    ) -> str:
        settings = self.runner.config.settings
        policy.check_action(action, settings.policy.allowed_kinds, settings.policy.approval_rules)
        blocks.approval_card("validation", title, body, action.reason, opinion_check=opinion)
        # These workflows always ask the owner, even when Stage 2 allows routine writes.
        action = action.model_copy(update={"requires_approval": True})
        approval_id = self.runner.approvals.create(action, self.task.id, self.run_id)
        self.actions.append(action)
        await self.runner.slack.send_approval(
            approval_id,
            title=title,
            body=body,
            opinion_check=opinion,
            channel_key="social" if personal else "ops_approvals",
        )
        return approval_id

    async def report(self, title: str, sections: list[str], channel: str = "ops") -> None:
        # Reports contain measured numbers, not public brand assertions. Never expose PII.
        safe = [
            section
            if not self.check_draft(section, claims_required=False)
            else "Metin politika denetiminde tutuldu; kaynağı kontrol edin."
            for section in sections
        ]
        await self.runner.slack.post(
            self.runner.config.settings.slack.channels[channel],  # type: ignore[index]
            blocks.report_message(title, safe, total_cost(self.costs).usd),
        )


class Runner:
    def __init__(
        self,
        config: Config,
        router: Router,
        ledger: Ledger,
        approvals: Approvals,
        documents: Documents,
        odoo: Any,
        github: Any,
        *,
        slack: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config, self.router, self.ledger, self.approvals = config, router, ledger, approvals
        self.documents, self.odoo, self.github, self.slack = documents, odoo, github, slack
        self.files = WorkFiles(config.settings.ops_dir)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._queue: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue(maxsize=100)
        self._lock = asyncio.Lock()

    def now(self) -> datetime:
        return self.clock().astimezone(ZoneInfo("Europe/Istanbul"))

    async def enqueue(self, job: str, *, requested_by: str) -> str:
        if job not in SOPS:
            raise ValueError("Bilinmeyen iş.")
        if requested_by != self.config.settings.owner_slack_id:
            raise PermissionError("Bu işi yalnızca yetkili kullanıcı başlatabilir.")
        run_id = str(uuid4())
        try:
            self._queue.put_nowait((job, requested_by, run_id))
        except asyncio.QueueFull:
            raise ValueError("İş kuyruğu dolu.") from None
        return run_id

    async def worker(self) -> None:
        while True:
            job, owner, run_id = await self._queue.get()
            try:
                await self.execute(job, requested_by=owner, source="slack", run_id=run_id)
            except Exception as exc:
                # Secrets never enter exception text (secrets.py), so the traceback is safe to log.
                logger.exception(
                    "job_failed",
                    extra={"job": job, "run_id": run_id, "error_type": type(exc).__name__},
                )
                await self._notify_failure(job, run_id, exc)
            finally:
                self._queue.task_done()

    async def _notify_failure(self, job: str, run_id: str, exc: Exception) -> None:
        if self.slack is None:
            return
        text = (
            f"İş başarısız: {job} (hata türü: {type(exc).__name__}, çalışma: {run_id[:8]}). "
            "Ayrıntı sunucu loglarında."
        )
        try:
            await self.slack.post(
                self.config.settings.slack.channels["ops"],
                {"text": text},
            )
        except Exception:
            logger.exception("job_failure_notice_failed", extra={"job": job, "run_id": run_id})

    async def execute(
        self,
        job: str,
        *,
        requested_by: str | None = None,
        source: Literal["schedule", "slack", "manual"] = "manual",
        run_id: str | None = None,
    ) -> TaskResult:
        from katip_ops.jobs import content, friday, growth, pipeline

        job = JOB_ALIASES.get(job, job)
        functions = {
            "icerik": content.run,
            "pipeline": pipeline.run,
            "geo": growth.run,
            "cuma": friday.run,
        }
        if job not in functions:
            raise ValueError("Bilinmeyen iş.")
        async with self._lock:
            data_classes: dict[str, DataClass] = {
                "pipeline": "public",
                "icerik": "personal",
                "geo": "public",
                "cuma": "customer",
            }
            task = Task(
                type=f"job.{job}",
                source=source,
                requested_by=requested_by or self.config.settings.owner_slack_id,
                sop=SOPS[job],
                data_class=data_classes[job],
            )
            context = JobContext(self, job, task, run_id or str(uuid4()))
            self.ledger.record_start(
                context.run_id, task, "orchestrator", self.documents.system(job)
            )
            try:
                output = await functions[job](context)
                status: Status = (
                    "rejected_by_policy"
                    if context.rejected
                    else "needs_approval"
                    if context.actions
                    else "done"
                )
                result = TaskResult(
                    task_id=task.id,
                    executor="orchestrator",
                    status=status,
                    output=output,
                    proposed_actions=context.actions,
                    cost=total_cost(context.costs),
                    run_id=context.run_id,
                )
            except BaseException:
                self.ledger.record_end(context.run_id, "failed", Cost(usd=0))
                raise
            # Provider calls have their own billed ledger rows; orchestration is free.
            self.ledger.record_end(context.run_id, result.status, Cost(usd=0))
            return result

    async def generate(
        self,
        context: JobContext,
        task_type: str,
        data_class: DataClass,
        evidence: str,
        instruction: str,
        *,
        allow_web: bool,
        measurement: bool,
        executor_name: str | None,
        tools: dict[str, ReadTool],
        trusted_input: bool,
    ) -> ExecutorResponse:
        if set(tools) - JOB_TOOL_GRANTS[context.job]:
            raise policy.PolicyRejection("Bu iş için araç izni yok.")
        if any(tool.schema.get("name") != name for name, tool in tools.items()):
            raise policy.PolicyRejection("Araç şeması işin izin listesiyle eşleşmiyor.")
        if tools and (not trusted_input or allow_web):
            raise policy.PolicyRejection("Güvenilmeyen içerikle istemci aracı birleştirilemez.")
        if tools and data_class != "customer":
            raise policy.PolicyRejection("Odoo verisi müşteri sınıfında işlenmeli.")
        task = Task(
            type=task_type,
            source=context.task.source,
            requested_by=context.task.requested_by,
            sop=context.task.sop,
            data_class=data_class,
            input={"allow_web": allow_web},
        )
        selection_router = self.router
        if executor_name is not None:
            if executor_name not in self.router.config.executors:
                raise NoEligibleExecutor(task)
            selection = self.router.config.routing.model_copy(
                update={"routes": {task.type: [executor_name]}}
            )
            selection_router = Router(
                self.router.config.model_copy(update={"routing": selection}), self.router.executors
            )
        candidates = selection_router.candidates(task)
        candidates = [
            e
            for e in candidates
            if (not allow_web or e.provider == "claude")
            and (executor_name is None or e.name == executor_name)
        ]
        if not candidates:
            raise NoEligibleExecutor(task)
        # Keep provider-native tool histories on one provider. Normal tool-less calls
        # retain ordered fallback; the tool phase is pinned to one executor.
        selected = candidates[:1] if tools else candidates
        routing = self.router.config.routing.model_copy(
            update={"routes": {task.type: [e.name for e in selected]}}
        )
        router = Router(
            self.router.config.model_copy(update={"routing": routing}), self.router.executors
        )
        system = (
            "Answer the user's question exactly as written. Use public sources only."
            if measurement
            else self.documents.system(context.job)
        )
        messages: list[Message] = [
            {
                "role": "user",
                "content": evidence
                if measurement or trusted_input
                else policy.wrap_untrusted(evidence, task_type),
            }
        ]
        if instruction:
            messages.append({"role": "user", "content": instruction})
        schemas: list[Tool] = [tool.schema for tool in tools.values()]
        if tools and selected[0].provider != "claude":
            schemas = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.schema["name"],
                        "description": tool.schema.get("description", ""),
                        "parameters": tool.schema["input_schema"],
                    },
                }
                for tool in tools.values()
            ]
        for round_number in range(self.config.settings.max_tool_rounds):
            call_id = str(uuid4())
            prompt = system + json.dumps(messages, ensure_ascii=False)
            self.ledger.record_start(call_id, task, selected[0].name, prompt)
            try:
                response = await router.run(task, system, messages, schemas)
            except BaseException:
                self.ledger.record_end(call_id, "failed", Cost())
                raise
            context.costs.append(response.cost)
            self.ledger.record_executor(call_id, response.executor or selected[0].name)
            denied = any(call.name not in tools for call in response.tool_calls) or bool(
                response.tool_calls and not schemas
            )
            self.ledger.record_end(
                call_id, "rejected_by_policy" if denied else response.status, response.cost
            )
            if response.status == "failed":
                raise ValueError("Yürütücü görevi tamamlayamadı.")
            if denied:
                raise policy.PolicyRejection("İzin verilmeyen araç çağrısı reddedildi.")
            if not response.tool_calls:
                return response
            if round_number + 1 >= self.config.settings.max_tool_rounds:
                raise policy.PolicyRejection("Araç turu sınırına ulaşıldı.")
            # All results are untrusted: next call is a fresh tool-less summary.
            results = []
            for call in response.tool_calls:
                value = await tools[call.name].handler(**call.arguments)
                results.append({"tool": call.name, "result": value})
            messages = [
                {
                    "role": "user",
                    "content": policy.wrap_untrusted(
                        json.dumps(results, ensure_ascii=False), "read-tool-results"
                    ),
                },
                {"role": "user", "content": instruction},
            ]
            schemas = []
        raise AssertionError("Configured round count must be positive")

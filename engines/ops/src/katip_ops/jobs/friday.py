"""Friday synthesis of internal summaries, without web or model tools."""

import json
from datetime import timedelta
from typing import Any

from pydantic import JsonValue

from katip_ops.runner import JobContext


async def run(context: JobContext) -> dict[str, JsonValue]:
    runner = context.runner
    now = runner.now()
    since = now - timedelta(days=7)
    rows = [
        row for row in runner.ledger.summaries(since.timestamp()) if row.run_id != context.run_id
    ]
    known_cost = sum(row.cost.usd for row in rows if row.cost and row.cost.usd is not None)
    unknown = any(row.cost is None or row.cost.usd is None for row in rows)
    cost = "bilinmiyor" if unknown else f"{known_cost:.4f} USD"
    pipeline = await runner.odoo.pipeline_summary()
    tasks = await runner.odoo.list_tasks()
    evidence: dict[str, Any] = {
        "period": {"from": since.isoformat(), "until": now.isoformat()},
        "ledger": [
            {
                "type": row.task_type,
                "status": row.status,
                "cost_usd": row.cost.usd if row.cost else None,
            }
            for row in rows
        ],
        "approvals": runner.approvals.summary(since.timestamp()),
        "pipeline": pipeline,
        "tasks": tasks,
        "content": {
            key: len(runner.files.read_tree("content/" + key))
            for key in ("drafts", "approved", "published")
        },
        "previous_reviews": dict(list(runner.files.read_tree("reviews").items())[-4:]),
        "weekly_cost": cost,
    }
    response = await context.generate(
        "review.friday",
        "customer",
        json.dumps(evidence, ensure_ascii=False),
        "Follow the Friday SOP. Turkish report text only. No web or tools. Missing data "
        "must say veri yok; do not invent time saved, deltas, revenue, approvals or costs. "
        "Maximum three decisions and three suggestions. Use the configured Opus review route.",
    )
    report = response.text
    if context.check_draft(report, claims_required=False):
        report = "Cuma raporu politika denetiminde tutuldu; kaynak kayıtları kontrol edin."
    # Include this synthesis call; unknown provider prices must stay unknown.
    current_unknown = any(item.usd is None for item in context.costs)
    total = known_cost + sum(item.usd or 0 for item in context.costs)
    cost_line = "bilinmiyor" if unknown or current_unknown else f"{total:.4f} USD"
    report += f"\n\nMaliyet (bu hafta): {cost_line}"
    runner.files.write(f"reviews/{context.week}.md", report)
    await context.report("Cuma değerlendirmesi", [report])
    return {"report": f"reviews/{context.week}.md", "cost": cost_line}

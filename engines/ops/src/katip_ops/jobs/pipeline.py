"""Public research followed by deterministic, customer-class CRM deduplication."""

import ipaddress
import json
import re
import unicodedata
from urllib.parse import urlsplit

from pydantic import Field, JsonValue

from katip_ops.core.contract import FrozenModel, Task
from katip_ops.jsonout import parse_model_json
from katip_ops.runner import JobContext


class Candidate(FrozenModel):
    name: str = Field(min_length=1, max_length=200)
    website: str
    brief: str = Field(min_length=1, max_length=2000)
    sources: list[str] = Field(min_length=1, max_length=10)
    scores: dict[str, int]
    score_evidence: dict[str, str]
    tag: str


class Research(FrozenModel):
    candidates: list[Candidate] = Field(max_length=30)


def criteria(markdown: str) -> dict[str, int]:
    result = {}
    for line in markdown.splitlines():
        cells = [cell.strip() for cell in line.strip("| ").split("|")]
        if len(cells) >= 3 and re.fullmatch(r"0\s*[–-]\s*\d+", cells[1]):
            result[cells[0]] = int(re.findall(r"\d+", cells[1])[-1])
    if not result or sum(result.values()) != 10:
        raise ValueError("ICP puanlama tablosu toplam on puan tanımlamalı.")
    return result


def normalized_name(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum()
    )


def domain(value: str) -> str:
    parsed = urlsplit(value if "://" in value else "https://" + value)
    return (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")


def public_url(value: str) -> bool:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    return bool(
        parsed.scheme == "https"
        and host
        and "." in host
        and not host.endswith((".local", ".internal", ".localhost"))
        and not parsed.username
        and not parsed.password
    )


async def run(context: JobContext) -> dict[str, JsonValue]:
    runner = context.runner
    rubric = criteria(runner.documents.read("ICP.md"))
    segments = re.findall(r"^\|\s*(S\d+)\s*\|", runner.documents.read("ICP.md"), re.MULTILINE)
    segment = segments[(runner.now().isocalendar().week - 1) % len(segments)] if segments else ""
    response = await context.generate(
        "pipeline.research",
        "public",
        "Research public companies for this week's ICP segment: " + segment,
        "Use only Claude server web search. No Odoo records or tools. Return JSON: "
        + json.dumps(Research.model_json_schema(), ensure_ascii=False)
        + " Use the exact ICP scoring criterion names. For every score provide a public URL "
        "in score_evidence. Briefs are Turkish, at most 120 words, include signal, fit, role "
        "and sources. No personal contacts. tag is icp-ai, icp-erp or icp-hr. "
        "Return fewer candidates and explain absent evidence rather than inventing companies.",
        allow_web=True,
    )
    research = parse_model_json(response.text, Research)
    scored = []
    for company in research.candidates:
        if (
            set(company.scores) != set(rubric)
            or set(company.score_evidence) != set(rubric)
            or not all(
                type(score) is int and 0 <= score <= rubric[key]
                for key, score in company.scores.items()
            )
            or not all(source in company.sources for source in company.score_evidence.values())
            or not all(public_url(source) for source in [company.website, *company.sources])
            or len(company.brief.split()) > 120
            or not normalized_name(company.name)
            or company.tag not in {"icp-ai", "icp-erp", "icp-hr"}
        ):
            continue
        score = sum(company.scores.values())
        if score >= 6:
            scored.append((score, company))
    # A separate code-only phase; customer records never enter the research context.
    crm_task = Task(
        type="pipeline.dedupe",
        source=context.task.source,
        requested_by=context.task.requested_by,
        data_class="customer",
    )
    from uuid import uuid4

    audit_id = str(uuid4())
    runner.ledger.record_start(audit_id, crm_task, "code", "CRM identity comparison only")
    from katip_ops.core.contract import Cost

    try:
        existing = await runner.odoo.lead_identities()
    except BaseException:
        runner.ledger.record_end(audit_id, "failed", Cost(usd=0))
        raise
    runner.ledger.record_end(audit_id, "done", Cost(usd=0))
    names = {
        normalized_name(str(row.get(key) or ""))
        for row in existing
        for key in ("name", "partner_name")
    }
    domains = {domain(str(row.get("website") or "")) for row in existing}
    kept = 0
    for score, company in sorted(scored, key=lambda item: (-item[0], item[1].name.casefold())):
        name, host = normalized_name(company.name), domain(company.website)
        if name in names or host in domains:
            continue
        text = company.name + "\n" + company.brief
        # Public-source company facts are not assertions about our brand. Claims
        # heuristic still runs; public brand drafts use its enforcing gate.
        if context.check_draft(text, claims_required=False):
            continue
        names.add(name)
        domains.add(host)
        reason = f"Pipeline araştırması {context.week}; SOP pipeline-research.md"
        action = runner.odoo.create_lead(
            name=company.name,
            partner_name=company.name,
            website=company.website,
            description=company.brief + "\nKaynaklar:\n" + "\n".join(company.sources),
            tag_ids=["agent-created", "source-research", company.tag],
            reason=reason,
            run_id=context.run_id,
        )
        await context.propose(
            action,
            title="Aday şirket",
            body=f"Şirket: {company.name}\nICP puanı: {score}/10\n"
            f"{' '.join(company.brief.split())}",
        )
        kept += 1
        if kept == 10:
            break
    summary = f"Araştırılan: {len(research.candidates)} · Onaya sunulan: {kept}"
    if kept < 10:
        summary += (
            "\nOn aday tamamlanamadı; kaynak, puan, politika ve "
            "mükerrer kayıt kontrolleri uygulandı."
        )
    runner.files.write(f"pipeline/{context.week}-{context.run_id}.md", summary)
    await context.report("Pipeline araştırması", [summary], channel="pipeline")
    return {"researched": len(research.candidates), "proposed": kept, "segment": segment}

"""Fresh GEO measurements; only code-scored data enters the summary call."""

import json
import re
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, JsonValue

from katip_ops.core.contract import FrozenModel, ProposedAction
from katip_ops.jsonout import parse_model_json
from katip_ops.runner import JobContext
from katip_ops.runtime_files import front_matter


class Suggestion(FrozenModel):
    text: str = Field(min_length=1, max_length=1800)
    questions: list[str] = Field(min_length=1)
    success_check: str = Field(min_length=1, max_length=400)


class Summary(FrozenModel):
    report: str = Field(min_length=1, max_length=12000)
    proposals: list[Suggestion] = Field(default_factory=list, max_length=5)


def questions(markdown: str, brand_terms: list[str]) -> tuple[list[dict[str, str]], int]:
    section = ""
    result = []
    skipped = 0
    for line in markdown.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        cells = [cell.strip() for cell in line.strip("| ").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"G-\d+", cells[0]):
            # The supplied set contains an explicitly separate brand-control group.
            # Baseline measurements must not prime answers with our brand.
            if int(cells[0][2:]) >= 50 or any(
                term.casefold() in cells[1].casefold() for term in brand_terms if term
            ):
                skipped += 1
            else:
                result.append({"id": cells[0], "segment": section, "question": cells[1]})
    return result, skipped


def contains_name(text: str, name: str) -> bool:
    return bool(name and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I))


def score_answer(
    text: str, names: list[str], owned: list[str], competitors: list[str]
) -> dict[str, Any]:
    urls = re.findall(r"https?://[^\s<>\]\[\)\(\"']+", text)
    domains = set()
    citations = set()
    for url in urls:
        try:
            host = (urlsplit(url).hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host):
            continue
        domains.add(host)
        if any(host == domain or host.endswith("." + domain) for domain in owned):
            citations.add(host)
    return {
        "mention": any(contains_name(text, name) for name in names),
        "citation": bool(citations),
        "citation_domains": sorted(citations),
        "competitors": [name for name in competitors if contains_name(text, name)],
        "domains": sorted(domains),
    }


def rates(table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in table:
        groups.setdefault((row["segment"], row["assistant"]), []).append(row)
    return [
        {
            "segment": segment,
            "assistant": assistant,
            "answers": len(rows),
            "mention_rate": sum(row["mention"] for row in rows) / len(rows),
            "citation_rate": sum(row["citation"] for row in rows) / len(rows),
            "top_domains": Counter(d for row in rows for d in row["domains"]).most_common(10),
        }
        for (segment, assistant), rows in groups.items()
    ]


async def run(context: JobContext) -> dict[str, JsonValue]:
    runner = context.runner
    geo = runner.config.settings.geo
    if not geo.mention_names or not geo.citation_domains:
        raise ValueError("GEO marka adları ve kaynak alan adları yapılandırılmalı.")
    question_set, skipped = questions(
        runner.documents.read("growth/geo-questions.md"), geo.mention_names
    )
    if not question_set:
        raise ValueError("Ölçülebilir GEO sorusu bulunamadı.")
    available = [
        name
        for name, entry in runner.config.executors.executors.items()
        if entry.enabled
        and "public" in entry.allowed_data_classes
        and name in runner.router.executors
        and entry.provider != "xai"
    ]
    if geo.assistants:
        assistants = [name for name in geo.assistants if name in available]
    else:
        seen: set[str] = set()
        assistants = []
        for name in available:
            provider = runner.config.executors.executors[name].provider
            if provider not in seen:
                assistants.append(name)
                seen.add(provider)
    if not assistants:
        raise ValueError("GEO için kullanılabilir asistan yok.")
    table: list[dict[str, Any]] = []
    for question in question_set:
        for name in assistants:
            entry = runner.config.executors.executors[name]
            response = await context.generate(
                f"growth.ask.{name}",
                "public",
                question["question"],
                "",
                measurement=True,
                executor_name=name,
                allow_web=entry.provider == "claude",
            )
            # Names never become filesystem paths: use deterministic numeric positions.
            number = len(table) + 1
            runner.files.write(
                f"growth/raw/{context.week}/{context.run_id}-{number:03d}.md",
                front_matter(
                    {
                        "assistant": name,
                        "model": response.model or entry.model,
                        "date": runner.now().isoformat(),
                        "question": question["id"],
                        "untrusted": True,
                        "public_quotation": False,
                        "sources": re.findall(r"https?://[^\s<>\]\[\)\(\"']+", response.text),
                    },
                    response.text,
                ),
            )
            table.append(
                {
                    "question": question["id"],
                    "segment": question["segment"],
                    "assistant": name,
                    **score_answer(
                        response.text, geo.mention_names, geo.citation_domains, geo.competitors
                    ),
                }
            )
    scored = {"scores": table, "rates": rates(table)}
    runner.files.write(
        f"growth/reports/{context.week}-{context.run_id}.json",
        json.dumps(scored, ensure_ascii=False, indent=2),
    )
    summary_call = await context.generate(
        "growth.summary",
        "public",
        json.dumps(scored, ensure_ascii=False),
        "Summarize only the scored table. No raw answer quotations or tools. Return JSON: "
        + json.dumps(Summary.model_json_schema(), ensure_ascii=False)
        + " At most five proposals; each names target question IDs and next week's success check. "
        "Missing comparisons must be marked veri yok.",
    )
    summary = parse_model_json(summary_call.text, Summary)
    ids = {question["id"] for question in question_set}
    for index, proposal in enumerate(summary.proposals, 1):
        if set(proposal.questions) - ids:
            continue
        text = proposal.text + "\nBaşarı kontrolü: " + proposal.success_check
        if context.check_draft(text):
            continue
        action = ProposedAction(
            kind="file.write",
            target=f"content/ideas/{context.week}-{context.run_id}-{index}.md",
            payload={"values": {"body": text}, "questions": list(proposal.questions)},
            reason="GEO ölçümünden türetilen içerik veya sayfa işi; ayrı karar gerekir.",
        )
        await context.propose(
            action,
            title="GEO önerisi",
            body=f"Öneri: {' '.join(proposal.text.split())}"
            f"\nSorular: {', '.join(proposal.questions)}"
            f"\nBaşarı kontrolü: {' '.join(proposal.success_check.split())}",
        )
    report = summary.report + f"\nMarka yönlendirmesi nedeniyle ölçülmeyen soru: {skipped}"
    if context.check_draft(report, claims_required=False):
        report = "Rapor politika denetiminde tutuldu; puan tablosunu kontrol edin."
    runner.files.write(f"growth/reports/{context.week}.md", report)
    await context.report("GEO raporu", [report])
    return {
        "answers": len(table),
        "skipped_brand_questions": skipped,
        "proposals": len(context.actions),
    }

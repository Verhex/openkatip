"""Weekly content: collect read-only evidence, gate drafts, save and card."""

import json
import re
from datetime import timedelta
from typing import Literal

from pydantic import Field, JsonValue

from katip_ops.core.contract import FrozenModel, ProposedAction
from katip_ops.jsonout import parse_model_json
from katip_ops.runner import JobContext
from katip_ops.runtime_files import front_matter


class Draft(FrozenModel):
    platform: Literal["x", "linkedin", "outline"]
    audience: Literal["personal", "company"]
    theme: str = Field(min_length=1, max_length=150)
    sources: list[str]
    evidenced: bool
    opinion: bool = False
    text: str = Field(default="", max_length=15000)
    thread: list[str] = Field(default_factory=list, max_length=6)


class Pack(FrozenModel):
    drafts: list[Draft] = Field(max_length=9)


def linkedin_limits(voice: str) -> tuple[int, int]:
    for line in voice.splitlines():
        if "linkedin" in line.casefold() and line.lstrip().startswith("|"):
            match = re.search(r"(\d+)\s*[–-]\s*(\d+)", line)
            if match:
                return int(match[1]), int(match[2])
    return 80, 200


def length_problems(draft: Draft, linkedin_range: tuple[int, int] = (80, 200)) -> list[str]:
    if draft.platform == "x":
        if draft.thread:
            if not 3 <= len(draft.thread) <= 6 or any(len(post) > 280 for post in draft.thread):
                return ["X zinciri 3–6 gönderi içermeli; her gönderi en fazla 280 karakter olmalı."]
        elif not draft.text.strip() or len(draft.text) > 280:
            return ["X taslağı en fazla 280 karakter olmalı."]
    elif draft.platform == "linkedin" and not (
        linkedin_range[0] <= len(draft.text.split()) <= linkedin_range[1]
    ):
        return [f"LinkedIn taslağı {linkedin_range[0]}–{linkedin_range[1]} kelime içermeli."]
    if draft.platform != "x" and draft.thread:
        return ["Bu platformda zincir kullanılamaz."]
    return []


# Bound evidence independently of the installation's repository activity volume.
_ITEM_CHARS = 1200
_TOTAL_CHARS = 60_000


def budget_evidence(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Owner ideas and decisions first, then PRs and commits; trimmed and deduplicated."""

    def rank(item: dict[str, str]) -> int:
        source = item["source"]
        if not source.startswith("http"):
            return 0  # content/ideas files and DECISIONS.md diff
        if "DECISIONS.md" in source:
            return 1
        if "/pull/" in source:
            return 2
        return 3

    seen: set[str] = set()
    result: list[dict[str, str]] = []
    total = 0
    for item in sorted(items, key=rank):
        text = item["text"].strip()
        key = text.splitlines()[0].casefold() if text else ""
        if not text or key in seen:
            continue
        seen.add(key)
        if len(text) > _ITEM_CHARS:
            text = text[:_ITEM_CHARS] + " …"
        if total + len(text) > _TOTAL_CHARS:
            break
        total += len(text)
        result.append({"source": item["source"], "text": text})
    return result


async def run(context: JobContext) -> dict[str, JsonValue]:
    runner = context.runner
    evidence = await runner.github.collect(
        runner.now() - timedelta(days=7), runner.config.settings.content_excluded_repos
    )
    evidence.extend(
        {"source": name, "text": text}
        for name, text in runner.files.read_tree("content/ideas").items()
    )
    decision_path = runner.documents.directory / "DECISIONS.md"
    decisions = runner.documents.read("DECISIONS.md") if decision_path.exists() else ""
    diff = runner.files.decisions_diff(decisions)
    if diff:
        evidence.append({"source": "DECISIONS.md", "text": diff})
    evidence = budget_evidence(evidence)
    response = await context.generate(
        "content.weekly",
        "personal",
        json.dumps(evidence, ensure_ascii=False),
        "Follow the weekly pack SOP and VOICE limits. Return JSON: "
        + json.dumps(Pack.model_json_schema(), ensure_ascii=False)
        + " Mark evidenced=false when opinion/experience is unsupported; leave text empty then. "
        "Sources must be exact source identifiers from the evidence. At most three themes. "
        "Do not quote assistant raw answers. No web or client tools.",
    )
    pack = parse_model_json(response.text, Pack)
    sources = {item["source"] for item in evidence}
    themes: set[str] = set()
    counts: dict[tuple[str, str], int] = {}
    limits = {
        ("x", "personal"): 5,
        ("linkedin", "personal"): 2,
        ("linkedin", "company"): 1,
        ("outline", "personal"): 1,
    }
    saved = 0
    thread_count = 0
    issues = []
    for index, draft in enumerate(pack.drafts, 1):
        if not draft.evidenced or not draft.sources or set(draft.sources) - sources:
            await context.report("Görüş gerekiyor", ["Bu konuda görüşün nedir?", draft.theme])
            continue
        themes.add(draft.theme)
        key = draft.platform, draft.audience
        counts[key] = counts.get(key, 0) + 1
        text = "\n\n".join(draft.thread) if draft.thread else draft.text
        problems = context.check_draft(text) + length_problems(
            draft, linkedin_limits(runner.documents.read("VOICE.md"))
        )
        if len(themes) > 3 or counts[key] > limits.get(key, 0):
            problems.append("Haftalık tema veya taslak sınırı aşıldı.")
        if draft.thread:
            thread_count += 1
            if thread_count > 1:
                problems.append("Haftada yalnızca bir X zinciri hazırlanabilir.")
        if draft.platform == "outline":
            month = runner.now().strftime("%Y-%m")
            history = runner.files.read_tree("content/drafts")
            if any(f"outline_month: {month}" in old for old in history.values()):
                problems.append("Bu ayın uzun yazı taslağı zaten hazırlandı.")
        if problems:
            context.rejected = True
            issues.extend(problems)
            continue
        claims = re.findall(r"C-\d{3,}", text)
        metadata: dict[str, object] = {
            "channel": draft.platform,
            "audience": draft.audience,
            "theme": draft.theme,
            "sources": draft.sources,
            "claims": claims,
            "run_id": context.run_id,
        }
        if draft.platform == "outline":
            metadata["outline_month"] = runner.now().strftime("%Y-%m")
        filename = f"{context.week}/{context.run_id}-{index:02d}.md"
        source = f"content/drafts/{filename}"
        runner.files.write(source, front_matter(metadata, text))
        action = ProposedAction(
            kind="file.write",
            target=f"content/approved/{filename}",
            payload={
                "values": {"body": text},
                "draft": source,
                "metadata": json.loads(json.dumps(metadata)),
                "platform": draft.platform,
                "thread": list(draft.thread),
            },
            reason="Haftalık içerik paketi; yayın öncesi kişisel kontrol gerekiyor.",
        )
        preview = " ".join(text.split())[:2200]
        await context.propose(
            action,
            title="İçerik taslağı",
            body=f"Kanal: {draft.platform}\nTema: {draft.theme}\nTaslak: {preview}",
            personal=draft.audience == "personal",
            opinion=draft.opinion,
        )
        saved += 1
    runner.files.write("content/.decisions-baseline.md", decisions)
    await context.report("İçerik paketi", [f"Onaya sunulan taslak: {saved}", *issues])
    return {"drafts": saved, "issues": list(issues)}

"""Bounded lexical retrieval; no embeddings or external services."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

STOP = frozenset(
    "katip bu su o bir ve veya ile icin mi mu mudur midir nedir nasil ne kadar "
    "bana bize lutfen soyle anlat acikla belge belgede belgeler dokuman dokumanda "
    "dosya dosyada sunum sunumda slayt slaytta gore hakkinda the what is are of in "
    "to and please according document documents does can".split()
)


def normalize(text: str) -> str:
    text = text.casefold().replace("ı", "i")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def tokens(text: str) -> list[str]:
    return [
        word
        for word in re.findall(r"[a-z0-9]+(?:[.,][0-9]+)?", normalize(text))
        if len(word) >= 2 and word not in STOP
    ]


def related(query: str, word: str) -> bool:
    if query == word:
        return True
    # Small suffix variation, not fuzzy spelling correction. Limit accidental matches
    # on short acronyms; product names and exact matches retain stronger weight.
    short, long = sorted((query, word), key=len)
    return len(short) >= 5 and len(long) - len(short) <= 4 and long.startswith(short)


def source_label(name: str, location: str) -> str:
    def clean(value: str) -> str:
        return re.sub(r"[\[\]\r\n]", " ", value).strip()

    return f"{clean(name)} — {clean(location)}"


@dataclass(frozen=True)
class Passage:
    source: str
    text: str
    start: int
    page_key: tuple[str, int]


@dataclass
class Evidence:
    text: str = ""
    sources: list[str] = field(default_factory=list)
    matched: bool = False


def passages(documents) -> list[Passage]:
    result = []
    for doc in documents:
        for index, page in enumerate(doc.pages):
            text = page["text"]
            for start in range(0, len(text), 1000):
                result.append(
                    Passage(
                        source_label(doc.name, page["location"]),
                        text[start : start + 1200],
                        start,
                        (doc.id, index),
                    )
                )
    return result


def retrieve(documents, query: str, budget: int) -> Evidence:
    if not documents:
        return Evidence()
    candidates = passages(documents)
    wanted = set(tokens(query))
    counts = [Counter(tokens(p.text)) for p in candidates]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(1, len(lengths)) or 1
    ranked = []
    frequencies = {
        term: sum(any(related(term, word) for word in count) for count in counts) for term in wanted
    }
    for index, passage in enumerate(candidates):
        score = 0.0
        for term in wanted:
            exact = counts[index].get(term, 0)
            suffix = sum(
                count
                for word, count in counts[index].items()
                if word != term and related(term, word)
            )
            frequency = exact + 0.5 * suffix
            if frequency:
                idf = math.log(
                    1 + (len(candidates) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
                )
                score += (
                    idf
                    * frequency
                    * 2.2
                    / (frequency + 1.2 * (0.25 + 0.75 * lengths[index] / average))
                )
        if score > 0:
            ranked.append((score, passage))
    ranked.sort(key=lambda item: -item[0])
    if not wanted:
        # Overview requests have no search terms: take bounded, diverse openings.
        ranked = [(0, p) for p in candidates if p.start == 0]
    header = (
        "Arama sonucu: seçili belge parçaları. Tam belgelerin tamamı bu bağlamda değildir.\n"
        if ranked
        else "Arama sonucu: bu isteğe uyan belge parçası bulunamadı. Tam belgeler tarama ile "
        "eşleştirilemedi; bu sonuç bilginin hiçbir belgede bulunmadığını kanıtlamaz.\n"
    )
    remaining = max(0, budget - len(header))
    blocks, sources = [], []
    per_page: Counter = Counter()
    for _, passage in ranked:
        if len(blocks) >= 12 or per_page[passage.page_key] >= 2:
            continue
        label = f"[{passage.source}]\n"
        if remaining < len(label) + 80:
            continue
        excerpt = passage.text[: remaining - len(label) - 2]
        blocks.append(label + excerpt)
        remaining -= len(label) + len(excerpt) + 2
        per_page[passage.page_key] += 1
        if passage.source not in sources:
            sources.append(passage.source)
    return Evidence((header + "\n\n".join(blocks))[:budget], sources, bool(blocks))

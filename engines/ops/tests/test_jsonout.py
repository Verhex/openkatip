import pytest
from pydantic import BaseModel

from katip_ops.jsonout import ModelOutputError, parse_model_json


class Item(BaseModel):
    name: str


@pytest.mark.parametrize(
    "text",
    [
        '{"name": "a"}',
        '```json\n{"name": "a"}\n```',
        'Haftalık paket aşağıda:\n```\n{"name": "a"}\n```\nİyi çalışmalar.',
        'Önce kısa bir not. {"name": "a"} Son.',
    ],
)
def test_parses_wrapped_json(text):
    assert parse_model_json(text, Item).name == "a"


def test_rejects_non_json():
    with pytest.raises(ModelOutputError):
        parse_model_json("JSON yok", Item)


def test_content_evidence_budget_keeps_ideas_and_trims():
    from katip_ops.jobs.content import _TOTAL_CHARS, budget_evidence

    items = [
        {"source": f"https://github.com/o/r/commit/{i}", "text": f"commit {i} " + "x" * 5000}
        for i in range(200)
    ]
    items.append({"source": "content/ideas/2026-10-26.md", "text": "Fikir"})
    items.append({"source": "https://github.com/o/r/commit/dup", "text": "commit 1 " + "x" * 5000})
    out = budget_evidence(items)
    assert out[0]["source"] == "content/ideas/2026-10-26.md"
    assert sum(len(i["text"]) for i in out) <= _TOTAL_CHARS
    assert all(len(i["text"]) <= 1202 for i in out)
    assert sum(1 for i in out if i["text"].startswith("commit 1 ")) == 1

"""Explicit offline dry-run services; never read keys or open connections."""

import json
from typing import Any

from katip_ops.core.contract import Task
from katip_ops.executors.base import ExecutorResponse, Message, Tool


class FakeExecutor:
    def __init__(self, name: str, provider: str, model: str) -> None:
        self.name, self.provider, self.model = name, provider, model

    async def run(
        self, task: Task, system: str, messages: list[Message], tools: list[Tool]
    ) -> ExecutorResponse:
        if task.type == "content.weekly":
            text = json.dumps(
                {
                    "drafts": [
                        {
                            "platform": "x",
                            "audience": "personal",
                            "theme": "Haftalık çalışma",
                            "sources": ["fake://commit"],
                            "evidenced": True,
                            "opinion": True,
                            "text": "Bu hafta küçük bir kararı yazıya döktüm.",
                        }
                    ]
                }
            )
        elif task.type == "pipeline.research":
            text = '{"candidates": []}'
        elif task.type == "growth.summary":
            text = '{"report": "Ölçüm tamamlandı. Önceki dönem için veri yok.", "proposals": []}'
        elif task.type.startswith("growth.ask."):
            text = "Çevrimdışı deneme yanıtı; canlı ölçüm için veri yok."
        else:
            text = (
                "Bu hafta deneme çalışmaları tamamlandı. Pipeline ve zaman dağılımı için veri yok."
            )
        return ExecutorResponse(text=text, model=self.model, executor=self.name)


class FakeGitHub:
    async def collect(self, since: Any, excluded: list[str]) -> list[dict[str, str]]:
        return [{"source": "fake://commit", "text": "A small decision was documented."}]

    async def close(self) -> None:
        pass


class FakeOdoo:
    async def lead_identities(self) -> list[dict[str, Any]]:
        return []

    async def pipeline_summary(self) -> dict[str, Any]:
        return {}

    async def list_tasks(self) -> list[dict[str, Any]]:
        return []


class PrintSlack:
    """Used through the real bridge so posting gates and cards still execute."""

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        print(json.dumps(kwargs, ensure_ascii=False))
        return {"ts": "dry-run", "channel": kwargs["channel"]}

    async def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
        return {"channel": {"id": "DDRYRUN"}}

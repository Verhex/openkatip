"""Disabled provider placeholder."""

from katip_ops.core.contract import Task
from katip_ops.executors.base import ExecutorResponse, Message, Tool


class XAIExecutor:
    name = "xai"
    provider = "xai"
    model = "disabled"

    async def run(
        self,
        task: Task,
        system: str,
        messages: list[Message],
        tools: list[Tool],
    ) -> ExecutorResponse:
        raise NotImplementedError("xAI executor is disabled")

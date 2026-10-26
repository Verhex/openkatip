"""Ordered config routes; residency filtering happens before every model call."""

from collections.abc import Mapping
from fnmatch import fnmatchcase

from katip_ops.config import ExecutorsConfig
from katip_ops.core.contract import Task
from katip_ops.executors.base import Executor, ExecutorResponse, Message, ProviderError, Tool


class NoEligibleExecutor(RuntimeError):
    def __init__(self, task: Task) -> None:
        self.user_message = "Bu görev ve veri sınıfı için uygun bir yürütücü bulunamadı."
        self.log_message = f"No eligible executor: type={task.type}, data_class={task.data_class}"
        super().__init__(self.log_message)


class Router:
    def __init__(self, config: ExecutorsConfig, executors: Mapping[str, Executor]) -> None:
        self.config = config
        self.executors = executors

    def candidates(self, task: Task) -> list[Executor]:
        routes = self.config.routing
        route = routes.routes.get(task.type)
        if route is None:
            route = next(
                (
                    names
                    for pattern, names in routes.routes.items()
                    if fnmatchcase(task.type, pattern)
                ),
                routes.default,
            )
        result = []
        for name in dict.fromkeys(route):
            entry = self.config.executors[name]
            if (
                entry.enabled
                and task.data_class in entry.allowed_data_classes
                and name in self.executors
            ):
                result.append(self.executors[name])
        if not result:
            raise NoEligibleExecutor(task)
        return result

    async def run(
        self,
        task: Task,
        system: str,
        messages: list[Message],
        tools: list[Tool],
    ) -> ExecutorResponse:
        candidates = self.candidates(task)
        for index, executor in enumerate(candidates):
            try:
                response = await executor.run(task, system, messages, tools)
                return response.model_copy(update={"executor": executor.name})
            except ProviderError:
                if index == len(candidates) - 1:
                    raise
        raise AssertionError("Unreachable: candidate list cannot be empty")

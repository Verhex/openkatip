from dataclasses import dataclass, field

import pytest

from katip_ops.config import ExecutorsConfig, Routing
from katip_ops.core.policy import PolicyRejection
from katip_ops.core.router import NoEligibleExecutor, Router
from katip_ops.executors.base import ExecutorResponse, ProviderError


@dataclass
class FakeExecutor:
    name: str
    failure: Exception | None = None
    response: ExecutorResponse = field(default_factory=ExecutorResponse)
    provider: str = "fake"
    model: str = "fake"
    calls: int = 0

    async def run(self, task, system, messages, tools):
        self.calls += 1
        if self.failure:
            raise self.failure
        return self.response.model_copy(update={"text": self.name})


def make_config(executor_config, primary="first"):
    return ExecutorsConfig(
        executors={
            "first": executor_config,
            "second": executor_config,
            "deepseek": executor_config.model_copy(
                update={
                    "provider": "deepseek",
                    "allowed_data_classes": ["public", "internal"],
                    "base_url": "https://example.invalid",
                }
            ),
        },
        routing=Routing(
            default=["first"],
            routes={
                "content.*": [primary, "second"],
                "content.special": ["second"],
                "research.*": ["deepseek", "first"],
            },
        ),
    )


@pytest.mark.asyncio
async def test_config_only_switch(task, executor_config):
    executors = {name: FakeExecutor(name) for name in ["first", "second"]}
    first = Router(make_config(executor_config), executors)
    second = Router(make_config(executor_config, "second"), executors)
    assert (await first.run(task, "", [], [])).text == "first"
    assert (await second.run(task, "", [], [])).text == "second"
    assert first.candidates(task.model_copy(update={"type": "unknown"}))[0].name == "first"
    assert first.candidates(task.model_copy(update={"type": "content.special"}))[0].name == "second"


async def test_fallback_order(task, executor_config):
    executors = {
        "first": FakeExecutor("first", ProviderError("timeout")),
        "second": FakeExecutor("second"),
    }
    router = Router(make_config(executor_config), executors)
    assert [executor.name for executor in router.candidates(task)] == ["first", "second"]
    response = await router.run(task, "", [], [])
    assert response.text == "second"
    assert response.executor == "second"
    executors["second"].failure = ProviderError("5xx")
    with pytest.raises(ProviderError):
        await router.run(task, "", [], [])


@pytest.mark.parametrize("failure", [PolicyRejection("blocked"), ValueError("bad request")])
async def test_no_fallback_for_policy_or_program_errors(task, executor_config, failure):
    executors = {"first": FakeExecutor("first", failure), "second": FakeExecutor("second")}
    with pytest.raises(type(failure)):
        await Router(make_config(executor_config), executors).run(task, "", [], [])
    assert executors["second"].calls == 0


async def test_no_fallback_for_refusal(task, executor_config):
    executors = {
        "first": FakeExecutor("first", response=ExecutorResponse(status="failed")),
        "second": FakeExecutor("second"),
    }
    response = await Router(make_config(executor_config), executors).run(task, "", [], [])
    assert response.status == "failed"
    assert executors["second"].calls == 0


@pytest.mark.parametrize("data_class", ["personal", "customer", "public", "internal"])
def test_residency(task, executor_config, data_class):
    router = Router(
        make_config(executor_config), {name: FakeExecutor(name) for name in ["deepseek", "first"]}
    )
    task = task.model_copy(update={"type": "research.web", "data_class": data_class})
    expected = ["first"] if data_class in ("personal", "customer") else ["deepseek", "first"]
    assert [candidate.name for candidate in router.candidates(task)] == expected


def test_no_eligible_executor(task, executor_config):
    router = Router(make_config(executor_config), {"deepseek": FakeExecutor("deepseek")})
    with pytest.raises(NoEligibleExecutor) as exc:
        router.candidates(
            task.model_copy(update={"type": "research.web", "data_class": "customer"})
        )
    assert "uygun" in exc.value.user_message
    assert "No eligible executor" in str(exc.value)
    disabled = executor_config.model_copy(update={"enabled": False})
    config = ExecutorsConfig(executors={"first": disabled}, routing=Routing(default=["first"]))
    with pytest.raises(NoEligibleExecutor):
        Router(config, {"first": FakeExecutor("first")}).candidates(task)


@pytest.mark.parametrize("data_class", ["personal", "customer"])
async def test_provider_failure_cannot_bypass_residency(task, executor_config, data_class):
    config = make_config(executor_config)
    config = config.model_copy(update={"routing": Routing(default=["first", "deepseek"])})
    executors = {
        "first": FakeExecutor("first", ProviderError("timeout")),
        "deepseek": FakeExecutor("deepseek"),
    }
    with pytest.raises(ProviderError):
        await Router(config, executors).run(
            task.model_copy(update={"data_class": data_class}),
            "",
            [],
            [],
        )
    assert executors["deepseek"].calls == 0

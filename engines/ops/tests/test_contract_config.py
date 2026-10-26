import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from katip_ops.config import ExecutorConfig, load_config
from katip_ops.core.contract import Cost, Event, ProposedAction, Task, TaskResult
from katip_ops.core.entitlement import check
from katip_ops.secrets import SecretUnavailable, read_secret


def test_contract_round_trip(task, action):
    assert Task.model_validate_json(task.model_dump_json()) == task
    result = TaskResult(
        task_id=task.id,
        executor="fake",
        status="needs_approval",
        proposed_actions=[action],
        cost=Cost(input_tokens=12),
        run_id="run",
    )
    assert TaskResult.model_validate_json(result.model_dump_json()) == result
    event = Event(type="meeting.summary_ready", source="katip", payload={"id": "meeting"})
    assert Event.model_validate_json(event.model_dump_json()) == event
    assert task.data_class == "internal"
    assert check("ops") is None
    with pytest.raises(ValidationError):
        task.type = "other"


def test_digest_stability_and_mutation(action):
    first = ProposedAction(
        kind=action.kind,
        target=action.target,
        payload={"z": [1, {"b": 2, "a": "İ"}], "a": 1},
        reason="one",
    )
    second = ProposedAction(
        kind=action.kind,
        target=action.target,
        payload={"a": 1, "z": [1, {"a": "İ", "b": 2}]},
        reason="two",
    )
    assert first.digest == second.digest
    assert len(first.digest) == 64
    second.payload["a"] = 2
    assert first.digest != second.digest
    assert ProposedAction.model_validate_json(first.model_dump_json()).digest == first.digest


@pytest.mark.parametrize("values", [{"input_tokens": -1}, {"usd": -1}, {"usd": float("nan")}])
def test_invalid_cost(values):
    with pytest.raises(ValidationError):
        Cost(**values)


def test_load_example_config_and_paths(tmp_path, monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "config"
    shutil.copy(directory / "settings.example.toml", tmp_path / "settings.toml")
    shutil.copy(directory / "executors.example.yaml", tmp_path / "executors.yaml")
    monkeypatch.setenv("KATIP_OPS_CONFIG", str(tmp_path))
    config = load_config()
    assert config.settings.database_path.is_absolute()
    assert config.settings.dry_run is True
    assert config.settings.ops_dir == tmp_path / "../work"
    assert config.settings.odoo.key_file == tmp_path / "/opt/katip/secrets/ops/odoo_api_key_test"
    assert (
        config.settings.slack.slack_bot_token == tmp_path / "/opt/katip/secrets/ops/slack_bot_token"
    )
    assert (
        config.settings.slack.slack_app_token == tmp_path / "/opt/katip/secrets/ops/slack_app_token"
    )
    assert config.settings.slack.channels["ops_approvals"] == "CREPLACEAPPROVALS"
    assert config.settings.policy.banned_terms == []
    assert config.executors.executors["claude-sonnet"].thinking == "adaptive"
    assert not config.executors.executors["deepseek"].enabled
    assert load_config(tmp_path) == config


def test_relative_key_paths(tmp_path):
    (tmp_path / "settings.toml").write_text('owner_slack_id="U1"', encoding="utf-8")
    (tmp_path / "executors.yaml").write_text(
        """
executors:
  fake:
    provider: claude
    model: model
    key_file: keys/api
    allowed_data_classes: [internal]
routing:
  default: [fake]
""",
        encoding="utf-8",
    )
    assert load_config(tmp_path).executors.executors["fake"].key_file == tmp_path / "keys/api"
    (tmp_path / "executors.yaml").write_text("executors: {}\nrouting: {default: [unknown]}")
    with pytest.raises(ValidationError):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "claude-haiku-any"},
        {"provider": "deepseek", "base_url": "https://example.invalid"},
        {"provider": "deepseek", "allowed_data_classes": ["internal"]},
    ],
)
def test_reject_unsafe_executor_config(executor_config, changes):
    with pytest.raises(ValidationError):
        ExecutorConfig.model_validate({**executor_config.model_dump(), **changes})


def test_secret_errors_never_include_value(tmp_path):
    path = tmp_path / "key"
    path.write_bytes(b"secret-value\r\n")
    assert read_secret(path) == "secret-value"
    path.write_bytes(b"secret-value\xff")
    with pytest.raises(SecretUnavailable) as exc:
        read_secret(path)
    assert str(exc.value) == str(path)
    assert exc.value.__suppress_context__
    path.write_text("")
    with pytest.raises(SecretUnavailable):
        read_secret(path)
    with pytest.raises(SecretUnavailable):
        read_secret(tmp_path / "missing")

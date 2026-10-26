from pathlib import Path
from uuid import uuid4

import pytest

from katip_ops.config import ExecutorConfig, Prices
from katip_ops.core.contract import ProposedAction, Task


@pytest.fixture
def tmp_path():
    # Keep all artifacts in this engine; avoid mode-0700 directories, which are
    # inaccessible to the restricted Windows token used in the build environment.
    directory = Path(__file__).resolve().parents[1] / ".test-tmp" / uuid4().hex
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def task():
    return Task(type="content.post", source="manual", requested_by="UOWNER")


@pytest.fixture
def action():
    return ProposedAction(
        kind="odoo.create_lead",
        target="crm.lead",
        payload={"name": "A"},
        reason="Qualified lead",
        requires_approval=False,
    )


@pytest.fixture
def executor_config():
    return ExecutorConfig(
        provider="claude",
        enabled=True,
        model="configured-model",
        key_file=Path("missing-key"),
        allowed_data_classes=["public", "internal", "personal", "customer"],
    )


@pytest.fixture
def prices():
    # Synthetic prices, deliberately unrelated to real provider pricing.
    return Prices(input=2, output=4, cache_read=1, cache_write=3, web_search=5)

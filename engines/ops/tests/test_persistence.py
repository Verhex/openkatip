import hashlib
import sqlite3
from datetime import UTC, datetime

import pytest

from katip_ops.core.approvals import Approvals
from katip_ops.core.contract import Cost
from katip_ops.core.ledger import Ledger


@pytest.fixture
def clock():
    return [datetime(2026, 10, 26, 12, tzinfo=UTC).timestamp()]


@pytest.fixture
def approvals(tmp_path, clock):
    store = Approvals(tmp_path / "ops.sqlite3", "UOWNER", clock=lambda: clock[0])
    yield store
    store.close()


def test_owner_only_and_single_use(approvals, action):
    identifier = approvals.create(action, "task", "run")
    with pytest.raises(PermissionError):
        approvals.decision(identifier, "approved", "UOTHER")
    assert approvals.get(identifier).status == "pending"
    with pytest.raises(ValueError):
        approvals.mark_applied(identifier)
    approved = approvals.decision(identifier, "approved", "UOWNER")
    assert approved.digest == action.digest
    assert approved.task_id == "task" and approved.run_id == "run"
    with pytest.raises(ValueError):
        approvals.decision(identifier, "rejected", "UOWNER")
    approvals.mark_applied(identifier)
    approvals.mark_applied(identifier)
    assert approvals.get(identifier).status == "applied"
    row = approvals.db.execute("SELECT actor, decided_at, applied_at FROM approvals").fetchone()
    assert row["actor"] == "UOWNER"
    assert row["decided_at"] is not None and row["applied_at"] is not None


def test_edit_changes_digest_and_preserves_original(approvals, action):
    identifier = approvals.create(action, "task", "run")
    # Creation snapshots nested mutable payloads, never retaining caller references.
    original_digest = action.digest
    action.payload["name"] = "Mutated externally"
    assert approvals.get(identifier).digest == original_digest
    approved = approvals.decision(identifier, "approved", "UOWNER", {"name": "Edited"})
    assert approved.digest != original_digest
    assert approved.original_digest == original_digest
    assert approved.digest == approved.action.digest
    assert approved.edited
    assert approved.action.payload == {"name": "Edited"}
    row = approvals.db.execute("SELECT original_action, action FROM approvals").fetchone()
    assert '"name":"A"' in row["original_action"]
    assert '"name":"Edited"' in row["action"]


@pytest.mark.parametrize("decide_first", [False, True])
def test_expiry_at_exact_boundary(approvals, clock, action, decide_first):
    identifier = approvals.create(action, "task", "run")
    if decide_first:
        approvals.decision(identifier, "approved", "UOWNER")
    clock[0] += 72 * 3600
    with pytest.raises(ValueError, match="süresi|geçerli"):
        if decide_first:
            approvals.mark_applied(identifier)
        else:
            approvals.decision(identifier, "approved", "UOWNER")


def test_rejected_and_unknown_never_apply(approvals, action):
    identifier = approvals.create(action, "task", "run")
    approvals.decision(identifier, "rejected", "UOWNER")
    with pytest.raises(ValueError):
        approvals.mark_applied(identifier)
    with pytest.raises(ValueError):
        approvals.mark_applied("unknown")
    with pytest.raises(ValueError):
        approvals.decision("unknown", "approved", "UOWNER")


def test_tampered_payload_never_applies(approvals, action):
    identifier = approvals.create(action, "task", "run")
    approvals.decision(identifier, "approved", "UOWNER")
    changed = action.model_copy(update={"payload": {"name": "tampered"}}).model_dump_json()
    with approvals.db:
        approvals.db.execute("UPDATE approvals SET action=? WHERE id=?", (changed, identifier))
    with pytest.raises(ValueError, match="özeti"):
        approvals.mark_applied(identifier)


def test_two_connections_cannot_reuse_decision(approvals, action, tmp_path, clock):
    other = Approvals(tmp_path / "ops.sqlite3", "UOWNER", clock=lambda: clock[0])
    try:
        identifier = approvals.create(action, "task", "run")
        other.decision(identifier, "approved", "UOWNER")
        with pytest.raises(ValueError):
            approvals.decision(identifier, "approved", "UOWNER")
        other.mark_applied(identifier)
        approvals.mark_applied(identifier)
    finally:
        other.close()


def test_custom_ttl_and_applied_idempotency_after_expiry(action, tmp_path, clock):
    store = Approvals(tmp_path / "custom.sqlite3", "UOWNER", ttl_hours=1, clock=lambda: clock[0])
    try:
        identifier = store.create(action, "task", "run")
        assert store.get(identifier).expires_at == clock[0] + 3600
        store.decision(identifier, "approved", "UOWNER")
        store.mark_applied(identifier)
        clock[0] += 3601
        store.mark_applied(identifier)
    finally:
        store.close()


@pytest.mark.parametrize("ledger_first", [False, True])
def test_shared_schema_migration_wal_and_reopen(tmp_path, clock, action, ledger_first):
    path = tmp_path / "shared.sqlite3"
    if ledger_first:
        ledger = Ledger(path)
        store = Approvals(path, "UOWNER")
    else:
        store = Approvals(path, "UOWNER")
        ledger = Ledger(path)
    identifier = store.create(action, "task", "run")
    assert ledger.db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert ledger.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    store.close()
    ledger.close()
    reopened = Approvals(path, "UOWNER")
    assert reopened.get(identifier).digest == action.digest
    reopened.close()


def test_future_schema_rejected(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="newer"):
        Ledger(path)


def test_ledger_cost_aggregation_summaries_and_no_secret_leak(tmp_path, task, clock):
    path = tmp_path / "ledger.sqlite3"
    ledger = Ledger(path, clock=lambda: clock[0])
    secret = "sk-test-DO-NOT-PERSIST-987654321"
    task = task.model_copy(update={"input": {"api_key": secret}})
    prompt = f"Private instructions containing {secret} and Türkçe"
    october = clock[0]
    clock[0] = datetime(2026, 9, 30, tzinfo=UTC).timestamp()
    ledger.record_start("previous", task, "fake", prompt)
    ledger.record_end("previous", "done", Cost(usd=100))
    clock[0] = october
    for run_id, cost, status in [
        ("a", Cost(input_tokens=10, usd=1.25), "done"),
        ("b", Cost(output_tokens=20, usd=2.5), "failed"),
        ("c", Cost(usd=None), "rejected_by_policy"),
    ]:
        ledger.record_start(run_id, task, "fake", prompt)
        ledger.record_end(run_id, status, cost)
    assert ledger.month_to_date_usd() == 3.75
    summaries = ledger.summaries(since=october)
    assert len(summaries) == 3
    assert summaries[0].cost.input_tokens == 10
    assert summaries[2].cost.usd is None
    assert summaries[0].prompt_hash == hashlib.sha256(prompt.encode()).hexdigest()
    assert summaries[0].prompt_bytes == len(prompt.encode())
    assert summaries[0].prompt_chars == len(prompt)
    assert summaries[0].task_id == task.id
    assert len(ledger.summaries(until=october - 1)) == 1
    # Check logical rows, active WAL and the database after checkpoint/close.
    assert secret not in "\n".join(ledger.db.iterdump())
    for candidate in tmp_path.glob("ledger.sqlite3*"):
        assert secret.encode() not in candidate.read_bytes()
    ledger.close()
    assert secret.encode() not in path.read_bytes()


def test_ledger_unknown_duplicate_completion_and_running(tmp_path, task, clock):
    ledger = Ledger(tmp_path / "ledger.sqlite3", clock=lambda: clock[0])
    try:
        with pytest.raises(ValueError):
            ledger.record_end("unknown", "done", Cost())
        ledger.record_start("run", task, "fake", "prompt")
        assert ledger.summaries()[0].status == "running"
        assert ledger.summaries()[0].cost is None
        with pytest.raises(sqlite3.IntegrityError):
            ledger.record_start("run", task, "fake", "prompt")
        ledger.record_end("run", "done", Cost(usd=1))
        with pytest.raises(ValueError):
            ledger.record_end("run", "done", Cost(usd=99))
        assert ledger.month_to_date_usd() == 1
    finally:
        ledger.close()

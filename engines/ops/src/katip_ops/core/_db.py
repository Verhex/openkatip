"""Shared schema versioning allows approvals and ledger to share a SQLite file."""

import sqlite3
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise ValueError("Database schema is newer than this engine")
        if version == 0:
            connection.execute("""CREATE TABLE approvals (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
                original_action TEXT NOT NULL, action TEXT NOT NULL,
                original_digest TEXT NOT NULL, digest TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL,
                status TEXT NOT NULL, actor TEXT, decided_at REAL,
                applied_at REAL, edited INTEGER NOT NULL DEFAULT 0
            )""")
            connection.execute("""CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_type TEXT NOT NULL,
                data_class TEXT NOT NULL, executor TEXT NOT NULL, status TEXT NOT NULL,
                started_at REAL NOT NULL, ended_at REAL, prompt_hash TEXT NOT NULL,
                prompt_bytes INTEGER NOT NULL, prompt_chars INTEGER NOT NULL,
                cost TEXT, usd REAL
            )""")
            connection.execute("PRAGMA user_version=1")
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        raise
    return connection

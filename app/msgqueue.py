import os
import json
import sqlite3
import time
import uuid
import threading
import logging

DB_PATH = os.environ.get("QUEUE_DB_PATH", "/data/queue.db")

_local = threading.local()


def _get_conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


def init_db():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id TEXT PRIMARY KEY,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            headers TEXT,
            body BLOB,
            chat_id TEXT,
            chat_type TEXT,
            attempt INTEGER DEFAULT 0,
            available_at REAL DEFAULT 0,
            status TEXT DEFAULT 'queued',
            created_at REAL DEFAULT (strftime('%s','now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_queue_ready
            ON queue (status, available_at)
    """)
    conn.commit()


def enqueue(method, path, headers, body, chat_id=None, chat_type=None):
    item_id = str(uuid.uuid4())
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO queue (id, method, path, headers, body, chat_id, chat_type, available_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            method,
            path,
            json.dumps(dict(headers)),
            body,
            str(chat_id) if chat_id else None,
            chat_type,
            time.time(),
        ),
    )
    conn.commit()
    return item_id


def dequeue_ready(batch_size=50):
    conn = _get_conn()
    now = time.time()
    rows = conn.execute(
        """
        SELECT * FROM queue
        WHERE status = 'queued' AND available_at <= ?
        ORDER BY available_at ASC
        LIMIT ?
        """,
        (now, batch_size),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_delivered(item_id):
    conn = _get_conn()
    conn.execute(
        "UPDATE queue SET status = 'delivered' WHERE id = ?",
        (item_id,),
    )
    conn.commit()


def mark_failed(item_id, reason=""):
    conn = _get_conn()
    conn.execute(
        "UPDATE queue SET status = 'failed' WHERE id = ?",
        (item_id,),
    )
    conn.commit()
    logging.warning("Item %s failed: %s", item_id, reason)


def reschedule(item_id, available_at):
    conn = _get_conn()
    conn.execute(
        """
        UPDATE queue
        SET available_at = ?, attempt = attempt + 1
        WHERE id = ?
        """,
        (available_at, item_id),
    )
    conn.commit()


def get_item(item_id):
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM queue WHERE id = ?", (item_id,)
    ).fetchone()
    return dict(row) if row else None


def count_pending():
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM queue WHERE status = 'queued'"
    ).fetchone()
    return row["cnt"] if row else 0

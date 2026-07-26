"""
Durable persistence.

Backed by SQLite so state survives process restarts, which is required
since the grader relies on caching/replay working across separate Check
runs, not just within one process's memory.

DEPLOYMENT NOTE: if you deploy this to a serverless platform with an
ephemeral filesystem (e.g. Vercel functions), the SQLite file will NOT
persist between cold starts/invocations, which will break replay and
caching across Checks. Point MAILROOM_DB at a mounted volume, or swap this
module for a hosted Postgres/SQLite-over-litestream/Redis backend -- the
rest of the app only calls the functions below, so the storage engine is
swappable without touching main.py.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

DB_PATH = os.environ.get("MAILROOM_DB", "/tmp/mailroom.db")

_lock = threading.Lock()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _lock, _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS evaluations (
                evaluation_id       TEXT PRIMARY KEY,
                dossiers_fingerprint TEXT NOT NULL,
                response_json       TEXT NOT NULL,
                receipt_key         TEXT,
                created_at          TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS proposals (
                call_id      TEXT PRIMARY KEY,
                dossier_id   TEXT NOT NULL,
                action       TEXT NOT NULL,
                target_json  TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                digest       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS decision_cache (
                content_fingerprint TEXT PRIMARY KEY,
                action       TEXT NOT NULL,
                target_json  TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS commits (
                receipt_id   TEXT PRIMARY KEY,
                outcome_json TEXT NOT NULL,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS effects (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id      TEXT NOT NULL,
                action       TEXT NOT NULL,
                detail_json  TEXT NOT NULL,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


# ---------------------------------------------------------------- evaluations

def get_evaluation(evaluation_id: str) -> Optional[sqlite3.Row]:
    with _lock, _conn() as conn:
        cur = conn.execute(
            "SELECT * FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)
        )
        return cur.fetchone()


def save_evaluation(
    evaluation_id: str,
    dossiers_fingerprint: str,
    response: dict,
    receipt_key: Optional[str],
) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO evaluations (evaluation_id, dossiers_fingerprint, response_json, receipt_key)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(evaluation_id) DO NOTHING""",
            (evaluation_id, dossiers_fingerprint, json.dumps(response), receipt_key),
        )


# ------------------------------------------------------------------ proposals

def get_proposal(call_id: str) -> Optional[sqlite3.Row]:
    with _lock, _conn() as conn:
        cur = conn.execute("SELECT * FROM proposals WHERE call_id = ?", (call_id,))
        return cur.fetchone()


def save_proposal(
    call_id: str, dossier_id: str, action: str, target: dict, payload: dict, digest: str
) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO proposals (call_id, dossier_id, action, target_json, payload_json, digest)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(call_id) DO NOTHING""",
            (call_id, dossier_id, action, json.dumps(target), json.dumps(payload), digest),
        )


# -------------------------------------------------------------- decision cache

def get_cached_decision(content_fingerprint: str) -> Optional[dict[str, Any]]:
    with _lock, _conn() as conn:
        cur = conn.execute(
            "SELECT * FROM decision_cache WHERE content_fingerprint = ?",
            (content_fingerprint,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "action": row["action"],
            "target": json.loads(row["target_json"]),
            "payload": json.loads(row["payload_json"]),
            "evidence": json.loads(row["evidence_json"]),
        }


def save_decision(content_fingerprint: str, action: str, target: dict, payload: dict, evidence: list[str]) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO decision_cache (content_fingerprint, action, target_json, payload_json, evidence_json)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(content_fingerprint) DO NOTHING""",
            (content_fingerprint, action, json.dumps(target), json.dumps(payload), json.dumps(evidence)),
        )


# ---------------------------------------------------------------------- commits

def get_commit_outcome(receipt_id: str) -> Optional[dict[str, Any]]:
    with _lock, _conn() as conn:
        cur = conn.execute("SELECT outcome_json FROM commits WHERE receipt_id = ?", (receipt_id,))
        row = cur.fetchone()
        return json.loads(row["outcome_json"]) if row else None


def save_commit_outcome(receipt_id: str, outcome: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO commits (receipt_id, outcome_json) VALUES (?, ?)
               ON CONFLICT(receipt_id) DO NOTHING""",
            (receipt_id, json.dumps(outcome)),
        )


def record_effect(call_id: str, action: str, detail: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO effects (call_id, action, detail_json) VALUES (?, ?, ?)",
            (call_id, action, json.dumps(detail)),
        )
